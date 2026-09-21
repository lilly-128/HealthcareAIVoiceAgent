"""
LangGraph patient AI agent.

Responsibilities:
- Preserve conversation context between turns
- Accept patient information in any order
- Avoid repeatedly asking for information already provided
- Pass conversation context to the LLM
- Persist hospital, doctor, specialty, city, date and time selections
- Keep using live capability functions from capabilities.py
- Prevent infinite agent/tool loops
"""

import json
import logging
import os
import re

from datetime import datetime
from typing import Annotated, Optional, TypedDict

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from langchain_core.tools import StructuredTool

from langgraph.graph import (
    END,
    START,
    StateGraph,
)

from langgraph.graph.message import add_messages

from langgraph.prebuilt import ToolNode

from langchain_groq import ChatGroq

from pydantic import Field, create_model

from sqlalchemy.orm import Session

from ..config import settings
from ..models import AIConversation

from .capabilities import (
    REGISTRY,
    CapabilityContext,
)

from .prompts import SYSTEM_PROMPT


log = logging.getLogger("healthcare")


# ================================================================
# Graph safety configuration
# ================================================================

MAX_TOOL_ROUNDS = 4


# ================================================================
# Groq model fallbacks
# ================================================================

GROQ_MODEL_CANDIDATES = [
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "qwen/qwen3.8-27b",
]

# Only use explicitly supported active models.
ACTIVE_GROQ_MODELS = set(
    GROQ_MODEL_CANDIDATES
)


# ================================================================
# Tool schemas
# ================================================================

TOOL_SCHEMAS: dict[str, dict] = {

    "search_hospitals": {
        "city": "City name",
        "specialty": "Medical specialty name",
    },

    "search_doctors": {
        "specialty": "Medical specialty, for example Gynecology",
        "city": "City name",
        "hospital_id": "Hospital ID from a previous hospital search",
    },

    "check_availability": {
        "doctor_id": "Doctor ID from search_doctors",
        "when": "Natural language date or time window",
    },

    "lookup_patient": {},

    "get_appointment": {
        "appointment_id": "Optional appointment ID",
    },

    "get_context": {},

    "update_preferences": {
        "key": "Preference key",
        "value": "Preference value",
    },

    "create_appointment": {
        "slot_id": "Slot ID returned by check_availability",
        "confirmed_by_patient": (
            "Set true for a complete patient booking request. "
            "When the patient has clearly requested a doctor, date "
            "and time, do not ask them to verify availability or "
            "repeat confirmation."
        ),
    },

    "reschedule_appointment": {
        "appointment_id": "",
        "new_slot_id": "",
    },

    "cancel_appointment": {
        "appointment_id": "",
    },

    "get_questionnaire": {
        "appointment_id": "",
    },

    "submit_questionnaire": {
        "response_id": "",
        "answers": "JSON object containing question key -> answer",
    },

    "send_notification": {
        "template": "",
        "body": "",
    },

    "start_workflow": {
        "workflow": "",
        "appointment_id": "",
    },

    "verify_external_appointment": {
        "appointment_id": "",
    },

    "synchronize_state": {
        "appointment_id": "",
    },

    "transfer_to_human": {
        "reason": "",
    },
}


# ================================================================
# Conversation state
# ================================================================

class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    tool_rounds: int


# ================================================================
# Initial persistent context
# ================================================================

def _default_context() -> dict:

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


# ================================================================
# Normalize old context
# ================================================================

def _normalize_context(
    old_context: Optional[dict],
) -> dict:

    context = _default_context()

    if old_context:
        context.update(old_context)

    # Backward compatibility with old context keys

    if not context.get("doctor_id"):
        context["doctor_id"] = context.get(
            "selected_doctor_id"
        )

    if not context.get("selected_slot_id"):
        context["selected_slot_id"] = context.get(
            "slot_id"
        )

    return context


# ================================================================
# Pydantic tool schema
# ================================================================

