"""
Capability layer.

The model chooses *what* to do; these functions decide whether it is allowed
and what actually happens. Each one: validate -> authorize -> execute -> audit.

The LLM never sees the database or the EHR.
"""

import re
import time
from datetime import datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

import dateparser
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    AIConversation,
    Appointment,
    Doctor,
    Hospital,
    Patient,
    Questionnaire,
    QuestionnaireResponse,
    CapabilityExecution,
)
from ..services import appointment as appointment_service
from ..services import scheduling
from ..services.audit import audit
from ..workflows.engine import emit_event, notify


# =============================================================
# TIMEZONE
# =============================================================

APP_TIMEZONE = ZoneInfo("Asia/Kolkata")


def _now() -> datetime:
    """
    Return current India-local time as a naive datetime.

    The scheduling database uses India-local naive datetimes.
    """
    return datetime.now(APP_TIMEZONE).replace(tzinfo=None)


# =============================================================
# CAPABILITY CONTEXT
# =============================================================


class CapabilityContext:
    """Everything a capability is allowed to assume about the caller."""

    def __init__(
        self,
        db: Session,
        patient_id: str,
        conversation_id: str,
        correlation_id: str,
    ):
        self.db = db
        self.patient_id = patient_id
        self.conversation_id = conversation_id
        self.correlation_id = correlation_id


# =============================================================
# CAPABILITY RECORDING
# =============================================================


def _record(
    ctx: CapabilityContext,
    name: str,
    request: dict,
    response: Any,
    success: bool,
    error: str | None,
    ms: int,
) -> None:

    ctx.db.add(
        CapabilityExecution(
            conversation_id=ctx.conversation_id,
            correlation_id=ctx.correlation_id,
            capability=name,
            request=request,
            response=(
                response
                if isinstance(response, dict)
                else {"result": response}
            ),
            success=success,
            error=error,
            duration_ms=ms,
        )
    )

    ctx.db.commit()


def instrument(name: str) -> Callable:
    """
    Wrap a capability so every call is validated,
    timed and audited.
    """

    def deco(fn):

        def wrapper(
            ctx: CapabilityContext,
            **kwargs,
        ):

            started = time.time()

            try:

                result = fn(
                    ctx,
                    **kwargs,
                )

                _record(
                    ctx,
                    name,
                    kwargs,
                    result,
                    True,
                    None,
                    int(
                        (time.time() - started)
                        * 1000
                    ),
                )

                return result

            except Exception as e:  # noqa: BLE001

                _record(
                    ctx,
                    name,
                    kwargs,
                    {},
                    False,
                    str(e)[:200],
                    int(
                        (time.time() - started)
                        * 1000
                    ),
                )

                return {
                    "error": str(e)[:200]
                }

        wrapper.__name__ = name

        return wrapper

    return deco


# =============================================================
# DATE / TIME PARSING
# =============================================================


def _is_time_only(text: str | None) -> bool:
    """
    Return True when the patient supplied only a time.

    Examples:
        11 am
        11:30 am
        3 pm
        15:00

    A time-only request must use the appointment date already
    stored in the conversation.
    """

    if not text:
        return False

    value = text.strip().lower()

    # ---------------------------------------------------------
    # Date is present -> NOT time-only
    # ---------------------------------------------------------

    if re.search(
        r"\b\d{1,2}[-/]\d{1,2}[-/]\d{2,4}\b",
        value,
    ):
        return False

    if re.search(
        r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|"
        r"apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
        r"aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|"
        r"nov(?:ember)?|dec(?:ember)?)\b",
        value,
    ):
        return False

    # ---------------------------------------------------------
    # AM/PM or 24-hour HH:MM
    # ---------------------------------------------------------

    return bool(
        re.fullmatch(
            r"\s*(?:"
            r"\d{1,2}(?::\d{2})?\s*(?:am|pm)"
            r"|"
            r"\d{1,2}:\d{2}"
            r")\s*",
            value,
        )
    )


