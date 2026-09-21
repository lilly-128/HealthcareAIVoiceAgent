from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import (
    HOSPITAL_ADMIN,
    PLATFORM_ADMIN,
    Principal,
    require_roles,
)
from ..db import get_db
from ..models import (
    AIConversation,
    Appointment,
    AuditEvent,
    CapabilityExecution,
    Doctor,
    Hospital,
    IntegrationOperation,
    Notification,
    Patient,
    ReconciliationRecord,
    User,
    WorkflowExecution,
)
from ..services.audit import audit
from ..workflows.engine import tick


router = APIRouter(prefix="/admin", tags=["admin"])


# =========================================================
# TENANT SCOPE
# =========================================================

def _scope(p: Principal, query, column):
    """
    Platform Admin sees everything.
    Hospital Admin sees only data belonging to their hospital.
    """

    if p.role == PLATFORM_ADMIN:
        return query

    if not p.hospital_id:
        raise HTTPException(
            status_code=403,
            detail="Hospital access is required",
        )

    return query.where(column == p.hospital_id)


# =========================================================
# HOSPITAL APPROVAL
# =========================================================

@router.post("/hospitals/{hospital_id}/approve")
def approve(
    hospital_id: str,
    p: Principal = Depends(require_roles(PLATFORM_ADMIN)),
    db: Session = Depends(get_db),
):
    hospital = db.get(Hospital, hospital_id)

    if not hospital:
        raise HTTPException(
            status_code=404,
            detail="Hospital not found",
        )

    if hospital.status == "APPROVED":
        return {
            "hospital_id": hospital_id,
            "status": hospital.status,
            "message": "Hospital is already approved",
        }

    hospital.status = "APPROVED"
    db.commit()

    audit(
        db,
        actor=p.user_id,
        actor_role=p.role,
        action="HOSPITAL_APPROVED",
        hospital_id=hospital_id,
        resource_type="hospital",
        resource_id=hospital_id,
    )

    return {
        "hospital_id": hospital_id,
        "status": hospital.status,
    }


@router.post("/hospitals/{hospital_id}/reject")
def reject(
    hospital_id: str,
    p: Principal = Depends(require_roles(PLATFORM_ADMIN)),
    db: Session = Depends(get_db),
):
    hospital = db.get(Hospital, hospital_id)

    if not hospital:
        raise HTTPException(
            status_code=404,
            detail="Hospital not found",
        )

    hospital.status = "REJECTED"
    db.commit()

    audit(
        db,
        actor=p.user_id,
        actor_role=p.role,
        action="HOSPITAL_REJECTED",
        hospital_id=hospital_id,
        resource_type="hospital",
        resource_id=hospital_id,
    )

    return {
        "hospital_id": hospital_id,
        "status": hospital.status,
    }


# =========================================================
# OVERVIEW
# =========================================================