def _create_pydantic_schema(
    tool_name: str,
    params: dict,
):

    fields = {}

    for param_name, param_desc in params.items():

        fields[param_name] = (
            Optional[str],
            Field(
                default=None,
                description=param_desc or "Argument",
            ),
        )

    # Zero argument tools need a placeholder

    if not fields:

        fields["dummy"] = (
            Optional[str],
            Field(
                default=None,
                description="Unused placeholder argument",
            ),
        )

    return create_model(
        f"{tool_name}_input",
        **fields,
    )


# ================================================================
# Build tools
# ================================================================

def build_tools(
    ctx: CapabilityContext,
) -> list[StructuredTool]:

    tools = []

    for name, params in TOOL_SCHEMAS.items():

        target_fn = REGISTRY[name]

        def make_call(
            fn=target_fn,
            tool_name=name,
        ):

            def call(**kwargs):

                try:

                    kwargs.pop(
                        "dummy",
                        None,
                    )

                    result = fn(
                        ctx,
                        **kwargs,
                    )

                    result_str = json.dumps(
                        result,
                        default=str,
                    )

                    if len(result_str) > 5000:

                        result_str = (
                            result_str[:5000]
                            + "... [truncated]"
                        )

                    return result_str

                except Exception as err:

                    log.error(
                        "Tool execution error in %s: %s",
                        tool_name,
                        err,
                        exc_info=True,
                    )

                    return json.dumps(
                        {
                            "error": str(err),
                        }
                    )

            return call

        description = (
            target_fn.__doc__
            or f"Capability {name}."
        ).strip()

        schema = _create_pydantic_schema(
            name,
            params,
        )

        tools.append(
            StructuredTool.from_function(
                func=make_call(),
                name=name,
                description=description[:500],
                args_schema=schema,
            )
        )

    return tools


# ================================================================
# API key
# ================================================================

def _get_api_key():

    key = (
        getattr(
            settings,
            "GROQ_API_KEY",
            None,
        )
        or getattr(
            settings,
            "OPENAI_API_KEY",
            None,
        )
        or os.getenv(
            "GROQ_API_KEY",
            "",
        )
    )

    if not key:

        raise ValueError(
            "GROQ_API_KEY is missing. "
            "Set GROQ_API_KEY in .env or docker-compose.yml."
        )

    return key


# ================================================================
# Get Groq candidate models
# ================================================================

def _get_model_candidates():

    configured_model = (
        getattr(
            settings,
            "LLM_MODEL",
            "",
        )
        or "openai/gpt-oss-120b"
    )

    candidates = []

    # Use configured model only when it is an active model.
    # This prevents stale/deprecated model IDs from being sent to Groq.

    if configured_model in ACTIVE_GROQ_MODELS:

        candidates.append(
            configured_model
        )

    for model_name in GROQ_MODEL_CANDIDATES:

        if model_name not in candidates:

            candidates.append(
                model_name
            )

    return candidates


# ================================================================
# LLM with tools
# ================================================================

def _invoke_llm_with_fallback(
    tools: list[StructuredTool],
    history: list[BaseMessage],
):

    api_key = _get_api_key()

    candidates = _get_model_candidates()

    last_error = None

    for model_name in candidates:

        try:

            log.info(
                "Attempting turn with Groq model: %s",
                model_name,
            )

            llm = ChatGroq(
                model=model_name,
                temperature=0,
                api_key=api_key,
            ).bind_tools(tools)

            return llm.invoke(
                history
            )

        except Exception as error:

            log.warning(
                "Groq model '%s' failed: %s",
                model_name,
                error,
            )

            last_error = error

    if last_error:

        raise last_error

    raise RuntimeError(
        "No Groq model was available."
    )


# ================================================================
# LLM WITHOUT TOOLS
# ================================================================

def _invoke_llm_without_tools(
    history: list[BaseMessage],
):

    api_key = _get_api_key()

    candidates = _get_model_candidates()

    last_error = None

    for model_name in candidates:

        try:

            log.info(
                "Attempting final response with Groq model: %s",
                model_name,
            )

            llm = ChatGroq(
                model=model_name,
                temperature=0,
                api_key=api_key,
            )

            return llm.invoke(
                history
            )

        except Exception as error:

            log.warning(
                "Groq final-response model '%s' failed: %s",
                model_name,
                error,
            )

            last_error = error

    if last_error:

        raise last_error

    raise RuntimeError(
        "No Groq model was available for final response."
    )