def _parse_when(
    text: str | None,
    reference_date: datetime | None = None,
):
    """
    Convert patient's date/time request into a datetime window.

    Time-only example:

        Previous turn:
            26-09-2026

        Current turn:
            3 pm

        Result:
            2026-09-26 15:00
            to
            2026-09-26 15:01

    The one-minute window means an exact-time request matches
    only a slot that STARTS at that requested time.
    """

    now = _now()

    # ---------------------------------------------------------
    # Empty request
    # ---------------------------------------------------------

    if not text:

        if reference_date is not None:

            day = reference_date.replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )

            return (
                day,
                day + timedelta(days=1),
            )

        return (
            now,
            now + timedelta(days=7),
        )

    text = text.strip()
    text_lower = text.lower()

    # =========================================================
    # TIME ONLY
    # =========================================================

    if _is_time_only(text):

        parsed_time = dateparser.parse(
            text,
            settings={
                "RETURN_AS_TIMEZONE_AWARE": False,
            },
        )

        if not parsed_time:

            return (
                now,
                now + timedelta(days=7),
            )

        # -----------------------------------------------------
        # No stored date
        # -----------------------------------------------------

        if reference_date is None:

            requested_time = parsed_time.replace(
                second=0,
                microsecond=0,
            )

        # -----------------------------------------------------
        # Stored appointment date
        # -----------------------------------------------------

        else:

            requested_time = datetime.combine(
                reference_date.date(),
                parsed_time.time().replace(
                    second=0,
                    microsecond=0,
                ),
            )

        return (
            requested_time,
            requested_time + timedelta(minutes=1),
        )

    # =========================================================
    # DATE / DATE + TIME
    # =========================================================

    parsed = dateparser.parse(
        text,
        settings={
            "PREFER_DATES_FROM": "future",
            "DATE_ORDER": "DMY",
            "RETURN_AS_TIMEZONE_AWARE": False,
        },
    )

    if not parsed:

        return (
            now,
            now + timedelta(days=7),
        )

    # =========================================================
    # YEAR VALIDATION
    # =========================================================

    if parsed.year < now.year:

        return (
            now,
            now + timedelta(days=7),
        )

    if parsed.year > now.year + 10:

        return (
            now,
            now + timedelta(days=7),
        )

    # =========================================================
    # WEEK REQUEST
    # =========================================================

    if "week" in text_lower:

        start = max(now, parsed)

        return (
            start,
            start + timedelta(days=7),
        )

    # =========================================================
    # MONTH REQUEST
    # =========================================================

    if "month" in text_lower:

        start = max(now, parsed)

        return (
            start,
            start + timedelta(days=30),
        )

    # =========================================================
    # DETECT SPECIFIC TIME
    # =========================================================

    has_time = bool(
        re.search(
            r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b",
            text_lower,
        )
        or re.search(
            r"\b\d{1,2}:\d{2}\b",
            text_lower,
        )
    )

    # =========================================================
    # DATE + TIME
    # =========================================================

    if has_time:

        requested_time = parsed.replace(
            second=0,
            microsecond=0,
        )

        return (
            requested_time,
            requested_time + timedelta(minutes=1),
        )

    # =========================================================
    # DATE ONLY
    # =========================================================

    day = parsed.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )

    return (
        day,
        day + timedelta(days=1),
    )


# =============================================================
# DISCOVERY
# =============================================================


@instrument("search_hospitals")
def search_hospitals(
    ctx,
    city: str | None = None,
    specialty: str | None = None,
):
    """
    Search real hospitals from the database.
    """

    hospitals = scheduling.search_hospitals(
        ctx.db,
        city=city,
        specialty=specialty,
    )

    return {
        "hospitals": hospitals
    }


@instrument("search_doctors")
def search_doctors(
    ctx,
    specialty: str | None = None,
    city: str | None = None,
    hospital_id: str | None = None,
):
    """
    Search real doctors from the database.
    """

    if (
        not specialty
        and not city
        and not hospital_id
    ):

        return {
            "error": (
                "Ask the patient which specialty or city "
                "they need first."
            )
        }

    doctors = scheduling.search_doctors(
        ctx.db,
        specialty=specialty,
        city=city,
        hospital_id=hospital_id,
    )

    if not doctors:

        return {
            "doctors": [],
            "count": 0,
            "message": (
                "No active doctor was found matching "
                "the requested specialty and location."
            ),
        }

    return {
        "doctors": doctors,
        "count": len(doctors),
    }


# =============================================================
# AVAILABILITY
# =============================================================