@router.get("/overview")
def overview(
    p: Principal = Depends(
        require_roles(PLATFORM_ADMIN, HOSPITAL_ADMIN)
    ),
    db: Session = Depends(get_db),
):

    # =====================================================
    # APPOINTMENTS
    # =====================================================

    if p.role == PLATFORM_ADMIN:

        appointments = db.scalars(
            select(Appointment)
        ).all()

    else:

        if not p.hospital_id:
            raise HTTPException(
                status_code=403,
                detail="Hospital access is required",
            )

        appointments = db.scalars(
            select(Appointment)
            .where(
                Appointment.hospital_id == p.hospital_id
            )
        ).all()

    by_status = {}

    for appointment in appointments:
        by_status[appointment.status] = (
            by_status.get(appointment.status, 0) + 1
        )

    # =====================================================
    # DOCTORS
    # =====================================================

    if p.role == PLATFORM_ADMIN:

        doctors_count = db.scalar(
            select(func.count())
            .select_from(Doctor)
        ) or 0

    else:

        doctors_count = db.scalar(
            select(func.count())
            .select_from(Doctor)
            .where(
                Doctor.hospital_id == p.hospital_id
            )
        ) or 0

    # =====================================================
    # PATIENTS
    # =====================================================
    #
    # Patient does NOT have hospital_id.
    #
    # Patient -> User -> hospital_id
    # =====================================================

    if p.role == PLATFORM_ADMIN:

        patients_count = db.scalar(
            select(func.count())
            .select_from(Patient)
        ) or 0

    else:

        patients_count = db.scalar(
            select(func.count())
            .select_from(Patient)
            .join(
                User,
                User.patient_id == Patient.id,
            )
            .where(
                User.hospital_id == p.hospital_id
            )
        ) or 0

    # =====================================================
    # AI CONVERSATIONS
    # =====================================================
    #
    # AIConversation does NOT have hospital_id.
    #
    # Do not use:
    # AIConversation.hospital_id
    # =====================================================

    if p.role == PLATFORM_ADMIN:

        ai_count = db.scalar(
            select(func.count())
            .select_from(AIConversation)
        ) or 0

    else:

        # Cannot safely filter by hospital because the
        # current AIConversation model has no hospital_id.
        ai_count = 0

    # =====================================================
    # CAPABILITY EXECUTIONS
    # =====================================================
    #
    # CapabilityExecution does NOT have hospital_id.
    # =====================================================

    capabilities = db.scalars(
        select(CapabilityExecution)
    ).all()

    if p.role == HOSPITAL_ADMIN:

        # No hospital_id exists on this model.
        # Keep the result empty rather than exposing
        # another hospital's data.
        capabilities = []

    # =====================================================
    # INTEGRATION OPERATIONS
    # =====================================================

    if p.role == PLATFORM_ADMIN:

        integrations = db.scalars(
            select(IntegrationOperation)
        ).all()

    else:

        integrations = db.scalars(
            select(IntegrationOperation)
            .where(
                IntegrationOperation.hospital_id
                == p.hospital_id
            )
        ).all()

    # =====================================================
    # RECONCILIATIONS
    # =====================================================

    if p.role == PLATFORM_ADMIN:

        reconciliations = db.scalars(
            select(ReconciliationRecord)
        ).all()

    else:

        reconciliations = db.scalars(
            select(ReconciliationRecord)
            .where(
                ReconciliationRecord.hospital_id
                == p.hospital_id
            )
        ).all()

    # =====================================================
    # WORKFLOWS
    # =====================================================

    if p.role == PLATFORM_ADMIN:

        workflows = db.scalars(
            select(WorkflowExecution)
        ).all()

    else:

        workflows = db.scalars(
            select(WorkflowExecution)
            .where(
                WorkflowExecution.hospital_id
                == p.hospital_id
            )
        ).all()

    # =====================================================
    # NOTIFICATIONS
    # =====================================================

    if p.role == PLATFORM_ADMIN:

        notifications_count = db.scalar(
            select(func.count())
            .select_from(Notification)
        ) or 0

    else:

        notifications_count = db.scalar(
            select(func.count())
            .select_from(Notification)
            .where(
                Notification.hospital_id
                == p.hospital_id
            )
        ) or 0

    # =====================================================
    # HOSPITAL COUNT
    # =====================================================

    if p.role == PLATFORM_ADMIN:

        hospitals_count = db.scalar(
            select(func.count())
            .select_from(Hospital)
        ) or 0

    else:

        hospitals_count = 1

    # =====================================================
    # RETURN DASHBOARD DATA
    # =====================================================

    return {
        "hospitals": hospitals_count,

        "doctors": doctors_count,

        "patients": patients_count,

        "appointments_total": len(appointments),

        "appointments_by_status": by_status,

        "ai_conversations": ai_count,

        "capability_calls": len(capabilities),

        "capability_failures": len([
            c
            for c in capabilities
            if c.success is False
        ]),

        "integration_ops": len(integrations),

        "integration_failures": len([
            i
            for i in integrations
            if i.outcome != "SUCCESS"
        ]),

        "reconciliations_open": len([
            r
            for r in reconciliations
            if r.status == "OPEN"
        ]),

        "workflows_by_state": {
            state: len([
                w
                for w in workflows
                if w.state == state
            ])
            for state in {
                w.state
                for w in workflows
            }
        },

        "notifications": notifications_count,
    }

# =========================================================
# PENDING HOSPITALS
# =========================================================

@router.get("/hospitals/pending")
def pending(
    p: Principal = Depends(
        require_roles(PLATFORM_ADMIN)
    ),
    db: Session = Depends(get_db),
):

    hospitals = db.scalars(
        select(Hospital)
        .where(
            Hospital.status.in_(
                ("SUBMITTED", "UNDER_REVIEW")
            )
        )
        .order_by(
            Hospital.created_at.desc()
        )
    ).all()

    return [
        {
            "id": hospital.id,
            "name": hospital.name,
            "address": hospital.address,
            "city": hospital.city,
            "phone": hospital.phone,
            "status": hospital.status,
            "ehr_enabled": hospital.ehr_enabled,
        }
        for hospital in hospitals
    ]


