import uuid

from sqlalchemy.orm import Session

from ..models import AuditEvent, OperationalMetric


def new_correlation_id() -> str:
    return "CORR-" + uuid.uuid4().hex[:10]


def audit(
    db: Session,
    *,
    actor: str,
    action: str,
    actor_role: str = "SYSTEM",
    hospital_id: str | None = None,
    resource_type: str = "",
    resource_id: str | None = None,
    success: bool = True,
    correlation_id: str | None = None,
    detail: str = "",
) -> None:
    """Privacy-aware: `detail` must never carry raw clinical answers."""
    db.add(AuditEvent(
        actor=actor, actor_role=actor_role, action=action, hospital_id=hospital_id,
        resource_type=resource_type, resource_id=resource_id, success=success,
        correlation_id=correlation_id, detail=detail[:500],
    ))
    db.commit()


def metric(db: Session, name: str, value: float = 1, **labels) -> None:
    db.add(OperationalMetric(name=name, value=value, labels=labels))
    db.commit()