@instrument("check_availability")
def check_availability(
    ctx,
    doctor_id: str,
    when: str | None = None,
):
    """
    Check REAL available appointment slots.

    Date + time:
        26-09-2026 11 am
        -> 2026-09-26 11:00

    Separate messages:
        26-09-2026
        11 am
        -> 2026-09-26 11:00

    Never invents appointment slots.
    """

    # =========================================================
    # 1. GET DOCTOR
    # =========================================================

    doctor = ctx.db.get(
        Doctor,
        doctor_id,
    )

    if not doctor:
        return {
            "slots": [],
            "count": 0,
            "requested_time_available": False,
            "day_has_other_available_slots": False,
            "alternative_count": 0,
            "alternatives": [],
            "note": "Doctor not found.",
        }

    # =========================================================
    # 2. DOCTOR MUST BE ACTIVE
    # =========================================================

    if getattr(doctor, "status", None) != "ACTIVE":
        return {
            "slots": [],
            "count": 0,
            "requested_time_available": False,
            "day_has_other_available_slots": False,
            "alternative_count": 0,
            "alternatives": [],
            "note": "Doctor is not currently active.",
        }

    # =========================================================
    # 3. GET CONVERSATION
    # =========================================================

    conversation = ctx.db.get(
        AIConversation,
        ctx.conversation_id,
    )

    context = {}

    if conversation and conversation.context:
        context = dict(conversation.context)

    # =========================================================
    # 4. GET STORED DATE
    # =========================================================

    reference_date = None

    stored_date = (
        context.get("appointment_date")
        or context.get("date")
    )

    if stored_date:
        try:
            reference_date = datetime.fromisoformat(
                str(stored_date)
            )

            reference_date = reference_date.replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )

        except (
            ValueError,
            TypeError,
        ):
            reference_date = None

    # =========================================================
    # 5. PARSE CURRENT REQUEST
    # =========================================================

    start, end = _parse_when(
        when,
        reference_date=reference_date,
    )

    print(
        "CHECK AVAILABILITY:",
        "doctor_id =", doctor_id,
        "when =", when,
        "reference_date =", reference_date,
        "start =", start,
        "end =", end,
    )

    # =========================================================
    # 6. SAVE DATE WHEN PATIENT PROVIDES DATE
    # =========================================================

    if when and not _is_time_only(
        when.strip()
    ):

        # Only store specific one-day requests.
        if end - start <= timedelta(days=1):

            selected_date = start.replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )

            context["appointment_date"] = (
                selected_date.isoformat()
            )

            context["date"] = (
                selected_date.strftime(
                    "%Y-%m-%d"
                )
            )

            if conversation:
                conversation.context = context
                ctx.db.commit()

            reference_date = selected_date

    # =========================================================
    # 7. ASK SCHEDULING SERVICE
    # =========================================================

    slots = scheduling.check_availability(
        ctx.db,
        doctor_id,
        start,
        end,
    )

    print(
        "EXACT AVAILABILITY:",
        slots,
    )

    # =========================================================
    # 8. EXACT TIME AVAILABLE
    # =========================================================

    if slots:

        result = []

        for slot in slots:

            slot_start = datetime.fromisoformat(
                slot["start_at"]
            )

            result.append(
                {
                    "slot_id": slot["slot_id"],
                    "when": slot_start.strftime(
                        "%a %d %b, %I:%M %p"
                    ),
                    "start_at": slot["start_at"],
                }
            )

        return {
            "slots": result,
            "count": len(result),

            "requested_start":
                start.isoformat(),

            "requested_end":
                end.isoformat(),

            "requested_time_available":
                True,

            "day_has_other_available_slots":
                True,

            "alternative_count": 0,
            "alternatives": [],

            "note": (
                "The requested time is available. "
                "Use the returned slot_id to book "
                "the appointment automatically."
            ),
        }

    # =========================================================
    # 9. CHECK WHETHER THIS WAS AN EXACT TIME REQUEST
    # =========================================================

    is_exact_time_request = False

    if when:

        value = when.strip().lower()

        is_exact_time_request = (
            _is_time_only(value)
            or bool(
                re.search(
                    r"\b\d{1,2}"
                    r"(?::\d{2})?"
                    r"\s*(?:am|pm)\b",
                    value,
                )
            )
            or bool(
                re.search(
                    r"\b\d{1,2}:\d{2}\b",
                    value,
                )
            )
        )

    # =========================================================
    # 10. FIND REAL ALTERNATIVES ON SAME DATE
    # =========================================================

    alternatives = []

    if is_exact_time_request:

        requested_day_start = start.replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )

        requested_day_end = (
            requested_day_start
            + timedelta(days=1)
        )

        print(
            "SEARCHING WHOLE DAY:",
            requested_day_start,
            "TO",
            requested_day_end,
        )

        all_day_slots = (
            scheduling.check_availability(
                ctx.db,
                doctor_id,
                requested_day_start,
                requested_day_end,
            )
        )

        print(
            "WHOLE DAY SLOTS:",
            all_day_slots,
        )

        requested_minutes = (
            start.hour * 60
            + start.minute
        )

        for slot in all_day_slots:

            slot_start = datetime.fromisoformat(
                slot["start_at"]
            )

            # Only requested date
            if (
                slot_start.date()
                != requested_day_start.date()
            ):
                continue

            slot_minutes = (
                slot_start.hour * 60
                + slot_start.minute
            )

            # Don't return requested unavailable time
            if slot_minutes == requested_minutes:
                continue

            alternatives.append(
                {
                    "slot_id": slot["slot_id"],
                    "when": slot_start.strftime(
                        "%a %d %b, %I:%M %p"
                    ),
                    "start_at": slot["start_at"],
                }
            )

        alternatives = alternatives[:10]

    # =========================================================
    # 11. RESULT
    # =========================================================

    alternative_count = len(
        alternatives
    )

    if alternative_count > 0:

        return {
            "slots": [],
            "count": 0,

            "requested_start":
                start.isoformat(),

            "requested_end":
                end.isoformat(),

            "requested_time_available":
                False,

            "day_has_other_available_slots":
                True,

            "alternative_count":
                alternative_count,

            "alternatives":
                alternatives,

            "note": (
                "The requested time is unavailable, "
                "but these real available slots exist "
                "on the same date. Offer ONLY these "
                "alternatives."
            ),
        }

    # =========================================================
    # 12. NO ALTERNATIVES
    # =========================================================

    return {
        "slots": [],
        "count": 0,

        "requested_start":
            start.isoformat(),

        "requested_end":
            end.isoformat(),

        "requested_time_available":
            False,

        "day_has_other_available_slots":
            False,

        "alternative_count":
            0,

        "alternatives":
            [],

        "note": (
            "The requested time is unavailable and "
            "no other AVAILABLE slots were found "
            "on the requested date. Do not invent "
            "a time. Ask whether the patient wants "
            "another date."
        ),
    }