# =========================================================
# APPOINTMENTS
# =========================================================

@router.get("/appointments")
def appointments(
    p: Principal = Depends(
        require_roles(
            PLATFORM_ADMIN,
            HOSPITAL_ADMIN,
        )
    ),
    db: Session = Depends(get_db),
):

    query = _scope(
        p,
        select(Appointment)
        .order_by(
            Appointment.created_at.desc()
        ),
        Appointment.hospital_id,
    )

    rows = db.scalars(
        query.limit(50)
    ).all()

    result = []

    for appointment in rows:

        doctor = db.get(
            Doctor,
            appointment.doctor_id,
        )

        patient = db.get(
            Patient,
            appointment.patient_id,
        )

        result.append({
            "id": appointment.id,

            "status": appointment.status,

            "when": appointment.start_at.isoformat(),

            "doctor": (
                doctor.name
                if doctor
                else "Unknown"
            ),

            "patient": (
                patient.name
                if patient
                else "Unknown"
            ),

            "external_id":
                appointment.external_appointment_id,

            "correlation_id":
                appointment.correlation_id,

            "hospital_id":
                appointment.hospital_id,
        })

    return result


# =========================================================
# INTEGRATION OPERATIONS
# =========================================================

@router.get("/integration-operations")
def integration_ops(
    p: Principal = Depends(
        require_roles(
            PLATFORM_ADMIN,
            HOSPITAL_ADMIN,
        )
    ),
    db: Session = Depends(get_db),
):

    query = _scope(
        p,
        select(IntegrationOperation)
        .order_by(
            IntegrationOperation.at.desc()
        ),
        IntegrationOperation.hospital_id,
    )

    rows = db.scalars(
        query.limit(50)
    ).all()

    return [
        {
            "id": row.id,
            "operation": row.operation,
            "attempt": row.attempt,
            "outcome": row.outcome,
            "appointment_id": row.appointment_id,
            "correlation_id": row.correlation_id,
            "at": row.at.isoformat(),
            "hospital_id": row.hospital_id,
        }
        for row in rows
    ]


# =========================================================
# RECONCILIATIONS
# =========================================================

@router.get("/reconciliations")
def reconciliations(
    p: Principal = Depends(
        require_roles(
            PLATFORM_ADMIN,
            HOSPITAL_ADMIN,
        )
    ),
    db: Session = Depends(get_db),
):

    query = _scope(
        p,
        select(ReconciliationRecord)
        .order_by(
            ReconciliationRecord.at.desc()
        ),
        ReconciliationRecord.hospital_id,
    )

    rows = db.scalars(
        query.limit(50)
    ).all()

    return [
        {
            "id": row.id,
            "appointment_id": row.appointment_id,
            "reason": row.reason,
            "status": row.status,
            "detail": row.detail,
            "correlation_id": row.correlation_id,
            "hospital_id": row.hospital_id,
        }
        for row in rows
    ]


# =========================================================
# RESOLVE RECONCILIATION
# =========================================================

@router.post(
    "/reconciliations/{record_id}/resolve"
)
def resolve(
    record_id: str,
    p: Principal = Depends(
        require_roles(
            PLATFORM_ADMIN,
            HOSPITAL_ADMIN,
        )
    ),
    db: Session = Depends(get_db),
):

    record = db.get(
        ReconciliationRecord,
        record_id,
    )

    if not record:
        raise HTTPException(
            status_code=404,
            detail="Reconciliation record not found",
        )

    if (
        p.role != PLATFORM_ADMIN
        and record.hospital_id != p.hospital_id
    ):
        raise HTTPException(
            status_code=403,
            detail=(
                "You cannot access this "
                "hospital's reconciliation record"
            ),
        )

    record.status = "RESOLVED"
    record.resolved_at = datetime.utcnow()

    db.commit()

    audit(
        db,
        actor=p.user_id,
        actor_role=p.role,
        action="RECONCILIATION_RESOLVED",
        hospital_id=record.hospital_id,
        resource_type="reconciliation",
        resource_id=record.id,
        correlation_id=record.correlation_id,
    )

    return {
        "status": record.status,
        "record_id": record.id,
    }


# =========================================================
# AUDIT LOG
# =========================================================