# ================================================================
# Trim history
# ================================================================

def _trim_messages(
    messages: list[BaseMessage],
    max_messages: int = 12,
) -> list[BaseMessage]:

    system_messages = [
        message
        for message in messages
        if isinstance(
            message,
            SystemMessage,
        )
    ]

    other_messages = [
        message
        for message in messages
        if not isinstance(
            message,
            SystemMessage,
        )
    ]

    if len(other_messages) <= max_messages:

        return (
            system_messages
            + other_messages
        )

    trimmed = other_messages[
        -max_messages:
    ]

    # A ToolMessage should normally have its
    # preceding AI tool-call message.

    while (
        trimmed
        and isinstance(
            trimmed[0],
            ToolMessage,
        )
    ):

        index = (
            len(other_messages)
            - len(trimmed)
            - 1
        )

        if index >= 0:

            trimmed.insert(
                0,
                other_messages[index],
            )

        else:

            break

    return (
        system_messages
        + trimmed
    )


# ================================================================
# Extract date from normal user text
# ================================================================

def _extract_date(
    text: str,
) -> Optional[str]:

    text = text.strip()

    # ------------------------------------------------------------
    # Standard formats:
    #
    # 26-09-2026
    # 26/09/2026
    # 26-09-26
    # 26/09/26
    # ------------------------------------------------------------

    patterns = [
        r"\b(\d{1,2})[-/](\d{1,2})[-/](\d{4})\b",
        r"\b(\d{1,2})[-/](\d{1,2})[-/](\d{2})\b",
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            text,
        )

        if not match:

            continue

        day = int(
            match.group(1)
        )

        month = int(
            match.group(2)
        )

        year = int(
            match.group(3)
        )

        if year < 100:

            year += 2000

        try:

            value = datetime(
                year,
                month,
                day,
            )

            return value.strftime(
                "%Y-%m-%d"
            )

        except ValueError:

            return None

    # ------------------------------------------------------------
    # ISO date formats:
    #
    # 2026-09-26
    # 2026-09-26T11:00:00
    # 2026-09-26 11:00:00
    # ------------------------------------------------------------

    iso_match = re.search(
        r"\b(\d{4})-(\d{2})-(\d{2})\b",
        text,
    )

    if iso_match:

        year = int(
            iso_match.group(1)
        )

        month = int(
            iso_match.group(2)
        )

        day = int(
            iso_match.group(3)
        )

        try:

            value = datetime(
                year,
                month,
                day,
            )

            return value.strftime(
                "%Y-%m-%d"
            )

        except ValueError:

            return None

    return None


# ================================================================
# Extract time
# ================================================================

def _extract_time(
    text: str,
) -> Optional[str]:

    text = text.lower().strip()

    pattern = (
        r"\b(\d{1,2})"
        r"(?:[:.](\d{2}))?"
        r"\s*"
        r"(am|pm)\b"
    )

    match = re.search(
        pattern,
        text,
    )

    if not match:

        return None

    hour = int(
        match.group(1)
    )

    minute = (
        int(match.group(2))
        if match.group(2)
        else 0
    )

    period = match.group(3)

    if hour < 1 or hour > 12:

        return None

    if minute < 0 or minute > 59:

        return None

    if period == "am":

        if hour == 12:

            hour = 0

    else:

        if hour != 12:

            hour += 12

    return f"{hour:02d}:{minute:02d}"


# ================================================================
# Detect simple specialty changes
# ================================================================

def _extract_specialty(
    text: str,
) -> Optional[str]:

    value = text.lower().strip()

    mappings = {

        "gynecologist": "Gynecology",
        "gynaecologist": "Gynecology",
        "gynecology": "Gynecology",
        "gynaecology": "Gynecology",
        "gyn": "Gynecology",

        "cardiologist": "Cardiology",
        "cardiology": "Cardiology",

        "orthopedic": "Orthopedics",
        "orthopaedic": "Orthopedics",
        "orthopedics": "Orthopedics",

        "dermatologist": "Dermatology",
        "dermatology": "Dermatology",

        "neurologist": "Neurology",
        "neurology": "Neurology",

        "pediatrician": "Pediatrics",
        "paediatrician": "Pediatrics",
        "pediatrics": "Pediatrics",

        "dentist": "Dentistry",
        "dentistry": "Dentistry",
    }

    for key, specialty in mappings.items():

        if re.search(
            rf"\b{re.escape(key)}\b",
            value,
        ):

            return specialty

    return None