# =============================================================
# PATIENT
# =============================================================


@instrument("lookup_patient")
def lookup_patient(
    ctx,
):

    patient = ctx.db.get(
        Patient,
        ctx.patient_id,
    )

    if not patient:

        return {
            "error": "Patient not found"
        }

    return {
        "patient_id": patient.id,
        "name": patient.name,
        "communication_preference": (
            patient.communication_preference
        ),
    }


@instrument("get_appointment")
def get_appointment(
    ctx,
    appointment_id: str | None = None,
):

    query = select(
        Appointment
    ).where(
        Appointment.patient_id
        == ctx.patient_id
    )

    if appointment_id:

        query = query.where(
            Appointment.id
            == appointment_id
        )

    appointments = ctx.db.scalars(
        query.order_by(
            Appointment.start_at
        )
    ).all()

    return {
        "appointments": [
            {
                "appointment_id": appointment.id,
                "status": appointment.status,
                "when": appointment.start_at.strftime(
                    "%a %d %b, %I:%M %p"
                ),
                "doctor": (
                    ctx.db.get(
                        Doctor,
                        appointment.doctor_id,
                    ).name
                ),
                "hospital": (
                    ctx.db.get(
                        Hospital,
                        appointment.hospital_id,
                    ).name
                ),
            }
            for appointment in appointments
            if appointment.status
            not in ("FAILED",)
        ]
    }


@instrument("get_context")
def get_context(
    ctx,
):

    conversation = ctx.db.get(
        AIConversation,
        ctx.conversation_id,
    )

    return {
        "context": (
            conversation.context
            if conversation
            else {}
        )
    }


@instrument("update_preferences")
def update_preferences(
    ctx,
    key: str,
    value: str,
):

    patient = ctx.db.get(
        Patient,
        ctx.patient_id,
    )

    if not patient:

        return {
            "error": "Patient not found"
        }

    if key == "communication_preference":

        patient.communication_preference = value

        ctx.db.commit()

    return {
        "updated": {
            key: value
        }
    }


# =============================================================
# BOOKING
# =============================================================