@router.get("/audit")
def audit_log(
    correlation_id: str | None = None,
    p: Principal = Depends(
        require_roles(
            PLATFORM_ADMIN,
            HOSPITAL_ADMIN,
        )
    ),
    db: Session = Depends(get_db),
):

    query = select(
        AuditEvent
    ).order_by(
        AuditEvent.at.desc()
    )

    if correlation_id:
        query = query.where(
            AuditEvent.correlation_id
            == correlation_id
        )

    if p.role != PLATFORM_ADMIN:

        if not p.hospital_id:
            raise HTTPException(
                status_code=403,
                detail="Hospital access is required",
            )

        query = query.where(
            AuditEvent.hospital_id
            == p.hospital_id
        )

    events = db.scalars(
        query.limit(80)
    ).all()

    return [
        {
            "at": event.at.isoformat(),
            "actor": event.actor,
            "role": event.actor_role,
            "action": event.action,

            "resource": (
                f"{event.resource_type}:"
                f"{event.resource_id}"
            ),

            "success": event.success,

            "correlation_id":
                event.correlation_id,

            "detail": event.detail,

            "hospital_id":
                event.hospital_id,
        }
        for event in events
    ]


# =========================================================
# END-TO-END TRACE
# =========================================================

@router.get("/trace/{correlation_id}")
def trace(
    correlation_id: str,
    p: Principal = Depends(
        require_roles(
            PLATFORM_ADMIN,
            HOSPITAL_ADMIN,
        )
    ),
    db: Session = Depends(get_db),
):

    caps = db.scalars(
        select(CapabilityExecution)
        .where(
            CapabilityExecution.correlation_id
            == correlation_id
        )
    ).all()

    ops = db.scalars(
        select(IntegrationOperation)
        .where(
            IntegrationOperation.correlation_id
            == correlation_id
        )
    ).all()

    workflows = db.scalars(
        select(WorkflowExecution)
        .where(
            WorkflowExecution.correlation_id
            == correlation_id
        )
    ).all()

    notes = db.scalars(
        select(Notification)
        .where(
            Notification.correlation_id
            == correlation_id
        )
    ).all()

    # -----------------------------------------------------
    # TENANT ISOLATION
    # -----------------------------------------------------

    if p.role != PLATFORM_ADMIN:

        if not p.hospital_id:
            raise HTTPException(
                status_code=403,
                detail="Hospital access is required",
            )

        hospital_ids = set()

        for capability in caps:
            if capability.hospital_id:
                hospital_ids.add(
                    capability.hospital_id
                )

        for operation in ops:
            if operation.hospital_id:
                hospital_ids.add(
                    operation.hospital_id
                )

        for workflow in workflows:
            if workflow.hospital_id:
                hospital_ids.add(
                    workflow.hospital_id
                )

        for notification in notes:
            if notification.hospital_id:
                hospital_ids.add(
                    notification.hospital_id
                )

        if hospital_ids and hospital_ids != {
            p.hospital_id
        }:
            raise HTTPException(
                status_code=403,
                detail="You cannot access this trace",
            )

    # -----------------------------------------------------
    # BUILD TIMELINE
    # -----------------------------------------------------

    events = []

    events += [
        {
            "stage": "CAPABILITY",
            "name": capability.capability,
            "ok": capability.success,
            "at": capability.at.isoformat(),
            "ms": capability.duration_ms,
        }
        for capability in caps
    ]

    events += [
        {
            "stage": "INTEGRATION",
            "name": (
                f"{operation.operation} "
                f"#{operation.attempt}"
            ),
            "ok": (
                operation.outcome
                == "SUCCESS"
            ),
            "at": operation.at.isoformat(),
        }
        for operation in ops
    ]

    events += [
        {
            "stage": "WORKFLOW",
            "name": workflow.workflow,
            "ok": (
                workflow.state
                == "COMPLETED"
            ),
            "at": workflow.run_at.isoformat(),
        }
        for workflow in workflows
    ]

    events += [
        {
            "stage": "NOTIFICATION",
            "name": notification.template,
            "ok": True,
            "at": notification.at.isoformat(),
        }
        for notification in notes
    ]

    return {
        "correlation_id": correlation_id,
        "timeline": sorted(
            events,
            key=lambda event: event["at"],
        ),
    }


# =========================================================
# RUN WORKFLOWS
# =========================================================

@router.post("/workflows/tick")
def run_workflows(
    p: Principal = Depends(
        require_roles(
            PLATFORM_ADMIN,
            HOSPITAL_ADMIN,
        )
    ),
    db: Session = Depends(get_db),
):

    return {
        "executed": tick(db)
    }