# ================================================================
# Extract information directly from user's message
# ================================================================

def _extract_user_context(
    context: dict,
    user_text: str,
) -> dict:

    updated = dict(context)

    text = user_text.strip()

    if not text:

        return updated

    # ------------------------------------------------------------
    # Specialty
    # ------------------------------------------------------------

    specialty = _extract_specialty(
        text
    )

    if specialty:

        old_specialty = context.get(
            "specialty"
        )

        updated[
            "specialty"
        ] = specialty

        # User explicitly changed specialty.
        # Existing hospital/doctor may now be incompatible.

        if (
            old_specialty
            and old_specialty.lower()
            != specialty.lower()
        ):

            updated[
                "hospital_id"
            ] = None

            updated[
                "hospital_name"
            ] = None

            updated[
                "doctor_id"
            ] = None

            updated[
                "doctor_name"
            ] = None

            updated[
                "selected_slot_id"
            ] = None

    # ------------------------------------------------------------
    # Date
    # ------------------------------------------------------------

    date_value = _extract_date(
        text
    )

    if date_value:

        # A new date means the old slot is no longer valid.

        if context.get("date") != date_value:

            updated[
                "selected_slot_id"
            ] = None

        updated[
            "date"
        ] = date_value

    # ------------------------------------------------------------
    # Time
    # ------------------------------------------------------------

    time_value = _extract_time(
        text
    )

    if time_value:

        # A new time means the old slot is no longer valid.

        if context.get("time") != time_value:

            updated[
                "selected_slot_id"
            ] = None

        updated[
            "time"
        ] = time_value

    # ------------------------------------------------------------
    # Booking intent
    # ------------------------------------------------------------

    if re.search(
        r"\b(?:book|schedule|reserve|appointment)\b",
        text.lower(),
    ):

        updated[
            "current_intent"
        ] = "BOOK_APPOINTMENT"

    return updated


# ================================================================
# Safely convert tool result JSON
# ================================================================

def _parse_tool_result(
    content,
):

    if not isinstance(
        content,
        str,
    ):

        return None

    try:

        return json.loads(
            content
        )

    except Exception:

        return None


# ================================================================
# Update context from tool arguments + tool results
# ================================================================