@instrument("create_appointment")
def create_appointment(
    ctx,
    slot_id: str,
    confirmed_by_patient: bool | str = True,
):
    """
    Create an appointment for a slot returned by
    check_availability.

    A complete patient request containing doctor + date + time
    is treated as the booking instruction.

    No redundant confirmation is required.

    IMPORTANT:
    This function never treats a failed or reconciliation-only
    appointment as confirmed.
    """

    # =========================================================
    # NORMALIZE CONFIRMATION VALUE
    # =========================================================

    if isinstance(
        confirmed_by_patient,
        str,
    ):

        confirmed_value = (
            confirmed_by_patient
            .strip()
            .lower()
        )

        if confirmed_value in {
            "false",
            "0",
            "no",
            "none",
            "",
        }:

            return {
                "error": (
                    "The booking instruction was not confirmed. "
                    "Use a slot returned by check_availability "
                    "for a complete patient appointment request."
                )
            }

    elif confirmed_by_patient is False:

        return {
            "error": (
                "The booking instruction was not confirmed. "
                "Use a slot returned by check_availability "
                "for a complete patient appointment request."
            )
        }

    # =========================================================
    # CREATE THROUGH APPOINTMENT SERVICE
    # =========================================================

    try:

        result = appointment_service.create_appointment(
            ctx.db,
            patient_id=ctx.patient_id,
            slot_id=slot_id,
            correlation_id=ctx.correlation_id,
        )

        # =====================================================
        # ONLY CONFIRMED IS CONFIRMED
        # =====================================================

        if (
            isinstance(result, dict)
            and result.get("status")
            == "CONFIRMED"
        ):

            result["booking_confirmed"] = True

        else:

            if isinstance(result, dict):

                result["booking_confirmed"] = False

        return result

    except appointment_service.BookingError as e:

        return {
            "error": str(e),
            "booking_confirmed": False,
            "message": (
                "That slot is no longer free. "
                "Do not claim confirmation. "
                "Offer another real available slot."
            ),
        }


@instrument("reschedule_appointment")
def reschedule_appointment(
    ctx,
    appointment_id: str,
    new_slot_id: str,
):

    appointment = ctx.db.get(
        Appointment,
        appointment_id,
    )

    if (
        not appointment
        or appointment.patient_id
        != ctx.patient_id
    ):

        return {
            "error": "NOT_YOUR_APPOINTMENT"
        }

    try:

        return appointment_service.reschedule_appointment(
            ctx.db,
            appointment_id,
            new_slot_id,
            ctx.correlation_id,
        )

    except appointment_service.BookingError as e:

        return {
            "error": str(e)
        }


@instrument("cancel_appointment")
def cancel_appointment(
    ctx,
    appointment_id: str,
):

    appointment = ctx.db.get(
        Appointment,
        appointment_id,
    )

    if (
        not appointment
        or appointment.patient_id
        != ctx.patient_id
    ):

        return {
            "error": "NOT_YOUR_APPOINTMENT"
        }

    return appointment_service.cancel_appointment(
        ctx.db,
        appointment_id,
        ctx.correlation_id,
    )


# =============================================================
# QUESTIONNAIRES
# =============================================================


@instrument("get_questionnaire")
def get_questionnaire(
    ctx,
    appointment_id: str,
):

    response = ctx.db.scalar(
        select(
            QuestionnaireResponse
        ).where(
            QuestionnaireResponse.appointment_id
            == appointment_id,
            QuestionnaireResponse.patient_id
            == ctx.patient_id,
        )
    )

    if not response:

        return {
            "questions": [],
            "note": "No questionnaire assigned.",
        }

    questionnaire = ctx.db.get(
        Questionnaire,
        response.questionnaire_id,
    )

    if not questionnaire:

        return {
            "questions": [],
            "note": "Questionnaire not found.",
        }

    return {
        "response_id": response.id,
        "name": questionnaire.name,
        "questions": questionnaire.questions,
        "status": response.status,
        "note": (
            "Ask only these approved questions. "
            "Do not add clinical questions."
        ),
    }


