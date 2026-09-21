"""
Appointment service — booking orchestration.

Order matters:

    lock slot
        ↓
    internal PENDING
        ↓
    EHR create
        ↓
    verify
        ↓
    synchronize
        ↓
    CONFIRMED

The patient is never told that an appointment is confirmed
until the verification step succeeds.
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
    open_reconciliation,
    synchronize_state,
    verify_external_appointment,
)
from ..models import (
    Appointment,
    AppointmentHistory,
    Doctor,
    ExternalIdMapping,
    Hospital,
    IntegrationOperation,
    Patient,
    Slot,
)
from ..services import scheduling
from ..services.audit import audit, metric
from ..workflows.engine import emit_event


# =========================================================
# ERRORS
# =========================================================

class BookingError(Exception):
    pass


# =========================================================
# IDEMPOTENCY
# =========================================================

def build_idempotency_key(
    patient_id: str,
    slot_id: str,
) -> str:
    """
    Create a unique key for patient + slot.

    The same patient cannot accidentally create
    multiple appointments for the same slot.
    """

    return hashlib.sha256(
        f"{patient_id}:{slot_id}".encode()
    ).hexdigest()[:32]


# =========================================================
# INTEGRATION LOGGING
# =========================================================

def _log_integration(
    db: Session,
    appt: Appointment,
    operation: str,
    attempt: int,
    outcome: str,
    error_class: str | None,
    ms: int,
) -> None:
    """
    Store every external EHR operation.
    """

    db.add(
        IntegrationOperation(
            hospital_id=appt.hospital_id,
            appointment_id=appt.id,
            operation=operation,
            attempt=attempt,
            outcome=outcome,
            error_class=error_class,
            external_id=appt.external_appointment_id,
            correlation_id=appt.correlation_id,
            duration_ms=ms,
        )
    )

    db.commit()


# =========================================================
# EXTERNAL ID MAPPING
# =========================================================

def _map_external_ids(
    db: Session,
    connector,
    patient: Patient,
    doctor: Doctor,
    hospital: Hospital,
) -> tuple[str, str, str]:
    """
    Make sure patient, doctor and hospital have
    their corresponding EHR identifiers.
    """

    # -----------------------------------------------------
    # PATIENT
    # -----------------------------------------------------

    if not patient.external_patient_id:

        patient.external_patient_id = connector.upsert_patient(
            {
                "name": patient.name,
                "phone": patient.phone,
                "dob": (
                    patient.date_of_birth.isoformat()
                    if patient.date_of_birth
                    else None
                ),
            }
        )

        db.add(
            ExternalIdMapping(
                hospital_id=hospital.id,
                entity_type="PATIENT",
                internal_id=patient.id,
                external_id=patient.external_patient_id,
            )
        )

    # -----------------------------------------------------
    # DOCTOR
    # -----------------------------------------------------

    if not doctor.external_provider_id:

        doctor.external_provider_id = connector.find_provider(
            {
                "name": doctor.name,
                "specialty": doctor.specialty,
                "facility": hospital.name,
            }
        )

        db.add(
            ExternalIdMapping(
                hospital_id=hospital.id,
                entity_type="PROVIDER",
                internal_id=doctor.id,
                external_id=doctor.external_provider_id,
            )
        )

    # -----------------------------------------------------
    # HOSPITAL
    # -----------------------------------------------------

    if not hospital.external_facility_id:

        hospital.external_facility_id = (
            f"FAC-{hospital.id[:6].upper()}"
        )

        db.add(
            ExternalIdMapping(
                hospital_id=hospital.id,
                entity_type="FACILITY",
                internal_id=hospital.id,
                external_id=hospital.external_facility_id,
            )
        )

    db.commit()

    return (
        patient.external_patient_id,
        doctor.external_provider_id,
        hospital.external_facility_id,
    )


# =========================================================
# VALIDATE SLOT
# =========================================================

def _validate_slot_relationships(
    db: Session,
    slot: Slot,
) -> tuple[Doctor, Hospital]:
    """
    Verify that:

        Slot
          ↓
        Doctor
          ↓
        Hospital

    all belong together.
    """

    doctor = db.get(
        Doctor,
        slot.doctor_id,
    )

    if not doctor:
        raise BookingError("DOCTOR_NOT_FOUND")

    hospital = db.get(
        Hospital,
        slot.hospital_id,
    )

    if not hospital:
        raise BookingError("HOSPITAL_NOT_FOUND")

    # Very important for multi-hospital isolation.
    if doctor.hospital_id != hospital.id:
        raise BookingError("SLOT_HOSPITAL_MISMATCH")

    if doctor.status != "ACTIVE":
        raise BookingError("DOCTOR_NOT_ACTIVE")

    if hospital.status != "APPROVED":
        raise BookingError("HOSPITAL_NOT_APPROVED")

    return doctor, hospital


# =========================================================
# CREATE APPOINTMENT
# =========================================================

def create_appointment(
    db: Session,
    *,
    patient_id: str,
    slot_id: str,
    correlation_id: str,
    actor: str = "AI_AGENT",
    appointment_type: str = "CONSULTATION",
) -> dict:
    """
    Create an appointment using a real scheduling slot.

    Flow:

        1. Check idempotency
        2. Find patient
        3. Lock real slot
        4. Validate doctor/hospital
        5. Reserve slot
        6. Create PENDING appointment
        7. Call EHR
        8. Verify EHR
        9. Synchronize
        10. CONFIRMED
    """

    # -----------------------------------------------------
    # 1. IDEMPOTENCY
    # -----------------------------------------------------

    idem = build_idempotency_key(
        patient_id,
        slot_id,
    )

    existing = db.scalar(
        select(Appointment).where(
            Appointment.idempotency_key == idem
        )
    )

    if existing and existing.status not in (
        "CANCELLED",
        "FAILED",
    ):
        return {
            "status": existing.status,
            "appointment_id": existing.id,
            "message": (
                "Existing appointment returned "
                "(idempotent replay)."
            ),
            "start_at": existing.start_at.isoformat(),
        }

    # -----------------------------------------------------
    # 2. PATIENT
    # -----------------------------------------------------

    patient = db.get(
        Patient,
        patient_id,
    )

    if not patient:
        raise BookingError("PATIENT_NOT_FOUND")

    # -----------------------------------------------------
    # 3. LOCK REAL SLOT
    # -----------------------------------------------------

    try:

        slot: Slot = scheduling.lock_slot_for_booking(
            db,
            slot_id,
        )

    except ValueError as e:

        db.rollback()

        metric(
            db,
            "booking.slot_conflict",
        )

        raise BookingError(str(e))

    # -----------------------------------------------------
    # 4. VALIDATE DOCTOR + HOSPITAL
    # -----------------------------------------------------

    try:

        doctor, hospital = _validate_slot_relationships(
            db,
            slot,
        )

    except BookingError:

        db.rollback()

        raise

    # -----------------------------------------------------
    # 5. RESERVE SLOT
    # -----------------------------------------------------

    slot.status = "BOOKED"

    # -----------------------------------------------------
    # 6. CREATE INTERNAL APPOINTMENT
    # -----------------------------------------------------

    appt = Appointment(
        hospital_id=hospital.id,
        doctor_id=doctor.id,
        patient_id=patient.id,
        slot_id=slot.id,
        start_at=slot.start_at,
        end_at=slot.end_at,
        appointment_type=appointment_type,
        status="PENDING",
        idempotency_key=idem,
        correlation_id=correlation_id,
    )

    db.add(appt)

    db.flush()

    db.add(
        AppointmentHistory(
            appointment_id=appt.id,
            to_status="PENDING",
            note="slot locked and reserved",
        )
    )

    # Commit so that the slot remains BOOKED
    # after the row lock is released.
    db.commit()

    # -----------------------------------------------------
    # AUDIT
    # -----------------------------------------------------

    audit(
        db,
        actor=actor,
        actor_role="AI",
        action="APPOINTMENT_CREATE_STARTED",
        hospital_id=hospital.id,
        resource_type="appointment",
        resource_id=appt.id,
        correlation_id=correlation_id,
    )

    # -----------------------------------------------------
    # 7. NO EHR CONFIGURED
    # -----------------------------------------------------

    if not hospital.ehr_enabled:

        appt.status = "CONFIRMED"

        db.add(
            AppointmentHistory(
                appointment_id=appt.id,
                to_status="CONFIRMED",
                note="No external EHR configured",
            )
        )

        db.commit()

        emit_event(
            db,
            "APPOINTMENT_BOOKED",
            appt,
        )

        metric(
            db,
            "booking.success",
        )

        return {
            "status": "CONFIRMED",
            "appointment_id": appt.id,
            "doctor": doctor.name,
            "hospital": hospital.name,
            "start_at": appt.start_at.isoformat(),
            "message": (
                "Appointment confirmed "
                "(no external system configured)."
            ),
        }

    # -----------------------------------------------------
    # 8. CONNECT TO EHR
    # -----------------------------------------------------

    connector = get_connector(
        hospital
    )

    ext_patient, ext_provider, ext_facility = (
        _map_external_ids(
            db,
            connector,
            patient,
            doctor,
            hospital,
        )
    )

    payload = {
        "patient_id": ext_patient,
        "provider_id": ext_provider,
        "facility_id": ext_facility,
        "start_at": appt.start_at.isoformat(),
        "end_at": appt.end_at.isoformat(),
        "type": appointment_type,
    }

    # -----------------------------------------------------
    # 9. EHR CREATE WITH BOUNDED RETRIES
    # -----------------------------------------------------

    last_error = None

    for attempt in range(
        1,
        settings.EHR_MAX_RETRIES + 1,
    ):

        started = time.time()

        try:

            remote = connector.create_appointment(
                payload,
                idem,
            )

            appt.external_appointment_id = (
                remote.external_id
            )

            appt.external_status = (
                remote.status
            )

            db.commit()

            _log_integration(
                db,
                appt,
                "CREATE",
                attempt,
                "SUCCESS",
                None,
                int(
                    (time.time() - started) * 1000
                ),
            )

            last_error = None

            break

        except IntegrationError as e:

            last_error = e

            _log_integration(
                db,
                appt,
                "CREATE",
                attempt,
                e.error_class,
                e.error_class,
                int(
                    (time.time() - started) * 1000
                ),
            )

            metric(
                db,
                "integration.failure",
                error_class=e.error_class,
            )

            # -------------------------------------------------
            # UNKNOWN OUTCOME
            # -------------------------------------------------

            if e.error_class in (
                "TIMEOUT",
                "NETWORK",
                "UNKNOWN",
            ):

                appt.status = (
                    "SYNCHRONIZATION_PENDING"
                )

                db.commit()

                # NEVER blindly create another appointment.
                # First ask EHR whether the appointment exists.
                if verify_external_appointment(
                    db,
                    connector,
                    appt,
                ):

                    synchronize_state(
                        db,
                        appt,
                    )

                    emit_event(
                        db,
                        "APPOINTMENT_BOOKED",
                        appt,
                    )

                    audit(
                        db,
                        actor=actor,
                        action=(
                            "APPOINTMENT_RECOVERED_"
                            "AFTER_TIMEOUT"
                        ),
                        hospital_id=hospital.id,
                        resource_type="appointment",
                        resource_id=appt.id,
                        correlation_id=correlation_id,
                    )

                    metric(
                        db,
                        "integration.recovered",
                    )

                    return {
                        "status": "CONFIRMED",
                        "appointment_id": appt.id,
                        "start_at": (
                            appt.start_at.isoformat()
                        ),
                        "recovery": (
                            "timeout_verified_"
                            "no_duplicate"
                        ),
                        "message": (
                            "Appointment confirmed "
                            "after verifying the "
                            "external record."
                        ),
                    }

                # EHR confirms it does not exist.
                # A retry is now safe.
                continue

            # -------------------------------------------------
            # OUTAGE / RATE LIMIT
            # -------------------------------------------------

            if e.error_class in (
                "OUTAGE",
                "RATE_LIMITED",
            ):

                continue

            # -------------------------------------------------
            # VALIDATION / AUTH / CONFLICT
            # -------------------------------------------------

            break

    # -----------------------------------------------------
    # 10. RETRIES EXHAUSTED
    # -----------------------------------------------------

    if last_error is not None:

        # One final external verification.
        if verify_external_appointment(
            db,
            connector,
            appt,
        ):

            synchronize_state(
                db,
                appt,
            )

            emit_event(
                db,
                "APPOINTMENT_BOOKED",
                appt,
            )

            metric(
                db,
                "integration.recovered",
            )

            return {
                "status": "CONFIRMED",
                "appointment_id": appt.id,
                "start_at": (
                    appt.start_at.isoformat()
                ),
                "recovery": "verified_after_retries",
                "message": (
                    "Appointment confirmed "
                    "after verification."
                ),
            }

        # -------------------------------------------------
        # EHR STATE UNKNOWN → RECONCILIATION
        # -------------------------------------------------

        appt.status = (
            "RECONCILIATION_REQUIRED"
        )

        db.add(
            AppointmentHistory(
                appointment_id=appt.id,
                to_status="RECONCILIATION_REQUIRED",
                note=(
                    "EHR create failed and "
                    "external state could not "
                    "be verified"
                ),
            )
        )

        db.commit()

        open_reconciliation(
            db,
            appt,
            reason=f"EHR_{last_error.error_class}",
            detail=(
                "retries exhausted; "
                "external state unresolved"
            ),
        )

        # Since we know the external state is unresolved,
        # release the internal slot so staff can recover it.
        scheduling.release_slot(
            db,
            slot.id,
        )

        emit_event(
            db,
            "RECONCILIATION_REQUIRED",
            appt,
        )

        audit(
            db,
            actor=actor,
            action="APPOINTMENT_ESCALATED",
            success=False,
            hospital_id=hospital.id,
            resource_type="appointment",
            resource_id=appt.id,
            correlation_id=correlation_id,
            detail=last_error.error_class,
        )

        return {
            "status": "RECONCILIATION_REQUIRED",
            "appointment_id": appt.id,
            "message": (
                "I could not confirm this booking "
                "with the hospital system. "
                "A staff member will follow up."
            ),
        }

    # -----------------------------------------------------
    # 11. VERIFY HAPPY PATH
    # -----------------------------------------------------

    if not verify_external_appointment(
        db,
        connector,
        appt,
    ):

        appt.status = (
            "RECONCILIATION_REQUIRED"
        )

        db.add(
            AppointmentHistory(
                appointment_id=appt.id,
                to_status="RECONCILIATION_REQUIRED",
                note=(
                    "EHR accepted create but "
                    "record could not be verified"
                ),
            )
        )

        db.commit()

        open_reconciliation(
            db,
            appt,
            reason="VERIFICATION_FAILED",
            detail=(
                "EHR accepted the create but "
                "the record was not found"
            ),
        )

        emit_event(
            db,
            "RECONCILIATION_REQUIRED",
            appt,
        )

        return {
            "status": "RECONCILIATION_REQUIRED",
            "appointment_id": appt.id,
            "message": (
                "The hospital system accepted "
                "the request but the record "
                "could not be verified. "
                "Staff will follow up."
            ),
        }

    # -----------------------------------------------------
    # 12. SYNCHRONIZE
    # -----------------------------------------------------

    synchronize_state(
        db,
        appt,
    )

    emit_event(
        db,
        "APPOINTMENT_BOOKED",
        appt,
    )

    audit(
        db,
        actor=actor,
        actor_role="AI",
        action="APPOINTMENT_CONFIRMED",
        hospital_id=hospital.id,
        resource_type="appointment",
        resource_id=appt.id,
        correlation_id=correlation_id,
    )

    metric(
        db,
        "booking.success",
    )

    return {
        "status": "CONFIRMED",
        "appointment_id": appt.id,
        "doctor": doctor.name,
        "hospital": hospital.name,
        "start_at": appt.start_at.isoformat(),
        "message": (
            "Appointment confirmed and "
            "verified in the hospital system."
        ),
    }


# =========================================================
# CANCEL APPOINTMENT
# =========================================================

def cancel_appointment(
    db: Session,
    appointment_id: str,
    correlation_id: str,
    actor: str = "AI_AGENT",
) -> dict:
    """
    Cancel an appointment.

    If an external EHR appointment exists,
    cancel it there first.

    The local appointment is only marked CANCELLED
    after the external cancellation succeeds.
    """

    appt = db.get(
        Appointment,
        appointment_id,
    )

    if not appt:
        raise BookingError(
            "APPOINTMENT_NOT_FOUND"
        )

    if appt.status == "CANCELLED":

        return {
            "status": "CANCELLED",
            "message": "Already cancelled.",
        }

    if appt.status == "FAILED":

        return {
            "status": "FAILED",
            "message": "Appointment already failed.",
        }

    hospital = db.get(
        Hospital,
        appt.hospital_id,
    )

    if not hospital:
        raise BookingError(
            "HOSPITAL_NOT_FOUND"
        )

    # -----------------------------------------------------
    # EXTERNAL CANCELLATION
    # -----------------------------------------------------

    if (
        hospital.ehr_enabled
        and appt.external_appointment_id
    ):

        connector = get_connector(
            hospital
        )

        try:

            connector.cancel_appointment(
                appt.external_appointment_id
            )

            _log_integration(
                db,
                appt,
                "CANCEL",
                1,
                "SUCCESS",
                None,
                0,
            )

        except IntegrationError as e:

            _log_integration(
                db,
                appt,
                "CANCEL",
                1,
                e.error_class,
                e.error_class,
                0,
            )

            open_reconciliation(
                db,
                appt,
                reason=f"CANCEL_{e.error_class}",
            )

            return {
                "status": "RECONCILIATION_REQUIRED",
                "message": (
                    "Cancellation could not be "
                    "confirmed with the hospital system."
                ),
            }

    # -----------------------------------------------------
    # LOCAL CANCELLATION
    # -----------------------------------------------------

    appt.status = "CANCELLED"

    db.add(
        AppointmentHistory(
            appointment_id=appt.id,
            to_status="CANCELLED",
            note="Appointment cancelled",
        )
    )

    # Release the real slot.
    scheduling.release_slot(
        db,
        appt.slot_id,
    )

    db.commit()

    emit_event(
        db,
        "APPOINTMENT_CANCELLED",
        appt,
    )

    audit(
        db,
        actor=actor,
        action="APPOINTMENT_CANCELLED",
        hospital_id=appt.hospital_id,
        resource_type="appointment",
        resource_id=appt.id,
        correlation_id=correlation_id,
    )

    return {
        "status": "CANCELLED",
        "appointment_id": appt.id,
        "message": (
            "Appointment cancelled "
            "and the slot released."
        ),
    }


# =========================================================
# RESCHEDULE APPOINTMENT
# =========================================================

def reschedule_appointment(
    db: Session,
    appointment_id: str,
    new_slot_id: str,
    correlation_id: str,
    actor: str = "AI_AGENT",
) -> dict:
    """
    Reschedule an appointment to another REAL slot.

    Important rules:

    1. Old appointment must exist.
    2. New slot must be available.
    3. New slot must belong to the same hospital.
    4. New doctor must be active.
    5. New hospital must be approved.
    6. New slot is locked before changing anything.
    7. External EHR is updated.
    8. Only after success is old slot released.
    """

    # -----------------------------------------------------
    # 1. FIND APPOINTMENT
    # -----------------------------------------------------

    appt = db.get(
        Appointment,
        appointment_id,
    )

    if not appt:
        raise BookingError(
            "APPOINTMENT_NOT_FOUND"
        )

    if appt.status == "CANCELLED":
        raise BookingError(
            "APPOINTMENT_ALREADY_CANCELLED"
        )

    if appt.status in (
        "FAILED",
        "RECONCILIATION_REQUIRED",
    ):
        raise BookingError(
            "APPOINTMENT_NOT_RESCHEDULABLE"
        )

    # -----------------------------------------------------
    # 2. LOCK NEW SLOT
    # -----------------------------------------------------

    try:

        new_slot = scheduling.lock_slot_for_booking(
            db,
            new_slot_id,
        )

    except ValueError as e:

        db.rollback()

        raise BookingError(
            str(e)
        )

    # -----------------------------------------------------
    # 3. VALIDATE NEW SLOT
    # -----------------------------------------------------

    try:

        new_doctor, new_hospital = (
            _validate_slot_relationships(
                db,
                new_slot,
            )
        )

    except BookingError:

        db.rollback()

        raise

    # -----------------------------------------------------
    # 4. SAME-HOSPITAL CHECK
    # -----------------------------------------------------

    if new_hospital.id != appt.hospital_id:

        db.rollback()

        raise BookingError(
            "RESCHEDULE_HOSPITAL_MISMATCH"
        )

    # -----------------------------------------------------
    # 5. SAME DOCTOR CHECK
    # -----------------------------------------------------

    if new_doctor.id != appt.doctor_id:

        db.rollback()

        raise BookingError(
            "RESCHEDULE_DOCTOR_MISMATCH"
        )

    # -----------------------------------------------------
    # 6. REMEMBER OLD SLOT
    # -----------------------------------------------------

    old_slot_id = appt.slot_id

    old_start_at = appt.start_at
    old_end_at = appt.end_at

    # -----------------------------------------------------
    # 7. NO EHR
    # -----------------------------------------------------

    if (
        not new_hospital.ehr_enabled
        or not appt.external_appointment_id
    ):

        new_slot.status = "BOOKED"

        appt.slot_id = new_slot.id
        appt.start_at = new_slot.start_at
        appt.end_at = new_slot.end_at
        appt.status = "RESCHEDULED"

        db.add(
            AppointmentHistory(
                appointment_id=appt.id,
                to_status="RESCHEDULED",
                note=(
                    "Rescheduled internally "
                    "without external EHR"
                ),
            )
        )

        scheduling.release_slot(
            db,
            old_slot_id,
        )

        db.commit()

        emit_event(
            db,
            "APPOINTMENT_RESCHEDULED",
            appt,
        )

        audit(
            db,
            actor=actor,
            action="APPOINTMENT_RESCHEDULED",
            hospital_id=appt.hospital_id,
            resource_type="appointment",
            resource_id=appt.id,
            correlation_id=correlation_id,
        )

        return {
            "status": "RESCHEDULED",
            "appointment_id": appt.id,
            "start_at": (
                appt.start_at.isoformat()
            ),
            "message": (
                "Appointment moved successfully."
            ),
        }

    # -----------------------------------------------------
    # 8. EXTERNAL EHR RESCHEDULE
    # -----------------------------------------------------

    connector = get_connector(
        new_hospital
    )

    try:

        connector.reschedule_appointment(
            appt.external_appointment_id,
            new_slot.start_at.isoformat(),
        )

    except IntegrationError as e:

        # New slot was locked in our transaction.
        # Roll it back so it becomes available again.
        db.rollback()

        _log_integration(
            db,
            appt,
            "RESCHEDULE",
            1,
            e.error_class,
            e.error_class,
            0,
        )

        open_reconciliation(
            db,
            appt,
            reason=f"RESCHEDULE_{e.error_class}",
            detail=(
                "External reschedule failed. "
                "Original appointment remains unchanged."
            ),
        )

        return {
            "status": "RECONCILIATION_REQUIRED",
            "message": (
                "Reschedule could not be confirmed "
                "with the hospital system. "
                "The original appointment remains unchanged."
            ),
        }

    # -----------------------------------------------------
    # 9. UPDATE LOCAL APPOINTMENT
    # -----------------------------------------------------

    new_slot.status = "BOOKED"

    appt.slot_id = new_slot.id
    appt.start_at = new_slot.start_at
    appt.end_at = new_slot.end_at
    appt.status = "PENDING"

    db.add(
        AppointmentHistory(
            appointment_id=appt.id,
            to_status="PENDING",
            note=(
                "External reschedule succeeded; "
                "verification pending"
            ),
        )
    )

    db.commit()

    # -----------------------------------------------------
    # 10. VERIFY EXTERNAL APPOINTMENT
    # -----------------------------------------------------

    if not verify_external_appointment(
        db,
        connector,
        appt,
    ):

        # Keep the new slot booked because the EHR
        # operation succeeded, but we cannot confirm
        # the external state.
        open_reconciliation(
            db,
            appt,
            reason="RESCHEDULE_VERIFICATION_FAILED",
            detail=(
                "External reschedule succeeded "
                "but verification failed."
            ),
        )

        return {
            "status": "RECONCILIATION_REQUIRED",
            "message": (
                "The reschedule was sent to the "
                "hospital system but could not "
                "be verified."
            ),
        }

    # -----------------------------------------------------
    # 11. SYNCHRONIZE
    # -----------------------------------------------------

    synchronize_state(
        db,
        appt,
    )

    # -----------------------------------------------------
    # 12. RELEASE OLD SLOT
    # -----------------------------------------------------

    scheduling.release_slot(
        db,
        old_slot_id,
    )

    appt.status = "RESCHEDULED"

    db.add(
        AppointmentHistory(
            appointment_id=appt.id,
            to_status="RESCHEDULED",
            note="Reschedule verified successfully",
        )
    )

    db.commit()

    emit_event(
        db,
        "APPOINTMENT_RESCHEDULED",
        appt,
    )

    audit(
        db,
        actor=actor,
        action="APPOINTMENT_RESCHEDULED",
        hospital_id=appt.hospital_id,
        resource_type="appointment",
        resource_id=appt.id,
        correlation_id=correlation_id,
    )

    return {
        "status": "RESCHEDULED",
        "appointment_id": appt.id,
        "start_at": (
            appt.start_at.isoformat()
        ),
        "message": (
            "Appointment moved and verified."
        ),
    }