"""
Healthcare AI Agent Graph

Responsibilities:
- Preserve conversational context.
- Extract patient-provided appointment information.
- Handle date/time follow-ups.
- Handle weekday follow-ups such as "Thursday".
- Use live scheduling capabilities.
- Automatically check availability for complete booking requests.
- Automatically create an appointment when the requested real slot is available.
- Never invent doctors, hospitals, dates, times, or slots.
- Avoid repeated clarification questions.
- Keep the LLM responsible for conversation while keeping booking-critical
  decisions deterministic.
"""

from __future__ import annotations

import logging
import re
from datetime import date as date_type
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, TypedDict

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode

from app.ai.capabilities import (
    CapabilityContext,
    REGISTRY,
    TOOL_SCHEMAS,
)
from app.ai.prompts import SYSTEM_PROMPT


logger = logging.getLogger("healthcare")


# ============================================================
# CONFIGURATION
# ============================================================

MAX_TOOL_ROUNDS = 4

GROQ_MODEL_CANDIDATES = [
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "qwen/qwen3.8-27b",
]


# ============================================================
# STATE
# ============================================================

class AgentState(TypedDict):
    messages: List[Any]
    tool_rounds: int


# ============================================================
# CONTEXT DEFAULTS
# ============================================================

def _default_context() -> Dict[str, Any]:
    return {
        "current_intent": None,

        "specialty": None,
        "city": None,

        "hospital_id": None,
        "hospital_name": None,

        "doctor_id": None,
        "doctor_name": None,

        "date": None,
        "time": None,

        "selected_slot_id": None,
        "appointment_id": None,

        "booking_confirmed": False,
    }