@instrument("submit_questionnaire")
def submit_questionnaire(
    ctx,
    response_id: str,
    answers: dict,
):

    response = ctx.db.get(
        QuestionnaireResponse,
        response_id,
    )

    if (
        not response
        or response.patient_id
        != ctx.patient_id
    ):

        return {
            "error": "NOT_YOUR_QUESTIONNAIRE"
        }

    merged = dict(
        response.answers or {}
    )

    merged.update(
        answers or {}
    )

    response.answers = merged

    questionnaire = ctx.db.get(
        Questionnaire,
        response.questionnaire_id,
    )

    if not questionnaire:

        return {
            "error": "QUESTIONNAIRE_NOT_FOUND"
        }

    if all(
        item["key"] in merged
        for item in questionnaire.questions
    ):

        response.status = "COMPLETED"

        response.completed_at = _now()

        emit_event(
            ctx.db,
            "QUESTIONNAIRE_COMPLETED",
            ctx.db.get(
                Appointment,
                response.appointment_id,
            ),
        )

    ctx.db.commit()

    audit(
        ctx.db,
        actor="AI_AGENT",
        actor_role="AI",
        action="QUESTIONNAIRE_RESPONSE_SAVED",
        hospital_id=response.hospital_id,
        resource_type="questionnaire_response",
        resource_id=response.id,
        correlation_id=ctx.correlation_id,
        detail=(
            "answers stored "
            "(content not logged)"
        ),
    )

    return {
        "status": response.status,
        "saved_keys": list(
            answers or {}
        ),
    }


# =============================================================
# NOTIFICATION / WORKFLOW
# =============================================================


@instrument("send_notification")
def send_notification(
    ctx,
    template: str,
    body: str,
):

    patient = ctx.db.get(
        Patient,
        ctx.patient_id,
    )

    if not patient:

        return {
            "error": "Patient not found"
        }

    notify(
        ctx.db,
        hospital_id=None,
        recipient_type="PATIENT",
        recipient_id=patient.id,
        template=template,
        body=body,
        correlation_id=ctx.correlation_id,
    )

    return {
        "sent": True
    }


@instrument("start_workflow")
def start_workflow(
    ctx,
    workflow: str,
    appointment_id: str,
):

    appointment = ctx.db.get(
        Appointment,
        appointment_id,
    )

    if (
        not appointment
        or appointment.patient_id
        != ctx.patient_id
    ):

        return {
            "error": "NOT_YOUR_APPOINTMENT"
        }

    emit_event(
        ctx.db,
        workflow,
        appointment,
    )

    return {
        "started": workflow
    }


# =============================================================
# VERIFICATION / ESCALATION
# =============================================================


@instrument("verify_external_appointment")
def verify_external_appointment(
    ctx,
    appointment_id: str,
):

    from ..integrations.mock_ehr import (
        get_connector,
    )

    from ..integrations.verification import (
        verify_external_appointment as verify,
    )

    appointment = ctx.db.get(
        Appointment,
        appointment_id,
    )

    if (
        not appointment
        or appointment.patient_id
        != ctx.patient_id
    ):

        return {
            "error": "NOT_YOUR_APPOINTMENT"
        }

    ok = verify(
        ctx.db,
        get_connector(),
        appointment,
    )

    return {
        "verified": ok,
        "external_id": (
            appointment.external_appointment_id
        ),
    }


@instrument("synchronize_state")
def synchronize_state(
    ctx,
    appointment_id: str,
):

    from ..integrations.verification import (
        synchronize_state as sync,
    )

    appointment = ctx.db.get(
        Appointment,
        appointment_id,
    )

    if (
        not appointment
        or appointment.patient_id
        != ctx.patient_id
    ):

        return {
            "error": "NOT_YOUR_APPOINTMENT"
        }

    sync(
        ctx.db,
        appointment,
    )

    return {
        "status": appointment.status
    }


@instrument("transfer_to_human")
def transfer_to_human(
    ctx,
    reason: str,
):

    audit(
        ctx.db,
        actor="AI_AGENT",
        actor_role="AI",
        action="HUMAN_ESCALATION",
        correlation_id=ctx.correlation_id,
        detail=reason[:200],
    )

    return {
        "escalated": True,
        "message": (
            "I'm handing this to a staff member "
            "who will call you back."
        ),
    }


# =============================================================
# REGISTRY
# =============================================================


REGISTRY = {
    f.__name__: f
    for f in [
        search_hospitals,
        search_doctors,
        check_availability,
        lookup_patient,
        get_appointment,
        get_context,
        update_preferences,
        create_appointment,
        reschedule_appointment,
        cancel_appointment,
        get_questionnaire,
        submit_questionnaire,
        send_notification,
        start_workflow,
        verify_external_appointment,
        synchronize_state,
        transfer_to_human,
    ]
}