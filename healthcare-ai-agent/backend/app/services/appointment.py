"""Appointment service — booking orchestration.

Order matters and is fixed by the PRD:
  lock slot -> internal PENDING -> EHR create -> verify -> synchronize -> CONFIRMED
Nothing tells the patient "confirmed" before the verify step passes.
"""
import hashlib
import time
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..integrations.base import IntegrationError
from ..integrations.mock_ehr import get_connector
from ..integrations.verification import (
    open_reconciliation, synchronize_state, verify_external_appointment,
)
from ..models import (
    Appointment, AppointmentHistory, Doctor, ExternalIdMapping, Hospital,
    IntegrationOperation, Patient, Slot,
)
from ..services import scheduling
from ..services.audit import audit, metric
from ..workflows.engine import emit_event


class BookingError(Exception):
    pass


def build_idempotency_key(patient_id: str, slot_id: str) -> str:
    return hashlib.sha256(f"{patient_id}:{slot_id}".encode()).hexdigest()[:32]


def _log_integration(db: Session, appt: Appointment, operation: str, attempt: int,
                     outcome: str, error_class: str | None, ms: int) -> None:
    db.add(IntegrationOperation(
        hospital_id=appt.hospital_id, appointment_id=appt.id, operation=operation,
        attempt=attempt, outcome=outcome, error_class=error_class,
        external_id=appt.external_appointment_id, correlation_id=appt.correlation_id,
        duration_ms=ms,
    ))
    db.commit()


def _map_external_ids(db: Session, connector, patient: Patient, doctor: Doctor,
                      hospital: Hospital) -> tuple[str, str, str]:
    if not patient.external_patient_id:
        patient.external_patient_id = connector.upsert_patient({
            "name": patient.name, "phone": patient.phone,
            "dob": patient.date_of_birth.isoformat() if patient.date_of_birth else None,
        })
        db.add(ExternalIdMapping(
            hospital_id=hospital.id, entity_type="PATIENT",
            internal_id=patient.id, external_id=patient.external_patient_id))
    if not doctor.external_provider_id:
        doctor.external_provider_id = connector.find_provider(
            {"name": doctor.name, "specialty": doctor.specialty, "facility": hospital.name})
        db.add(ExternalIdMapping(
            hospital_id=hospital.id, entity_type="PROVIDER",
            internal_id=doctor.id, external_id=doctor.external_provider_id))
    if not hospital.external_facility_id:
        hospital.external_facility_id = f"FAC-{hospital.id[:6].upper()}"
        db.add(ExternalIdMapping(
            hospital_id=hospital.id, entity_type="FACILITY",
            internal_id=hospital.id, external_id=hospital.external_facility_id))
    db.commit()
    return patient.external_patient_id, doctor.external_provider_id, hospital.external_facility_id


