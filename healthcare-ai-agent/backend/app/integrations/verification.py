"""Verification + synchronization.

Rule from the PRD: a 2xx from the EHR is not proof. We re-read the record
from the external system, then copy that truth into our own state.
"""
from datetime import datetime

from sqlalchemy.orm import Session

from ..models import (
    Appointment, AppointmentHistory, ExternalIdMapping, IntegrationVerification,
    ReconciliationRecord,
)
from .base import EHRConnector, IntegrationError


def verify_external_appointment(
    db: Session, connector: EHRConnector, appointment: Appointment,
    external_id: str | None = None,
) -> bool:
    external_id = external_id or appointment.external_appointment_id
    verified, detail = False, ""
    try:
        if external_id:
            remote = connector.get_appointment(external_id)
        else:
            remote = connector.find_by_idempotency_key(appointment.idempotency_key)
        if remote:
            verified = True
            external_id = remote.external_id
            appointment.external_appointment_id = remote.external_id
            appointment.external_status = remote.status
            detail = f"found in EHR with status {remote.status}"
        else:
            detail = "not present in EHR"
    except IntegrationError as e:
        detail = f"verification failed: {e.error_class}"

    db.add(IntegrationVerification(
        appointment_id=appointment.id, external_id=external_id, verified=verified,
        detail=detail, correlation_id=appointment.correlation_id,
    ))
    db.commit()
    return verified


def synchronize_state(db: Session, appointment: Appointment) -> Appointment:
    """External truth → internal state. Only now may we say 'confirmed'."""
    previous = appointment.status
    appointment.status = "CONFIRMED"
    appointment.updated_at = datetime.utcnow()

    exists = db.query(ExternalIdMapping).filter_by(
        entity_type="APPOINTMENT", internal_id=appointment.id
    ).first()
    if not exists and appointment.external_appointment_id:
        db.add(ExternalIdMapping(
            hospital_id=appointment.hospital_id, entity_type="APPOINTMENT",
            internal_id=appointment.id, external_id=appointment.external_appointment_id,
        ))
    db.add(AppointmentHistory(
        appointment_id=appointment.id, from_status=previous, to_status="CONFIRMED",
        note="synchronized from verified external record",
    ))
    db.commit()
    return appointment


def open_reconciliation(db: Session, appointment: Appointment, reason: str,
                        detail: str = "") -> ReconciliationRecord:
    appointment.status = "RECONCILIATION_REQUIRED"
    record = ReconciliationRecord(
        hospital_id=appointment.hospital_id, appointment_id=appointment.id,
        reason=reason, detail=detail, correlation_id=appointment.correlation_id,
    )
    db.add(record)
    db.add(AppointmentHistory(
        appointment_id=appointment.id, to_status="RECONCILIATION_REQUIRED", note=reason
    ))
    db.commit()
    return record