def _update_context(
    context: dict,
    result: dict,
    user_text: str,
) -> dict:

    context = _normalize_context(
        context
    )

    # ============================================================
    # First extract information directly from latest user text
    # ============================================================

    context = _extract_user_context(
        context,
        user_text,
    )

    # ============================================================
    # Process every message generated during this turn
    # ============================================================

    for message in result.get(
        "messages",
        [],
    ):

        # --------------------------------------------------------
        # AI tool calls
        # --------------------------------------------------------

        if isinstance(
            message,
            AIMessage,
        ):

            for tool_call in (
                message.tool_calls or []
            ):

                tool_name = tool_call.get(
                    "name"
                )

                args = (
                    tool_call.get("args")
                    or {}
                )

                # ------------------------------------------------
                # Hospital search
                # ------------------------------------------------

                if tool_name == "search_hospitals":

                    if args.get("city"):

                        context[
                            "city"
                        ] = args["city"]

                    if args.get("specialty"):

                        context[
                            "specialty"
                        ] = args[
                            "specialty"
                        ]

                # ------------------------------------------------
                # Doctor search
                # ------------------------------------------------

                elif tool_name == "search_doctors":

                    if args.get("city"):

                        context[
                            "city"
                        ] = args["city"]

                    if args.get("specialty"):

                        context[
                            "specialty"
                        ] = args[
                            "specialty"
                        ]

                    if args.get("hospital_id"):

                        context[
                            "hospital_id"
                        ] = args[
                            "hospital_id"
                        ]

                # ------------------------------------------------
                # Availability
                # ------------------------------------------------

                elif tool_name == "check_availability":

                    if args.get("doctor_id"):

                        context[
                            "doctor_id"
                        ] = args[
                            "doctor_id"
                        ]

                    if args.get("when"):

                        when = str(
                            args["when"]
                        )

                        date_value = (
                            _extract_date(
                                when
                            )
                        )

                        time_value = (
                            _extract_time(
                                when
                            )
                        )

                        if date_value:

                            context[
                                "date"
                            ] = date_value

                        if time_value:

                            context[
                                "time"
                            ] = time_value

                # ------------------------------------------------
                # Appointment creation
                # ------------------------------------------------

                elif tool_name == "create_appointment":

                    if args.get("slot_id"):

                        context[
                            "selected_slot_id"
                        ] = args[
                            "slot_id"
                        ]

                    # A tool call is NOT proof that booking succeeded.

                    context[
                        "current_intent"
                    ] = "BOOK_APPOINTMENT"

        # --------------------------------------------------------
        # Tool results
        # --------------------------------------------------------

        elif isinstance(
            message,
            ToolMessage,
        ):

            data = _parse_tool_result(
                message.content
            )

            if not isinstance(
                data,
                dict,
            ):

                continue

            # ====================================================
            # Hospital search result
            # ====================================================

            if (
                message.name
                == "search_hospitals"
            ):

                hospitals = (
                    data.get("hospitals")
                    or data.get("results")
                    or data.get("data")
                )

                if isinstance(
                    hospitals,
                    list,
                ):

                    # Only automatically select when exactly
                    # one hospital was found.

                    if len(hospitals) == 1:

                        hospital = hospitals[0]

                        if isinstance(
                            hospital,
                            dict,
                        ):

                            context[
                                "hospital_id"
                            ] = (
                                hospital.get(
                                    "hospital_id"
                                )
                                or hospital.get(
                                    "id"
                                )
                            )

                            context[
                                "hospital_name"
                            ] = (
                                hospital.get(
                                    "name"
                                )
                                or hospital.get(
                                    "hospital_name"
                                )
                            )

                            context[
                                "city"
                            ] = (
                                hospital.get(
                                    "city"
                                )
                                or context.get(
                                    "city"
                                )
                            )

            # ====================================================
            # Doctor search result
            # ====================================================

            elif (
                message.name
                == "search_doctors"
            ):

                doctors = (
                    data.get("doctors")
                    or data.get("results")
                    or data.get("data")
                )

                if isinstance(
                    doctors,
                    list,
                ):

                    # Only automatically select when exactly
                    # one doctor was found.

                    if len(doctors) == 1:

                        doctor = doctors[0]

                        if isinstance(
                            doctor,
                            dict,
                        ):

                            context[
                                "doctor_id"
                            ] = (
                                doctor.get(
                                    "doctor_id"
                                )
                                or doctor.get(
                                    "id"
                                )
                            )

                            context[
                                "doctor_name"
                            ] = (
                                doctor.get(
                                    "name"
                                )
                                or doctor.get(
                                    "doctor_name"
                                )
                            )

                            if doctor.get(
                                "hospital_id"
                            ):

                                context[
                                    "hospital_id"
                                ] = doctor[
                                    "hospital_id"
                                ]

            # ====================================================
            # Availability result
            # ====================================================

            elif (
                message.name
                == "check_availability"
            ):

                slots = (
                    data.get("slots")
                    or data.get(
                        "available_slots"
                    )
                    or data.get("results")
                    or data.get("data")
                )

                if isinstance(
                    slots,
                    list,
                ):

                    # If exactly one slot exists,
                    # remember it.

                    if len(slots) == 1:

                        slot = slots[0]

                        if isinstance(
                            slot,
                            dict,
                        ):

                            context[
                                "selected_slot_id"
                            ] = (
                                slot.get("id")
                                or slot.get(
                                    "slot_id"
                                )
                            )

                            if slot.get(
                                "doctor_id"
                            ):

                                context[
                                    "doctor_id"
                                ] = slot[
                                    "doctor_id"
                                ]

                            start_at = slot.get(
                                "start_at"
                            )

                            if start_at:

                                start_text = str(
                                    start_at
                                )

                                date_value = (
                                    _extract_date(
                                        start_text
                                    )
                                )

                                time_value = (
                                    _extract_time(
                                        start_text
                                    )
                                )

                                if date_value:

                                    context[
                                        "date"
                                    ] = date_value

                                if time_value:

                                    context[
                                        "time"
                                    ] = time_value

            # ====================================================
            # Create appointment result
            # ====================================================

            elif (
                message.name
                == "create_appointment"
            ):

                appointment_id = (
                    data.get(
                        "appointment_id"
                    )
                    or data.get(
                        "id"
                    )
                )

                if appointment_id:

                    context[
                        "appointment_id"
                    ] = appointment_id

                if (
                    data.get("status")
                    == "CONFIRMED"
                    and appointment_id
                ):

                    # Only a successful CONFIRMED result
                    # may mark the booking as confirmed.

                    context[
                        "booking_confirmed"
                    ] = True

                    context[
                        "current_intent"
                    ] = "BOOK_APPOINTMENT"

                else:

                    # Errors, conflicts, pending verification,
                    # reconciliation states, etc.

                    context[
                        "booking_confirmed"
                    ] = False

    return context