def create_appointment(
    db: Session, *, patient_id: str, slot_id: str, correlation_id: str,
    actor: str = "AI_AGENT", appointment_type: str = "CONSULTATION",
) -> dict:
    """Returns {status, appointment_id, message, recovery?}."""
    idem = build_idempotency_key(patient_id, slot_id)

    # Idempotency: the same patient+slot never produces two appointments.
    existing = db.scalar(select(Appointment).where(Appointment.idempotency_key == idem))
    if existing and existing.status not in ("CANCELLED", "FAILED"):
        return {"status": existing.status, "appointment_id": existing.id,
                "message": "Existing appointment returned (idempotent replay).",
                "start_at": existing.start_at.isoformat()}

    patient = db.get(Patient, patient_id)
    if not patient:
        raise BookingError("PATIENT_NOT_FOUND")

    # 1. Lock + revalidate the slot inside a transaction.
    try:
        slot: Slot = scheduling.lock_slot_for_booking(db, slot_id)
    except ValueError as e:
        db.rollback()
        metric(db, "booking.slot_conflict")
        raise BookingError(str(e))

    doctor = db.get(Doctor, slot.doctor_id)
    hospital = db.get(Hospital, slot.hospital_id)

    slot.status = "BOOKED"
    appt = Appointment(
        hospital_id=hospital.id, doctor_id=doctor.id, patient_id=patient.id,
        slot_id=slot.id, start_at=slot.start_at, end_at=slot.end_at,
        appointment_type=appointment_type, status="PENDING",
        idempotency_key=idem, correlation_id=correlation_id,
    )
    db.add(appt)
    db.flush()
    db.add(AppointmentHistory(appointment_id=appt.id, to_status="PENDING",
                              note="slot locked and reserved"))
    db.commit()  # releases the row lock; slot is now BOOKED

    audit(db, actor=actor, actor_role="AI", action="APPOINTMENT_CREATE_STARTED",
          hospital_id=hospital.id, resource_type="appointment", resource_id=appt.id,
          correlation_id=correlation_id)

    if not hospital.ehr_enabled:
        appt.status = "CONFIRMED"
        db.commit()
        emit_event(db, "APPOINTMENT_BOOKED", appt)
        return {"status": "CONFIRMED", "appointment_id": appt.id,
                "start_at": appt.start_at.isoformat(),
                "message": "Appointment confirmed (no external system configured)."}

    # 2. External create, with classification + bounded retry.
    connector = get_connector(hospital)
    ext_patient, ext_provider, ext_facility = _map_external_ids(
        db, connector, patient, doctor, hospital)
    payload = {
        "patient_id": ext_patient, "provider_id": ext_provider,
        "facility_id": ext_facility, "start_at": appt.start_at.isoformat(),
        "end_at": appt.end_at.isoformat(), "type": appointment_type,
    }

    last_error = None
    for attempt in range(1, settings.EHR_MAX_RETRIES + 1):
        started = time.time()
        try:
            remote = connector.create_appointment(payload, idem)
            appt.external_appointment_id = remote.external_id
            appt.external_status = remote.status
            db.commit()
            _log_integration(db, appt, "CREATE", attempt, "SUCCESS", None,
                             int((time.time() - started) * 1000))
            last_error = None
            break
        except IntegrationError as e:
            last_error = e
            _log_integration(db, appt, "CREATE", attempt, e.error_class, e.error_class,
                             int((time.time() - started) * 1000))
            metric(db, "integration.failure", error_class=e.error_class)

            if e.error_class in ("TIMEOUT", "NETWORK", "UNKNOWN"):
                # Unknown outcome: never blindly retry a create. Ask the EHR first.
                appt.status = "SYNCHRONIZATION_PENDING"
                db.commit()
                if verify_external_appointment(db, connector, appt):
                    synchronize_state(db, appt)
                    emit_event(db, "APPOINTMENT_BOOKED", appt)
                    audit(db, actor=actor, action="APPOINTMENT_RECOVERED_AFTER_TIMEOUT",
                          hospital_id=hospital.id, resource_type="appointment",
                          resource_id=appt.id, correlation_id=correlation_id)
                    metric(db, "integration.recovered")
                    return {"status": "CONFIRMED", "appointment_id": appt.id,
                            "start_at": appt.start_at.isoformat(),
                            "recovery": "timeout_verified_no_duplicate",
                            "message": "Appointment confirmed after verifying the external record."}
                continue  # genuinely not created → safe to retry
            if e.error_class in ("OUTAGE", "RATE_LIMITED"):
                continue
            break  # VALIDATION / AUTH / CONFLICT → not retryable

    # 3. Retries exhausted: check external state once more, then escalate.
    if last_error is not None:
        if verify_external_appointment(db, connector, appt):
            synchronize_state(db, appt)
            emit_event(db, "APPOINTMENT_BOOKED", appt)
            return {"status": "CONFIRMED", "appointment_id": appt.id,
                    "start_at": appt.start_at.isoformat(),
                    "recovery": "verified_after_retries",
                    "message": "Appointment confirmed after verification."}
        open_reconciliation(db, appt, reason=f"EHR_{last_error.error_class}",
                            detail="retries exhausted; external state unresolved")
        scheduling.release_slot(db, slot.id)
        emit_event(db, "RECONCILIATION_REQUIRED", appt)
        audit(db, actor=actor, action="APPOINTMENT_ESCALATED", success=False,
              hospital_id=hospital.id, resource_type="appointment", resource_id=appt.id,
              correlation_id=correlation_id, detail=last_error.error_class)
        return {"status": "RECONCILIATION_REQUIRED", "appointment_id": appt.id,
                "message": "I could not confirm this booking with the hospital system. "
                           "A staff member will follow up — nothing was double-booked."}

    # 4. Happy path still verifies before confirming.
    if not verify_external_appointment(db, connector, appt):
        open_reconciliation(db, appt, reason="VERIFICATION_FAILED",
                            detail="EHR accepted the create but the record was not found")
        emit_event(db, "RECONCILIATION_REQUIRED", appt)
        return {"status": "RECONCILIATION_REQUIRED", "appointment_id": appt.id,
                "message": "The hospital system accepted the request but the record "
                           "could not be verified. Staff will follow up."}

    synchronize_state(db, appt)
    emit_event(db, "APPOINTMENT_BOOKED", appt)
    audit(db, actor=actor, actor_role="AI", action="APPOINTMENT_CONFIRMED",
          hospital_id=hospital.id, resource_type="appointment", resource_id=appt.id,
          correlation_id=correlation_id)
    metric(db, "booking.success")
    return {"status": "CONFIRMED", "appointment_id": appt.id,
            "doctor": doctor.name, "hospital": hospital.name,
            "start_at": appt.start_at.isoformat(),
            "message": "Appointment confirmed and verified in the hospital system."}