def _normalize_context(context: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Normalize the persisted conversation context.

    Also supports older context keys:
    - selected_doctor_id
    - slot_id
    """
    result = _default_context()

    if isinstance(context, dict):
        result.update(context)

    # Backwards compatibility
    if not result.get("doctor_id"):
        result["doctor_id"] = result.get("selected_doctor_id")

    if not result.get("selected_slot_id"):
        result["selected_slot_id"] = result.get("slot_id")

    return result


# ============================================================
# BASIC HELPERS
# ============================================================

def _safe_string(value: Any) -> Optional[str]:
    if value is None:
        return None

    value = str(value).strip()

    return value if value else None


def _parse_tool_result(content: Any) -> Any:
    """
    ToolMessage.content can be a string, dict, list, etc.
    Try to recover structured JSON when possible.
    """
    if isinstance(content, (dict, list)):
        return content

    if not isinstance(content, str):
        return content

    text = content.strip()

    if not text:
        return text

    try:
        import json

        return json.loads(text)
    except Exception:
        return text


def _result_status(result: Any) -> Optional[str]:
    if not isinstance(result, dict):
        return None

    status = result.get("status")

    if status is None:
        return None

    return str(status).upper()


def _has_complete_booking_context(context: Dict[str, Any]) -> bool:
    """
    A booking request has enough scheduling information when:
    - doctor is known
    - date is known
    - time is known
    """
    return bool(
        context.get("doctor_id")
        and context.get("date")
        and context.get("time")
    )


def _is_explicit_booking_intent(context: Dict[str, Any], text: str) -> bool:
    """
    Detect whether the patient is trying to book an appointment.

    Explicit booking words are preferred.

    We also preserve an existing BOOK_APPOINTMENT intent because the
    patient may provide date/time in separate messages.
    """
    if context.get("current_intent") == "BOOK_APPOINTMENT":
        return True

    lowered = text.lower()

    booking_words = (
        "book",
        "booking",
        "schedule",
        "appointment",
        "reserve",
        "reservation",
        "see the doctor",
        "visit the doctor",
        "meet the doctor",
    )

    return any(word in lowered for word in booking_words)


# ============================================================
# DATE EXTRACTION
# ============================================================

def _extract_date(text: str) -> Optional[str]:
    """
    Extract explicit dates.

    Supported examples:
    - 26-09-2026
    - 26/09/2026
    - 26-09-26
    - 2026-09-26
    """

    if not text:
        return None

    # DD-MM-YYYY / DD/MM/YYYY
    match = re.search(
        r"\b(\d{1,2})[-/](\d{1,2})[-/](\d{4})\b",
        text,
    )

    if match:
        day = int(match.group(1))
        month = int(match.group(2))
        year = int(match.group(3))

        try:
            value = date_type(year, month, day)
            return value.isoformat()
        except ValueError:
            return None

    # DD-MM-YY / DD/MM/YY
    match = re.search(
        r"\b(\d{1,2})[-/](\d{1,2})[-/](\d{2})\b",
        text,
    )

    if match:
        day = int(match.group(1))
        month = int(match.group(2))
        year = 2000 + int(match.group(3))

        try:
            value = date_type(year, month, day)
            return value.isoformat()
        except ValueError:
            return None

    # YYYY-MM-DD
    match = re.search(
        r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b",
        text,
    )

    if match:
        year = int(match.group(1))
        month = int(match.group(2))
        day = int(match.group(3))

        try:
            value = date_type(year, month, day)
            return value.isoformat()
        except ValueError:
            return None

    return None


# ============================================================
# WEEKDAY EXTRACTION
# ============================================================

WEEKDAY_NAMES = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def _extract_weekday(text: str) -> Optional[int]:
    """
    Return weekday number if the patient explicitly gives a weekday.
    """
    if not text:
        return None

    lowered = text.lower()

    for name, number in WEEKDAY_NAMES.items():
        if re.search(rf"\b{name}\b", lowered):
            return number

    return None


def _resolve_weekday(
    weekday_number: int,
    base_date: Optional[str],
) -> Optional[str]:
    """
    Resolve a weekday relative to the stored date.

    Example:

        stored date = 2026-09-26 (Saturday)
        patient = "Thursday"

    Result:

        2026-09-24 (Thursday)

    This is useful when the assistant previously asked:
    "Which day this week?"
    and the patient replies:
    "Thursday".
    """

    if not base_date:
        return None

    try:
        base = datetime.strptime(base_date, "%Y-%m-%d").date()
    except ValueError:
        return None

    monday = base - timedelta(days=base.weekday())

    resolved = monday + timedelta(days=weekday_number)

    return resolved.isoformat()


# ============================================================
# TIME EXTRACTION
# ============================================================

def _extract_time(text: str) -> Optional[str]:
    """
    Extract times such as:

    10 am
    10:30 am
    2 pm
    2:30 pm
    14:00
    """

    if not text:
        return None

    lowered = text.lower()

    # 12-hour time
    match = re.search(
        r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b",
        lowered,
    )

    if match:
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        meridiem = match.group(3)

        if hour < 1 or hour > 12:
            return None

        if minute < 0 or minute > 59:
            return None

        if meridiem == "am":
            if hour == 12:
                hour = 0
        else:
            if hour != 12:
                hour += 12

        return f"{hour:02d}:{minute:02d}"

    # 24-hour time
    match = re.search(
        r"\b([01]?\d|2[0-3]):([0-5]\d)\b",
        lowered,
    )

    if match:
        hour = int(match.group(1))
        minute = int(match.group(2))

        return f"{hour:02d}:{minute:02d}"

    return None


# ============================================================
# SPECIALTY EXTRACTION
# ============================================================

SPECIALTY_MAP = {
    "cardiologist": "Cardiology",
    "cardiology": "Cardiology",

    "dermatologist": "Dermatology",
    "dermatology": "Dermatology",

    "dentist": "Dentistry",
    "dental": "Dentistry",
    "dentistry": "Dentistry",

    "orthopedic": "Orthopedics",
    "orthopaedic": "Orthopedics",
    "orthopedics": "Orthopedics",
    "orthopaedics": "Orthopedics",

    "neurologist": "Neurology",
    "neurology": "Neurology",

    "pediatrician": "Pediatrics",
    "paediatrician": "Pediatrics",
    "pediatrics": "Pediatrics",
    "paediatrics": "Pediatrics",

    "gynecologist": "Gynecology",
    "gynaecologist": "Gynecology",
    "gynecology": "Gynecology",
    "gynaecology": "Gynecology",

    "ophthalmologist": "Ophthalmology",
    "ophthalmology": "Ophthalmology",

    "psychiatrist": "Psychiatry",
    "psychiatry": "Psychiatry",

    "general physician": "General Medicine",
    "general medicine": "General Medicine",
}


def _extract_specialty(text: str) -> Optional[str]:
    if not text:
        return None

    lowered = text.lower()

    for key, specialty in SPECIALTY_MAP.items():
        if key in lowered:
            return specialty

    return None


# ============================================================
# USER CONTEXT EXTRACTION
# ============================================================

def _extract_user_context(
    context: Dict[str, Any],
    user_text: str,
) -> Dict[str, Any]:
    """
    Extract information from the latest patient message and merge
    it into persistent context.
    """

    updated = _normalize_context(context)

    text = user_text.strip()

    if not text:
        return updated

    # --------------------------------------------------------
    # SPECIALTY
    # --------------------------------------------------------

    specialty = _extract_specialty(text)

    if specialty:
        previous_specialty = updated.get("specialty")

        updated["specialty"] = specialty

        # If the patient explicitly changes specialty, old doctor
        # and slot should no longer be trusted.
        if (
            previous_specialty
            and previous_specialty.lower() != specialty.lower()
        ):
            updated["hospital_id"] = None
            updated["hospital_name"] = None
            updated["doctor_id"] = None
            updated["doctor_name"] = None
            updated["selected_slot_id"] = None

    # --------------------------------------------------------
    # DATE
    # --------------------------------------------------------

    explicit_date = _extract_date(text)

    if explicit_date:
        previous_date = updated.get("date")

        updated["date"] = explicit_date

        # Changing date invalidates selected slot.
        if previous_date != explicit_date:
            updated["selected_slot_id"] = None

    else:
        # ----------------------------------------------------
        # WEEKDAY
        # ----------------------------------------------------

        weekday_number = _extract_weekday(text)

        if weekday_number is not None:
            resolved_weekday = _resolve_weekday(
                weekday_number,
                updated.get("date"),
            )

            if resolved_weekday:
                previous_date = updated.get("date")

                updated["date"] = resolved_weekday

                if previous_date != resolved_weekday:
                    updated["selected_slot_id"] = None

    # --------------------------------------------------------
    # TIME
    # --------------------------------------------------------

    extracted_time = _extract_time(text)

    if extracted_time:
        previous_time = updated.get("time")

        updated["time"] = extracted_time

        # Changing time invalidates selected slot.
        if previous_time != extracted_time:
            updated["selected_slot_id"] = None

    # --------------------------------------------------------
    # BOOKING INTENT
    # --------------------------------------------------------

    if _is_explicit_booking_intent(updated, text):
        updated["current_intent"] = "BOOK_APPOINTMENT"

    return updated


# ============================================================
# CONTEXT UPDATE FROM TOOL RESULTS
# ============================================================

def _update_context(
    context: Dict[str, Any],
    result: Dict[str, Any],
    user_text: str,
) -> Dict[str, Any]:

    updated = _extract_user_context(
        _normalize_context(context),
        user_text,
    )

    messages = result.get("messages", [])

    # --------------------------------------------------------
    # READ AI TOOL CALLS
    # --------------------------------------------------------

    for message in messages:

        if not isinstance(message, AIMessage):
            continue

        tool_calls = getattr(message, "tool_calls", None)

        if not tool_calls:
            continue

        for call in tool_calls:
            name = call.get("name")
            args = call.get("args") or {}

            if not isinstance(args, dict):
                continue

            # Doctor
            if name == "search_doctors":

                doctor_id = (
                    args.get("doctor_id")
                    or args.get("selected_doctor_id")
                )

                doctor_name = args.get("doctor_name")

                if doctor_id:
                    updated["doctor_id"] = doctor_id

                if doctor_name:
                    updated["doctor_name"] = doctor_name

            # Hospital
            elif name == "search_hospitals":

                hospital_id = args.get("hospital_id")
                hospital_name = args.get("hospital_name")

                if hospital_id:
                    updated["hospital_id"] = hospital_id

                if hospital_name:
                    updated["hospital_name"] = hospital_name

            # Availability
            elif name == "check_availability":

                slot_id = args.get("slot_id")

                if slot_id:
                    updated["selected_slot_id"] = slot_id

    # --------------------------------------------------------
    # READ TOOL MESSAGES
    # --------------------------------------------------------

    for message in messages:

        if not isinstance(message, ToolMessage):
            continue

        parsed = _parse_tool_result(message.content)

        if not isinstance(parsed, dict):
            continue

        # ----------------------------------------------------
        # HOSPITAL SEARCH
        # ----------------------------------------------------

        if "hospitals" in parsed:

            hospitals = parsed.get("hospitals")

            if isinstance(hospitals, list) and len(hospitals) == 1:

                hospital = hospitals[0]

                if isinstance(hospital, dict):

                    updated["hospital_id"] = (
                        hospital.get("id")
                        or hospital.get("hospital_id")
                    )

                    updated["hospital_name"] = (
                        hospital.get("name")
                        or hospital.get("hospital_name")
                    )

        # ----------------------------------------------------
        # DOCTOR SEARCH
        # ----------------------------------------------------

        if "doctors" in parsed:

            doctors = parsed.get("doctors")

            if isinstance(doctors, list) and len(doctors) == 1:

                doctor = doctors[0]

                if isinstance(doctor, dict):

                    updated["doctor_id"] = (
                        doctor.get("id")
                        or doctor.get("doctor_id")
                    )

                    updated["doctor_name"] = (
                        doctor.get("name")
                        or doctor.get("doctor_name")
                    )

        # ----------------------------------------------------
        # AVAILABILITY
        # ----------------------------------------------------

        if (
            "slots" in parsed
            or "available_slots" in parsed
            or "requested_time_available" in parsed
        ):

            slots = (
                parsed.get("slots")
                or parsed.get("available_slots")
                or []
            )

            requested_available = parsed.get(
                "requested_time_available"
            )

            slot_id = parsed.get("slot_id")

            # Exact requested slot
            if slot_id and (
                requested_available is True
                or len(slots) == 1
            ):
                updated["selected_slot_id"] = slot_id

            # If one exact slot is returned
            if (
                isinstance(slots, list)
                and len(slots) == 1
            ):
                slot = slots[0]

                if isinstance(slot, dict):

                    updated["selected_slot_id"] = (
                        slot.get("slot_id")
                        or slot.get("id")
                    )

                    if slot.get("doctor_id"):
                        updated["doctor_id"] = slot["doctor_id"]

                    if slot.get("doctor_name"):
                        updated["doctor_name"] = slot["doctor_name"]

                    start_at = (
                        slot.get("start_at")
                        or slot.get("when")
                    )

                    if start_at:
                        try:
                            parsed_dt = _parse_datetime_value(start_at)

                            if parsed_dt:
                                updated["date"] = parsed_dt.strftime(
                                    "%Y-%m-%d"
                                )

                                updated["time"] = parsed_dt.strftime(
                                    "%H:%M"
                                )
                        except Exception:
                            pass

        # ----------------------------------------------------
        # CREATE APPOINTMENT
        # ----------------------------------------------------

        appointment_id = (
            parsed.get("appointment_id")
            or parsed.get("id")
        )

        if appointment_id:
            updated["appointment_id"] = appointment_id

        status = _result_status(parsed)

        if status == "CONFIRMED":
            updated["booking_confirmed"] = True

    return updated


# ============================================================
# DATETIME PARSING
# ============================================================

def _parse_datetime_value(value: Any) -> Optional[datetime]:
    if value is None:
        return None

    if isinstance(value, datetime):
        return value

    text = str(value).strip()

    if not text:
        return None

    # ISO datetime
    try:
        return datetime.fromisoformat(
            text.replace("Z", "+00:00")
        )
    except Exception:
        pass

    # Common formats
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(text, fmt)
        except Exception:
            continue

    return None


# ============================================================
# TOOL CREATION
# ============================================================

def _create_pydantic_schema(schema: Dict[str, Any]):
    """
    Convert OpenAI-style JSON schema into a Pydantic model.

    This keeps compatibility with the capability schemas already
    defined in capabilities.py.
    """
    from pydantic import BaseModel, create_model

    properties = schema.get("properties", {})
    required = set(schema.get("required", []))

    fields = {}

    for field_name, field_schema in properties.items():

        field_type = Any

        field_type_name = field_schema.get("type")

        if field_type_name == "string":
            field_type = str

        elif field_type_name == "integer":
            field_type = int

        elif field_type_name == "number":
            field_type = float

        elif field_type_name == "boolean":
            field_type = bool

        elif field_type_name == "array":
            field_type = list

        if field_name not in required:
            from typing import Optional

            field_type = Optional[field_type]

        default = ...
        if field_name not in required:
            default = None

        fields[field_name] = (
            field_type,
            default,
        )

    return create_model(
        schema.get("title", "ToolSchema"),
        **fields,
    )


# ============================================================
# BUILD TOOLS
# ============================================================

def build_tools(ctx: CapabilityContext):

    tools = []

    for tool_name, schema in TOOL_SCHEMAS.items():

        capability = REGISTRY.get(tool_name)

        if capability is None:
            logger.warning(
                "Capability %s is declared but not found in REGISTRY",
                tool_name,
            )
            continue

        try:
            model_schema = _create_pydantic_schema(schema)

            tool = capability.as_langchain_tool(
                ctx=ctx,
                args_schema=model_schema,
            )

            tools.append(tool)

        except AttributeError:
            try:
                tool = capability(
                    ctx=ctx,
                )

                tools.append(tool)

            except Exception:
                logger.exception(
                    "Could not build capability tool: %s",
                    tool_name,
                )

        except Exception:
            logger.exception(
                "Could not build capability tool: %s",
                tool_name,
            )

    return tools


# ============================================================
# GROQ MODEL
# ============================================================

def _build_groq_model(model_name: str):

    from langchain_groq import ChatGroq

    return ChatGroq(
        model=model_name,
        temperature=0,
    )


def _invoke_llm_with_fallback(
    tools: List[Any],
    messages: List[Any],
):
    """
    Try configured Groq models in order.
    """

    last_error = None

    for model_name in GROQ_MODEL_CANDIDATES:

        try:

            logger.info(
                "Attempting turn with Groq model: %s",
                model_name,
            )

            model = _build_groq_model(model_name)

            if tools:
                model = model.bind_tools(tools)

            return model.invoke(messages)

        except Exception as exc:

            last_error = exc

            logger.exception(
                "Groq model failed: %s",
                model_name,
            )

    raise RuntimeError(
        f"All Groq models failed: {last_error}"
    )


# ============================================================
# MESSAGE HISTORY
# ============================================================

def _trim_messages(
    messages: List[Any],
    max_messages: int = 12,
) -> List[Any]:

    if len(messages) <= max_messages:
        return messages

    system_messages = [
        message
        for message in messages
        if isinstance(message, SystemMessage)
    ]

    non_system_messages = [
        message
        for message in messages
        if not isinstance(message, SystemMessage)
    ]

    return system_messages + non_system_messages[-max_messages:]


# ============================================================
# BOOKING RESPONSE HELPERS
# ============================================================

def _extract_slot_from_result(
    result: Any,
) -> Optional[Dict[str, Any]]:

    if not isinstance(result, dict):
        return None

    slot_id = result.get("slot_id")

    requested_available = result.get(
        "requested_time_available"
    )

    if slot_id and requested_available is True:
        return {
            "slot_id": slot_id,
            "data": result,
        }

    slots = (
        result.get("slots")
        or result.get("available_slots")
        or []
    )

    if (
        isinstance(slots, list)
        and len(slots) == 1
        and isinstance(slots[0], dict)
    ):

        slot = slots[0]

        slot_id = (
            slot.get("slot_id")
            or slot.get("id")
        )

        if slot_id:
            return {
                "slot_id": slot_id,
                "data": slot,
            }

    return None


def _format_booking_confirmation(
    context: Dict[str, Any],
) -> str:
    """
    Short voice-friendly confirmation.

    The values come from context populated from the patient/tool data.
    """

    doctor = context.get("doctor_name") or "the doctor"
    hospital = context.get("hospital_name")

    date_value = context.get("date")
    time_value = context.get("time")

    if hospital:
        return (
            f"Your appointment with {doctor} at {hospital} "
            f"on {date_value} at {time_value} is confirmed."
        )

    return (
        f"Your appointment with {doctor} "
        f"on {date_value} at {time_value} is confirmed."
    )


def _format_unavailable_response(
    context: Dict[str, Any],
    availability_result: Dict[str, Any],
) -> Optional[str]:
    """
    Format only real alternatives returned by the capability.

    Never create times here.
    """

    doctor = context.get("doctor_name") or "the doctor"

    requested_time = context.get("time")
    date_value = context.get("date")

    slots = (
        availability_result.get("slots")
        or availability_result.get("available_slots")
        or availability_result.get("alternatives")
        or []
    )

    if not isinstance(slots, list):
        return None

    real_times = []

    for slot in slots:

        if not isinstance(slot, dict):
            continue

        value = (
            slot.get("start_at")
            or slot.get("when")
            or slot.get("start")
            or slot.get("time")
        )

        if not value:
            continue

        parsed = _parse_datetime_value(value)

        if parsed:
            real_times.append(
                parsed.strftime("%I:%M %p").lstrip("0")
            )
        else:
            real_times.append(str(value))

    # Remove duplicates while preserving order
    unique_times = []

    for value in real_times:
        if value not in unique_times:
            unique_times.append(value)

    if not unique_times:
        return None

    requested_display = requested_time or "that time"

    if len(unique_times) == 1:
        alternatives_text = unique_times[0]
    else:
        alternatives_text = " or ".join(unique_times)

    return (
        f"{requested_display} is not available for {doctor} "
        f"on {date_value}; available times are {alternatives_text}."
    )


# ============================================================
# DETERMINISTIC BOOKING
# ============================================================

def _execute_complete_booking(
    ctx: CapabilityContext,
    context: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Deterministic booking path.

    This is intentionally outside the LLM decision loop.

    Flow:

        doctor + date + time
                    ↓
            check_availability
                    ↓
              exact slot?
              /       \
            yes       no
             ↓         ↓
       create_appointment
                       ↓
                 real alternatives

    All actual operations still go through the capability layer.
    """

    doctor_id = context.get("doctor_id")
    date_value = context.get("date")
    time_value = context.get("time")

    if not (
        doctor_id
        and date_value
        and time_value
    ):
        return {
            "handled": False,
            "status": "INCOMPLETE",
        }

    logger.info(
        "DETERMINISTIC BOOKING: doctor=%s date=%s time=%s",
        context.get("doctor_name"),
        date_value,
        time_value,
    )

    # --------------------------------------------------------
    # CHECK AVAILABILITY
    # --------------------------------------------------------

    check_availability = REGISTRY.get(
        "check_availability"
    )

    if check_availability is None:
        logger.error(
            "check_availability capability not found"
        )

        return {
            "handled": False,
            "status": "CAPABILITY_MISSING",
        }

    try:

        availability_result = check_availability(
            ctx,
            doctor_id=doctor_id,
            when=f"{date_value} {time_value}",
        )

    except TypeError:

        # Some capability implementations may use start/end
        # instead of "when".
        try:

            start_value = datetime.strptime(
                f"{date_value} {time_value}",
                "%Y-%m-%d %H:%M",
            )

            end_value = start_value + timedelta(
                minutes=1
            )

            availability_result = check_availability(
                ctx,
                doctor_id=doctor_id,
                start=start_value,
                end=end_value,
            )

        except Exception:

            logger.exception(
                "check_availability failed"
            )

            return {
                "handled": False,
                "status": "AVAILABILITY_ERROR",
            }

    except Exception:

        logger.exception(
            "check_availability failed"
        )

        return {
            "handled": False,
            "status": "AVAILABILITY_ERROR",
        }

    availability_result = _parse_tool_result(
        availability_result
    )

    if not isinstance(
        availability_result,
        dict,
    ):
        return {
            "handled": False,
            "status": "INVALID_AVAILABILITY_RESULT",
        }

    logger.info(
        "AVAILABILITY RESULT: %s",
        availability_result,
    )

    # --------------------------------------------------------
    # EXACT SLOT AVAILABLE
    # --------------------------------------------------------

    selected_slot = _extract_slot_from_result(
        availability_result
    )

    if selected_slot:

        slot_id = selected_slot["slot_id"]

        logger.info(
            "EXACT SLOT AVAILABLE: slot_id=%s",
            slot_id,
        )

        create_appointment = REGISTRY.get(
            "create_appointment"
        )

        if create_appointment is None:

            logger.error(
                "create_appointment capability not found"
            )

            return {
                "handled": False,
                "status": "CAPABILITY_MISSING",
            }

        # ----------------------------------------------------
        # CREATE APPOINTMENT
        # ----------------------------------------------------

        try:

            appointment_result = create_appointment(
                ctx,
                slot_id=slot_id,
                confirmed_by_patient=True,
            )

        except TypeError:

            # Compatibility with capability versions that
            # may use a slightly different signature.
            try:

                appointment_result = create_appointment(
                    ctx,
                    slot_id=slot_id,
                )

            except Exception:

                logger.exception(
                    "create_appointment failed"
                )

                return {
                    "handled": False,
                    "status": "BOOKING_ERROR",
                }

        except Exception:

            logger.exception(
                "create_appointment failed"
            )

            return {
                "handled": False,
                "status": "BOOKING_ERROR",
            }

        appointment_result = _parse_tool_result(
            appointment_result
        )

        logger.info(
            "CREATE APPOINTMENT RESULT: %s",
            appointment_result,
        )

        status = _result_status(
            appointment_result
        )

        # ----------------------------------------------------
        # CONFIRMED
        # ----------------------------------------------------

        if status == "CONFIRMED":

            appointment_id = None

            if isinstance(
                appointment_result,
                dict,
            ):
                appointment_id = (
                    appointment_result.get(
                        "appointment_id"
                    )
                    or appointment_result.get("id")
                )

            context["selected_slot_id"] = slot_id
            context["booking_confirmed"] = True

            if appointment_id:
                context["appointment_id"] = appointment_id

            return {
                "handled": True,
                "status": "CONFIRMED",
                "appointment_result": appointment_result,
                "context": context,
            }

        # ----------------------------------------------------
        # NOT CONFIRMED
        # ----------------------------------------------------

        return {
            "handled": True,
            "status": status or "NOT_CONFIRMED",
            "appointment_result": appointment_result,
            "context": context,
        }

    # --------------------------------------------------------
    # REQUESTED SLOT UNAVAILABLE
    # --------------------------------------------------------

    context["selected_slot_id"] = None

    unavailable_response = _format_unavailable_response(
        context,
        availability_result,
    )

    return {
        "handled": True,
        "status": "UNAVAILABLE",
        "availability_result": availability_result,
        "response": unavailable_response,
        "context": context,
    }


# ============================================================
# GRAPH
# ============================================================

def build_graph(
    tools: List[Any],
):

    # --------------------------------------------------------
    # AGENT NODE
    # --------------------------------------------------------

    def agent_node(
        state: AgentState,
    ):

        messages = state["messages"]

        response = _invoke_llm_with_fallback(
            tools,
            messages,
        )

        return {
            "messages": [response],
            "tool_rounds": state.get(
                "tool_rounds",
                0,
            ) + 1,
        }

    # --------------------------------------------------------
    # FINALIZE NODE
    # --------------------------------------------------------

    def finalize_node(
        state: AgentState,
    ):

        messages = state["messages"]

        response = _invoke_llm_with_fallback(
            [],
            messages,
        )

        return {
            "messages": [response],
        }

    # --------------------------------------------------------
    # ROUTING
    # --------------------------------------------------------

    def route_after_agent(
        state: AgentState,
    ):

        messages = state["messages"]

        if not messages:
            return END

        last_message = messages[-1]

        tool_calls = getattr(
            last_message,
            "tool_calls",
            None,
        )

        if not tool_calls:
            return END

        rounds = state.get(
            "tool_rounds",
            0,
        )

        if rounds >= MAX_TOOL_ROUNDS:
            return "finalize"

        return "tools"

    # --------------------------------------------------------
    # GRAPH
    # --------------------------------------------------------

    workflow = StateGraph(
        AgentState
    )

    workflow.add_node(
        "agent",
        agent_node,
    )

    workflow.add_node(
        "tools",
        ToolNode(tools),
    )

    workflow.add_node(
        "finalize",
        finalize_node,
    )

    workflow.set_entry_point(
        "agent"
    )

    workflow.add_conditional_edges(
        "agent",
        route_after_agent,
        {
            "tools": "tools",
            "finalize": "finalize",
            END: END,
        },
    )

    workflow.add_edge(
        "tools",
        "agent",
    )

    workflow.add_edge(
        "finalize",
        END,
    )

    return workflow.compile()


# ============================================================
# RUN TURN
# ============================================================

def run_turn(
    db,
    conversation,
    user_text: str,
):
    """
    Main entry point used by ai_routes.py.

    Returns:

        {
            "reply": "...",
            "capabilities_used": [...],
            "correlation_id": "...",
            "context": {...}
        }
    """

    # ========================================================
    # LOAD PERSISTENT CONTEXT
    # ========================================================

    context = _normalize_context(
        getattr(
            conversation,
            "context",
            None,
        )
    )

    # ========================================================
    # EXTRACT LATEST USER INFORMATION
    # ========================================================

    context = _extract_user_context(
        context,
        user_text,
    )

    # Save immediately so follow-up messages can use it.
    conversation.context = context

    # ========================================================
    # DEBUG LOG
    # ========================================================

    complete_booking = (
        _has_complete_booking_context(
            context
        )
    )

    logger.info(
        "BOOKING CONTEXT: doctor=%s doctor_id=%s date=%s time=%s "
        "slot=%s intent=%s complete=%s",
        context.get("doctor_name"),
        context.get("doctor_id"),
        context.get("date"),
        context.get("time"),
        context.get("selected_slot_id"),
        context.get("current_intent"),
        complete_booking,
    )

    # ========================================================
    # CAPABILITY CONTEXT
    # ========================================================

    correlation_id = (
        getattr(
            conversation,
            "correlation_id",
            None,
        )
        or f"conversation-{getattr(conversation, 'id', 'unknown')}"
    )

    patient_id = getattr(
        conversation,
        "patient_id",
        None,
    )

    user_id = getattr(
        conversation,
        "user_id",
        None,
    )

    hospital_id = context.get(
        "hospital_id"
    )

    ctx = CapabilityContext(
        db=db,
        patient_id=patient_id,
        user_id=user_id,
        hospital_id=hospital_id,
        conversation_id=getattr(
            conversation,
            "id",
            None,
        ),
        correlation_id=correlation_id,
    )

    # ========================================================
    # DETERMINISTIC COMPLETE BOOKING
    # ========================================================

    explicit_booking = _is_explicit_booking_intent(
        context,
        user_text,
    )

    if (
        complete_booking
        and explicit_booking
        and not context.get("booking_confirmed")
    ):

        booking_result = _execute_complete_booking(
            ctx,
            context,
        )

        if booking_result.get("handled"):

            status = booking_result.get(
                "status"
            )

            # ------------------------------------------------
            # CONFIRMED
            # ------------------------------------------------

            if status == "CONFIRMED":

                context = _normalize_context(
                    booking_result.get(
                        "context",
                        context,
                    )
                )

                reply = _format_booking_confirmation(
                    context
                )

                conversation.context = context

                # Save user message
                try:
                    conversation.messages.append(
                        {
                            "role": "user",
                            "content": user_text,
                        }
                    )

                    conversation.messages.append(
                        {
                            "role": "assistant",
                            "content": reply,
                        }
                    )
                except Exception:
                    logger.exception(
                        "Could not save deterministic booking messages"
                    )

                return {
                    "reply": reply,
                    "capabilities_used": [
                        "check_availability",
                        "create_appointment",
                    ],
                    "correlation_id": correlation_id,
                    "context": context,
                }

            # ------------------------------------------------
            # UNAVAILABLE
            # ------------------------------------------------

            if status == "UNAVAILABLE":

                context = _normalize_context(
                    booking_result.get(
                        "context",
                        context,
                    )
                )

                reply = booking_result.get(
                    "response"
                )

                if not reply:
                    reply = (
                        "That time is not available; "
                        "please choose from the available times."
                    )

                conversation.context = context

                try:
                    conversation.messages.append(
                        {
                            "role": "user",
                            "content": user_text,
                        }
                    )

                    conversation.messages.append(
                        {
                            "role": "assistant",
                            "content": reply,
                        }
                    )
                except Exception:
                    logger.exception(
                        "Could not save unavailable-slot messages"
                    )

                return {
                    "reply": reply,
                    "capabilities_used": [
                        "check_availability",
                    ],
                    "correlation_id": correlation_id,
                    "context": context,
                }

            # ------------------------------------------------
            # BOOKING ATTEMPTED BUT NOT CONFIRMED
            # ------------------------------------------------

            if status not in (
                "BOOKING_ERROR",
                "CAPABILITY_MISSING",
                "AVAILABILITY_ERROR",
                "INVALID_AVAILABILITY_RESULT",
            ):

                appointment_result = booking_result.get(
                    "appointment_result"
                )

                # Never say confirmed unless actual tool result
                # says CONFIRMED.
                if isinstance(
                    appointment_result,
                    dict,
                ):
                    result_status = _result_status(
                        appointment_result
                    )

                    if result_status == "CONFIRMED":
                        context["booking_confirmed"] = True

                        reply = _format_booking_confirmation(
                            context
                        )

                        conversation.context = context

                        return {
                            "reply": reply,
                            "capabilities_used": [
                                "check_availability",
                                "create_appointment",
                            ],
                            "correlation_id": correlation_id,
                            "context": context,
                        }

    # ========================================================
    # NORMAL LLM / TOOL GRAPH
    # ========================================================

    tools = build_tools(
        ctx
    )

    # ========================================================
    # BUILD HISTORY
    # ========================================================

    history: List[Any] = []

    # Base system prompt
    history.append(
        SystemMessage(
            content=SYSTEM_PROMPT
        )
    )

    # --------------------------------------------------------
    # PREVIOUS CONVERSATION
    # --------------------------------------------------------

    previous_messages = getattr(
        conversation,
        "messages",
        None,
    )

    if previous_messages is None:
        previous_messages = []

    # Keep only recent messages
    previous_messages = previous_messages[-12:]

    for message in previous_messages:

        if not isinstance(
            message,
            dict,
        ):
            continue

        role = message.get(
            "role"
        )

        content = message.get(
            "content",
            "",
        )

        if not content:
            continue

        if role == "user":

            history.append(
                HumanMessage(
                    content=content
                )
            )

        elif role == "assistant":

            history.append(
                AIMessage(
                    content=content
                )
            )

    # ========================================================
    # CURRENT STATE INSTRUCTION
    # ========================================================
    #
    # IMPORTANT:
    # This comes AFTER previous conversation history.
    #
    # That prevents an old assistant question from overpowering
    # the latest persistent context.
    # ========================================================

    state_instruction = f"""
CURRENT PERSISTENT PATIENT CONTEXT:

{context}

The persistent context above is authoritative.

IMPORTANT:

- Do not ask again for information already present.
- Do not repeat an old question if the patient has already
  supplied its answer.
- Combine information from previous messages with the latest
  patient message.
- The patient may provide information in multiple messages.

BOOKING RULE:

If doctor_id, date, and time are present, the appointment
information is complete.

For an explicit booking request:

1. Use check_availability.
2. Use only real slots returned by the capability.
3. If the requested slot is available, create the appointment
   automatically.
4. Do not ask for redundant confirmation.
5. Pass confirmed_by_patient=true for an explicit booking request.
6. Say "confirmed" only if create_appointment returns
   status CONFIRMED.

DATE RULE:

If a date is already stored and the patient provides only a
time, use that time on the stored date.

Example:

Stored date: 2026-09-26
Patient: "10 am"

Interpret it as:

2026-09-26 at 10:00.

Do not replace the stored date with today's date.

WEEKDAY RULE:

If the patient gives a weekday after a date has been selected,
use the resolved date stored in context.

Do not ask again for the date if the weekday can be resolved
from the existing appointment context.

AVAILABILITY RULE:

Never invent appointment times.

If the requested time is unavailable, use only actual
alternatives returned by check_availability.

Do not create an alternative time yourself.

CLARIFICATION RULE:

Ask one short question only when required information is
actually missing.

Do not ask:

"Which day would you like?"

when date is already present.

Do not ask:

"What time would you like?"

when time is already present.

EMERGENCY:

If the patient describes an emergency or life-threatening
situation, tell them to contact local emergency services
immediately and call transfer_to_human.

MEDICAL:

Do not diagnose.

Do not provide treatment advice.

Do not provide medication advice.

Only help with healthcare platform operations and scheduling.

CURRENT USER MESSAGE:

{user_text}
"""

    history.append(
        SystemMessage(
            content=state_instruction
        )
    )

    # Latest user message
    history.append(
        HumanMessage(
            content=user_text
        )
    )

    # ========================================================
    # BUILD AND RUN GRAPH
    # ========================================================

    graph = build_graph(
        tools
    )

    state: AgentState = {
        "messages": history,
        "tool_rounds": 0,
    }

    try:

        result = graph.invoke(
            state,
            config={
                "recursion_limit": 12,
            },
        )

    except Exception:

        logger.exception(
            "Agent graph execution failed"
        )

        # Keep a safe short reply.
        reply = (
            "I’m sorry, I couldn’t complete that request right now."
        )

        try:
            conversation.messages.append(
                {
                    "role": "user",
                    "content": user_text,
                }
            )

            conversation.messages.append(
                {
                    "role": "assistant",
                    "content": reply,
                }
            )

            conversation.context = context

        except Exception:
            logger.exception(
                "Could not save graph error response"
            )

        return {
            "reply": reply,
            "capabilities_used": [],
            "correlation_id": correlation_id,
            "context": context,
        }

    # ========================================================
    # EXTRACT FINAL RESPONSE
    # ========================================================

    result_messages = result.get(
        "messages",
        [],
    )

    reply = ""

    for message in reversed(
        result_messages
    ):

        if isinstance(
            message,
            AIMessage,
        ):

            content = message.content

            if isinstance(
                content,
                str,
            ):

                reply = content.strip()

                if reply:
                    break

    if not reply:
        reply = (
            "I’m sorry, I couldn’t complete that request."
        )

    # ========================================================
    # UPDATE CONTEXT FROM TOOL RESULTS
    # ========================================================

    context = _update_context(
        context,
        result,
        user_text,
    )

    conversation.context = context

    # ========================================================
    # CAPABILITIES USED
    # ========================================================

    capabilities_used = []

    for message in result_messages:

        if not isinstance(
            message,
            AIMessage,
        ):
            continue

        tool_calls = getattr(
            message,
            "tool_calls",
            None,
        )

        if not tool_calls:
            continue

        for call in tool_calls:

            name = call.get(
                "name"
            )

            if (
                name
                and name not in capabilities_used
            ):
                capabilities_used.append(
                    name
                )

    # ========================================================
    # SAVE CONVERSATION
    # ========================================================

    try:

        conversation.messages.append(
            {
                "role": "user",
                "content": user_text,
            }
        )

        conversation.messages.append(
            {
                "role": "assistant",
                "content": reply,
            }
        )

    except Exception:

        logger.exception(
            "Could not save conversation messages"
        )

    # ========================================================
    # RETURN
    # ========================================================

    return {
        "reply": reply,
        "capabilities_used": capabilities_used,
        "correlation_id": correlation_id,
        "context": context,
    }