# ================================================================
# Build LangGraph
# ================================================================

def build_graph(
    ctx: CapabilityContext,
):

    tools = build_tools(
        ctx
    )

    # ------------------------------------------------------------
    # Normal agent node
    # ------------------------------------------------------------

    def agent(
        state: AgentState,
    ):

        history = _trim_messages(
            state["messages"],
            max_messages=12,
        )

        response = _invoke_llm_with_fallback(
            tools,
            history,
        )

        return {
            "messages": [
                response
            ]
        }

    # ------------------------------------------------------------
    # Tool node
    # ------------------------------------------------------------

    tool_node = ToolNode(
        tools
    )

    def run_tools(
        state: AgentState,
    ):

        current_rounds = state.get(
            "tool_rounds",
            0,
        )

        result = tool_node.invoke(
            state
        )

        return {
            "messages": result.get(
                "messages",
                [],
            ),

            "tool_rounds": (
                current_rounds + 1
            ),
        }

    # ------------------------------------------------------------
    # Finalize node
    # ------------------------------------------------------------

    def finalize(
        state: AgentState,
    ):

        history = _trim_messages(
            state["messages"],
            max_messages=12,
        )

        history = history + [
            SystemMessage(
                content=(
                    "You have reached the maximum number of "
                    "tool execution rounds for this patient turn. "
                    "Do not call any tools. "
                    "Respond to the patient using only the "
                    "information already available in the "
                    "conversation and tool results. "
                    "Do not invent hospitals, doctors, availability, "
                    "appointments, or other healthcare data. "
                    "If the requested action could not be completed, "
                    "clearly explain what information is still needed."
                )
            )
        ]

        response = _invoke_llm_without_tools(
            history
        )

        return {
            "messages": [
                response
            ]
        }

    # ============================================================
    # Create graph
    # ============================================================

    graph = StateGraph(
        AgentState
    )

    # ============================================================
    # Add nodes
    # ============================================================

    graph.add_node(
        "agent",
        agent,
    )

    graph.add_node(
        "tools",
        run_tools,
    )

    graph.add_node(
        "finalize",
        finalize,
    )

    # ============================================================
    # START -> agent
    # ============================================================

    graph.add_edge(
        START,
        "agent",
    )

    # ============================================================
    # Agent routing
    # ============================================================

    def route_after_agent(
        state: AgentState,
    ):

        messages = state.get(
            "messages",
            [],
        )

        tool_rounds = state.get(
            "tool_rounds",
            0,
        )

        if not messages:

            return END

        last_message = messages[-1]

        tool_calls = (
            getattr(
                last_message,
                "tool_calls",
                None,
            )
            or []
        )

        if not tool_calls:

            return END

        if tool_rounds >= MAX_TOOL_ROUNDS:

            log.warning(
                "Maximum tool rounds reached: %s",
                tool_rounds,
            )

            return "finalize"

        return "tools"

    # ============================================================
    # Conditional edge
    # ============================================================

    graph.add_conditional_edges(
        "agent",
        route_after_agent,
        {
            "tools": "tools",
            "finalize": "finalize",
            END: END,
        },
    )

    # ============================================================
    # tools -> agent
    # ============================================================

    graph.add_edge(
        "tools",
        "agent",
    )

    # ============================================================
    # finalize -> END
    # ============================================================

    graph.add_edge(
        "finalize",
        END,
    )

    return graph.compile()