def cancel_appointment(db: Session, appointment_id: str, correlation_id: str,
                       actor: str = "AI_AGENT") -> dict:
    appt = db.get(Appointment, appointment_id)
    if not appt:
        raise BookingError("APPOINTMENT_NOT_FOUND")
    if appt.status == "CANCELLED":
        return {"status": "CANCELLED", "message": "Already cancelled."}

    hospital = db.get(Hospital, appt.hospital_id)
    if hospital.ehr_enabled and appt.external_appointment_id:
        connector = get_connector(hospital)
        try:
            connector.cancel_appointment(appt.external_appointment_id)
            _log_integration(db, appt, "CANCEL", 1, "SUCCESS", None, 0)
        except IntegrationError as e:
            _log_integration(db, appt, "CANCEL", 1, e.error_class, e.error_class, 0)
            open_reconciliation(db, appt, reason=f"CANCEL_{e.error_class}")
            return {"status": "RECONCILIATION_REQUIRED",
                    "message": "Cancellation could not be confirmed with the hospital system."}

    appt.status = "CANCELLED"
    db.add(AppointmentHistory(appointment_id=appt.id, to_status="CANCELLED"))
    scheduling.release_slot(db, appt.slot_id)
    db.commit()
    emit_event(db, "APPOINTMENT_CANCELLED", appt)
    audit(db, actor=actor, action="APPOINTMENT_CANCELLED", hospital_id=appt.hospital_id,
          resource_type="appointment", resource_id=appt.id, correlation_id=correlation_id)
    return {"status": "CANCELLED", "appointment_id": appt.id,
            "message": "Appointment cancelled and the slot released."}


def reschedule_appointment(db: Session, appointment_id: str, new_slot_id: str,
                           correlation_id: str, actor: str = "AI_AGENT") -> dict:
    appt = db.get(Appointment, appointment_id)
    if not appt:
        raise BookingError("APPOINTMENT_NOT_FOUND")
    try:
        new_slot = scheduling.lock_slot_for_booking(db, new_slot_id)
    except ValueError as e:
        db.rollback()
        raise BookingError(str(e))

    old_slot_id = appt.slot_id
    new_slot.status = "BOOKED"
    appt.slot_id = new_slot.id
    appt.start_at, appt.end_at = new_slot.start_at, new_slot.end_at
    appt.status = "PENDING"
    db.commit()

    hospital = db.get(Hospital, appt.hospital_id)
    if hospital.ehr_enabled and appt.external_appointment_id:
        connector = get_connector(hospital)
        try:
            connector.reschedule_appointment(
                appt.external_appointment_id, appt.start_at.isoformat())
            _log_integration(db, appt, "RESCHEDULE", 1, "SUCCESS", None, 0)
        except IntegrationError as e:
            _log_integration(db, appt, "RESCHEDULE", 1, e.error_class, e.error_class, 0)
            open_reconciliation(db, appt, reason=f"RESCHEDULE_{e.error_class}")
            return {"status": "RECONCILIATION_REQUIRED",
                    "message": "Reschedule could not be confirmed with the hospital system."}
        if not verify_external_appointment(db, get_connector(hospital), appt):
            open_reconciliation(db, appt, reason="RESCHEDULE_VERIFICATION_FAILED")
            return {"status": "RECONCILIATION_REQUIRED",
                    "message": "Reschedule could not be verified."}

    appt.status = "RESCHEDULED"
    db.add(AppointmentHistory(appointment_id=appt.id, to_status="RESCHEDULED"))
    scheduling.release_slot(db, old_slot_id)
    db.commit()
    emit_event(db, "APPOINTMENT_RESCHEDULED", appt)
    audit(db, actor=actor, action="APPOINTMENT_RESCHEDULED", hospital_id=appt.hospital_id,
          resource_type="appointment", resource_id=appt.id, correlation_id=correlation_id)
    return {"status": "RESCHEDULED", "appointment_id": appt.id,
            "start_at": appt.start_at.isoformat(),
            "message": "Appointment moved and verified."}
