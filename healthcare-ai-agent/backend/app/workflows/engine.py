"""Event-driven workflow engine (in-process, good enough for a prototype).

`emit_event` schedules work; `tick` runs anything due. Every execution has an
idempotency key so a replayed tick never sends the same reminder twice.
"""
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Appointment, Doctor, Notification, Patient, Questionnaire,
    QuestionnaireResponse, WorkflowExecution,
)


def _schedule(db: Session, workflow: str, appt: Appointment, run_at: datetime) -> None:
    key = f"{workflow}:{appt.id}"
    if db.scalar(select(WorkflowExecution).where(WorkflowExecution.idempotency_key == key)):
        return
    db.add(WorkflowExecution(
        hospital_id=appt.hospital_id, workflow=workflow, appointment_id=appt.id,
        run_at=run_at, idempotency_key=key, correlation_id=appt.correlation_id,
        history=[{"at": datetime.utcnow().isoformat(), "event": "scheduled"}],
    ))
    db.commit()


def emit_event(db: Session, event: str, appt: Appointment) -> None:
    if event == "APPOINTMENT_BOOKED":
        _schedule(db, "SEND_CONFIRMATION", appt, datetime.utcnow())
        _schedule(db, "ASSIGN_QUESTIONNAIRE", appt, datetime.utcnow())
        # Reminder 24h before the visit (or right away in a short demo).
        _schedule(db, "APPOINTMENT_REMINDER", appt, appt.start_at - timedelta(hours=24))
    elif event == "APPOINTMENT_CANCELLED":
        _schedule(db, "SEND_CANCELLATION", appt, datetime.utcnow())
    elif event == "APPOINTMENT_RESCHEDULED":
        _schedule(db, "SEND_RESCHEDULE", appt, datetime.utcnow())
    elif event == "QUESTIONNAIRE_COMPLETED":
        _schedule(db, "NOTIFY_DOCTOR_QUESTIONNAIRE", appt, datetime.utcnow())
    elif event == "RECONCILIATION_REQUIRED":
        _schedule(db, "ESCALATE_TO_HUMAN", appt, datetime.utcnow())


def notify(db: Session, *, hospital_id: str | None, recipient_type: str, recipient_id: str,
           template: str, body: str, correlation_id: str, channel: str = "SMS") -> None:
    key = f"{template}:{recipient_id}:{correlation_id}"
    if db.scalar(select(Notification).where(Notification.idempotency_key == key)):
        return
    db.add(Notification(
        hospital_id=hospital_id, recipient_type=recipient_type, recipient_id=recipient_id,
        template=template, body=body, channel=channel, idempotency_key=key,
        correlation_id=correlation_id,
    ))
    db.commit()
    # Real deployment: call Twilio / SendGrid here.


def _run_one(db: Session, wf: WorkflowExecution) -> None:
    appt = db.get(Appointment, wf.appointment_id)
    if not appt:
        raise RuntimeError("appointment missing")
    patient = db.get(Patient, appt.patient_id)
    doctor = db.get(Doctor, appt.doctor_id)
    when = appt.start_at.strftime("%d %b %Y at %I:%M %p")

    if wf.workflow == "SEND_CONFIRMATION":
        notify(db, hospital_id=appt.hospital_id, recipient_type="PATIENT",
               recipient_id=patient.id, template="APPOINTMENT_CONFIRMED",
               body=f"Your appointment with {doctor.name} is confirmed for {when}.",
               correlation_id=wf.correlation_id)
        notify(db, hospital_id=appt.hospital_id, recipient_type="DOCTOR",
               recipient_id=doctor.id, template="NEW_APPOINTMENT",
               body=f"New appointment: {patient.name} on {when}.",
               correlation_id=wf.correlation_id)

    elif wf.workflow == "ASSIGN_QUESTIONNAIRE":
        q = db.scalar(
            select(Questionnaire).where(
                Questionnaire.hospital_id == appt.hospital_id,
                Questionnaire.active.is_(True),
            ).order_by(Questionnaire.specialty.is_(None))
        )
        if q:
            exists = db.scalar(select(QuestionnaireResponse).where(
                QuestionnaireResponse.appointment_id == appt.id))
            if not exists:
                db.add(QuestionnaireResponse(
                    hospital_id=appt.hospital_id, questionnaire_id=q.id,
                    appointment_id=appt.id, patient_id=appt.patient_id))
                db.commit()
            notify(db, hospital_id=appt.hospital_id, recipient_type="PATIENT",
                   recipient_id=patient.id, template="QUESTIONNAIRE_ASSIGNED",
                   body="A few pre-visit questions are ready in your assistant.",
                   correlation_id=wf.correlation_id)

    elif wf.workflow == "APPOINTMENT_REMINDER":
        if appt.status in ("CONFIRMED", "RESCHEDULED"):
            notify(db, hospital_id=appt.hospital_id, recipient_type="PATIENT",
                   recipient_id=patient.id, template="APPOINTMENT_REMINDER",
                   body=f"Reminder: {doctor.name} on {when}.",
                   correlation_id=wf.correlation_id)

    elif wf.workflow == "SEND_CANCELLATION":
        notify(db, hospital_id=appt.hospital_id, recipient_type="PATIENT",
               recipient_id=patient.id, template="APPOINTMENT_CANCELLED",
               body=f"Your appointment on {when} is cancelled.",
               correlation_id=wf.correlation_id)

    elif wf.workflow == "SEND_RESCHEDULE":
        notify(db, hospital_id=appt.hospital_id, recipient_type="PATIENT",
               recipient_id=patient.id, template="APPOINTMENT_RESCHEDULED",
               body=f"Your appointment is now {when}.", correlation_id=wf.correlation_id)

    elif wf.workflow == "NOTIFY_DOCTOR_QUESTIONNAIRE":
        notify(db, hospital_id=appt.hospital_id, recipient_type="DOCTOR",
               recipient_id=doctor.id, template="QUESTIONNAIRE_COMPLETED",
               body=f"Pre-visit answers ready for {patient.name} ({when}).",
               correlation_id=wf.correlation_id)

    elif wf.workflow == "ESCALATE_TO_HUMAN":
        notify(db, hospital_id=appt.hospital_id, recipient_type="HOSPITAL",
               recipient_id=appt.hospital_id, template="RECONCILIATION_REQUIRED",
               body=f"Booking {appt.id} needs manual reconciliation.",
               correlation_id=wf.correlation_id, channel="DASHBOARD")


def tick(db: Session, limit: int = 20) -> int:
    """Run every due workflow. Called by the scheduler loop or POST /ops/tick."""
    due = db.scalars(
        select(WorkflowExecution)
        .where(WorkflowExecution.state.in_(("SCHEDULED", "FAILED")),
               WorkflowExecution.run_at <= datetime.utcnow(),
               WorkflowExecution.attempts < 3)
        .limit(limit)
    ).all()
    ran = 0
    for wf in due:
        wf.state, wf.attempts = "RUNNING", wf.attempts + 1
        db.commit()
        try:
            _run_one(db, wf)
            wf.state = "COMPLETED"
            wf.history = wf.history + [
                {"at": datetime.utcnow().isoformat(), "event": "completed"}]
            ran += 1
        except Exception as e:  # noqa: BLE001 - prototype: record and retry later
            wf.state = "FAILED"
            wf.last_error = str(e)[:200]
            wf.run_at = datetime.utcnow() + timedelta(seconds=30 * wf.attempts)
            wf.history = wf.history + [
                {"at": datetime.utcnow().isoformat(), "event": "failed", "error": str(e)[:120]}]
        db.commit()
    return ran