# ================================================================
# RUN ONE PATIENT TURN
# ================================================================

def run_turn(
    db: Session,
    conversation: AIConversation,
    user_text: str,
) -> dict:

    # ------------------------------------------------------------
    # Persistent context
    # ------------------------------------------------------------

    context = _normalize_context(
        conversation.context
    )

    # ------------------------------------------------------------
    # Update context immediately from latest user message
    # ------------------------------------------------------------

    context = _extract_user_context(
        context,
        user_text,
    )

    # Save immediately so information survives this turn.

    conversation.context = context

    db.add(
        conversation
    )

    db.commit()

    # ------------------------------------------------------------
    # Capability context
    # ------------------------------------------------------------

    ctx = CapabilityContext(
        db=db,
        patient_id=conversation.patient_id,
        conversation_id=conversation.id,
        correlation_id=conversation.correlation_id,
    )

    # ------------------------------------------------------------
    # Build graph
    # ------------------------------------------------------------

    graph = build_graph(
        ctx
    )

    # ------------------------------------------------------------
    # Build LLM history
    # ------------------------------------------------------------

    history: list[BaseMessage] = []

    history.append(
        SystemMessage(
            SYSTEM_PROMPT
        )
    )

    # ------------------------------------------------------------
    # Strong state-management instructions
    # ------------------------------------------------------------

    state_instruction = f"""
IMPORTANT PATIENT CONVERSATION RULES:

You are handling an ongoing healthcare appointment conversation.

Current persistent conversation state:

{json.dumps(context, default=str, indent=2)}

NEVER ask the patient for information that is already present
in the persistent conversation state.

For example:

- If specialty is present, do NOT ask for specialty again.
- If city is present, do NOT ask for city again.
- If hospital_id/hospital_name is present, do NOT ask for hospital again.
- If doctor_id/doctor_name is present, do NOT ask for doctor again.
- If date is present, do NOT ask for date again.
- If time is present, do NOT ask for time again.

The patient may provide information in ANY ORDER.

Examples:

"doctors in Visakhapatnam"
-> use the city immediately.
Do not first ask for specialty.

"gynecologists in Amalapuram"
-> use both specialty and city immediately.

"City Care Hospital"
-> use the currently selected city and specialty and search/select
that hospital if it exists.

"26-09-2026"
-> store/use the date and continue to availability.

"11 am"
-> store/use the time and continue to availability.
-> If a date is already stored, interpret this time on that stored date;
   do NOT use today's date and do NOT invent a different date.

If the patient explicitly changes the specialty or city,
update that value and search again using the new value.

Do NOT silently change the patient's specialty.

For example:
gynecologist -> Gynecology.

Never change Gynecology to Cardiology unless the patient explicitly
requests Cardiology.

IMPORTANT:

A hospital must be considered selected only when it exists
in the database/search result.

A doctor must be considered part of a hospital only when the
database result confirms the doctor belongs to that hospital.

NEVER invent:

- hospitals
- doctors
- availability
- appointment slots

Availability must always come from check_availability.

BOOKING FLOW:

1. Understand whatever information the patient supplied.
2. Search hospitals/doctors using all information already known.
3. If one valid hospital is found, remember it.
4. Find doctors belonging to that hospital.
5. As soon as doctor + date + time are known, AUTOMATICALLY call
   check_availability. Never ask the patient to verify availability.
6. If the exact requested time is available, AUTOMATICALLY call
   create_appointment using the returned slot_id.
7. A complete request such as "Book Dr. Guna at 11 am on 26-09-2026"
   is already a booking instruction. Do NOT ask an additional
   yes/no confirmation before creating the appointment.
8. If the requested slot is unavailable, do NOT create an appointment.
   Return only real available alternatives and ask the patient to choose.
   If the patient then replies with one of those returned alternatives
   (for example "2 pm"), treat it as the selected slot and automatically
   call create_appointment without asking for another confirmation.
   Use the slot_id from the returned alternative/check_availability result;
   never construct a slot_id yourself.
9. Never invent a slot, doctor, hospital, appointment ID, or confirmation.
10. Never say the appointment is confirmed unless create_appointment
    actually succeeds.
11. After successful create_appointment, tell the patient the
    appointment is confirmed and include the actual booked date/time.

AUTOMATIC AVAILABILITY AND BOOKING RULE:

If the current conversation state contains a doctor, date and time,
do not stop at a conversational statement such as "I need to verify
availability." Use the capabilities immediately.

For example:

Patient: "Dr Guna at 11 am on 26-09-2026"

Required tool sequence:
    check_availability(doctor_id, "26-09-2026 11 am")
    -> if the exact slot exists
    create_appointment(slot_id, confirmed_by_patient=true)

The patient must never be asked to perform the availability check.

If check_availability returns an empty list, do not call
create_appointment. Show the real alternatives instead.

If create_appointment returns an error, do not claim that the
appointment was booked.

CURRENT STATE MUST BE PRESERVED ACROSS TURNS.

IMPORTANT TOOL LOOP RULE:

Do not repeatedly call the same capability when the previous
tool result already contains the required information.

After receiving a tool result:

- inspect the result
- use the result
- answer the patient if the requested task is complete
- only call another tool when additional real database information
  is actually required

Never call a tool simply because a previous tool was called.
"""

    history.append(
        SystemMessage(
            state_instruction
        )
    )

    # ------------------------------------------------------------
    # Previous conversation messages
    # ------------------------------------------------------------

    for message in (
        conversation.messages or []
    )[-12:]:

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
                    content
                )
            )

        elif role == "assistant":

            history.append(
                AIMessage(
                    content
                )
            )

    # ------------------------------------------------------------
    # Current user message
    # ------------------------------------------------------------

    history.append(
        HumanMessage(
            user_text
        )
    )

    # ------------------------------------------------------------
    # Execute graph
    # ------------------------------------------------------------

    result = graph.invoke(
        {
            "messages": history,
            "tool_rounds": 0,
        },
        {
            "recursion_limit": 20,
        },
    )

    # ------------------------------------------------------------
    # Final response
    # ------------------------------------------------------------

    final = result[
        "messages"
    ][-1]

    if isinstance(
        final.content,
        str,
    ):

        reply = final.content

    else:

        reply = str(
            final.content
        )

    # ------------------------------------------------------------
    # Capabilities used
    # ------------------------------------------------------------

    used = []

    for message in result.get(
        "messages",
        [],
    ):

        if isinstance(
            message,
            AIMessage,
        ):

            for tool_call in (
                message.tool_calls or []
            ):

                tool_name = tool_call.get(
                    "name"
                )

                if (
                    tool_name
                    and tool_name not in used
                ):

                    used.append(
                        tool_name
                    )

    # ------------------------------------------------------------
    # Update persistent context AFTER complete graph execution
    # ------------------------------------------------------------

    context = _update_context(
        context,
        result,
        user_text,
    )

    # ------------------------------------------------------------
    # Save messages
    # ------------------------------------------------------------

    conversation.messages = (
        conversation.messages or []
    ) + [
        {
            "role": "user",
            "content": user_text,
        },
        {
            "role": "assistant",
            "content": reply,
        },
    ]

    # ------------------------------------------------------------
    # Save context
    # ------------------------------------------------------------

    conversation.context = context

    db.add(
        conversation
    )

    db.commit()

    # ------------------------------------------------------------
    # Return
    # ------------------------------------------------------------

    return {
        "reply": reply,

        "capabilities_used": used,

        "correlation_id": (
            conversation.correlation_id
        ),

        "context": context,
    }