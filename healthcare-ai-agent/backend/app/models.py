"""Data model.

One file on purpose: the prototype stays readable, and every table carries the
tenant key (hospital_id) where the PRD requires isolation.
"""

import uuid
from datetime import datetime, date, time

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    Time,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def uid() -> str:
    return uuid.uuid4().hex[:12]


def now() -> datetime:
    return datetime.utcnow()


# ---------------------------------------------------------------- tenancy ---
class Hospital(Base):
    __tablename__ = "hospitals"

    id: Mapped[str] = mapped_column(
        String,
        primary_key=True,
        default=uid
    )

    name: Mapped[str] = mapped_column(
        String,
        nullable=False
    )

    address: Mapped[str] = mapped_column(
        String,
        default=""
    )

    city: Mapped[str] = mapped_column(
        String,
        default=""
    )

    phone: Mapped[str] = mapped_column(
        String,
        default=""
    )

    # DRAFT | SUBMITTED | UNDER_REVIEW | APPROVED | REJECTED | SUSPENDED
    status: Mapped[str] = mapped_column(
        String,
        default="SUBMITTED"
    )

    ehr_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True
    )

    external_facility_id: Mapped[str | None] = mapped_column(
        String,
        nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now
    )

    # One hospital can have many doctors
    doctors: Mapped[list["Doctor"]] = relationship(
        back_populates="hospital"
    )


# ---------------------------------------------------------------- departments ---
class Department(Base):
    __tablename__ = "departments"

    id: Mapped[str] = mapped_column(
        String,
        primary_key=True,
        default=uid
    )

    hospital_id: Mapped[str] = mapped_column(
        ForeignKey("hospitals.id"),
        index=True
    )

    name: Mapped[str] = mapped_column(
        String
    )


# ---------------------------------------------------------------- specialties ---
class Specialty(Base):
    __tablename__ = "specialties"

    id: Mapped[str] = mapped_column(
        String,
        primary_key=True,
        default=uid
    )

    name: Mapped[str] = mapped_column(
        String,
        unique=True
    )


# ---------------------------------------------------------------- users ---
class User(Base):
    """
    Login account for:
    PLATFORM_ADMIN
    HOSPITAL_ADMIN
    DOCTOR
    PATIENT
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(
        String,
        primary_key=True,
        default=uid
    )

    email: Mapped[str] = mapped_column(
        String,
        unique=True,
        index=True
    )

    password_hash: Mapped[str] = mapped_column(
        String
    )

    role: Mapped[str] = mapped_column(
        String
    )

    # For Hospital Admin and Doctor users
    hospital_id: Mapped[str | None] = mapped_column(
        ForeignKey("hospitals.id"),
        nullable=True,
        index=True
    )

    # For Doctor login
    doctor_id: Mapped[str | None] = mapped_column(
        String,
        nullable=True
    )

    # For Patient login
    patient_id: Mapped[str | None] = mapped_column(
        String,
        nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now
    )

    # Allows:
    # user.hospital
    hospital: Mapped["Hospital | None"] = relationship()


# ------------------------------------------------------------- scheduling ---
class Doctor(Base):
    __tablename__ = "doctors"

    id: Mapped[str] = mapped_column(
        String,
        primary_key=True,
        default=uid
    )

    # Every doctor belongs to one hospital
    hospital_id: Mapped[str] = mapped_column(
        ForeignKey("hospitals.id"),
        index=True
    )

    name: Mapped[str] = mapped_column(
        String
    )

    specialty: Mapped[str] = mapped_column(
        String,
        index=True
    )

    department: Mapped[str] = mapped_column(
        String,
        default=""
    )

    qualification: Mapped[str] = mapped_column(
        String,
        default=""
    )

    experience_years: Mapped[int] = mapped_column(
        Integer,
        default=0
    )

    languages: Mapped[str] = mapped_column(
        String,
        default="English"
    )

    consultation_minutes: Mapped[int] = mapped_column(
        Integer,
        default=30
    )

    # INVITED | ACTIVE | INACTIVE
    status: Mapped[str] = mapped_column(
        String,
        default="ACTIVE"
    )

    external_provider_id: Mapped[str | None] = mapped_column(
        String,
        nullable=True
    )

    # Doctor belongs to one hospital
    hospital: Mapped[Hospital] = relationship(
        back_populates="doctors"
    )


# ---------------------------------------------------------------- calendar ---
class Calendar(Base):
    __tablename__ = "calendars"

    id: Mapped[str] = mapped_column(
        String,
        primary_key=True,
        default=uid
    )

    hospital_id: Mapped[str] = mapped_column(
        ForeignKey("hospitals.id"),
        index=True
    )

    doctor_id: Mapped[str] = mapped_column(
        ForeignKey("doctors.id"),
        index=True
    )

    # 0 = Monday ... 6 = Sunday
    weekday: Mapped[int] = mapped_column(
        Integer
    )

    start_time: Mapped[time] = mapped_column(
        Time
    )

    end_time: Mapped[time] = mapped_column(
        Time
    )

    active: Mapped[bool] = mapped_column(
        Boolean,
        default=True
    )


# ---------------------------------------------------------------- slots ---
class Slot(Base):
    """
    Materialised bookable slot.
    Source of truth for availability.
    """

    __tablename__ = "slots"

    __table_args__ = (
        # Same doctor cannot have two slots
        # starting at the same time.
        UniqueConstraint(
            "doctor_id",
            "start_at",
            name="uq_slot_doctor_start"
        ),

        Index(
            "ix_slot_lookup",
            "hospital_id",
            "doctor_id",
            "start_at",
            "status"
        ),
    )

    id: Mapped[str] = mapped_column(
        String,
        primary_key=True,
        default=uid
    )

    hospital_id: Mapped[str] = mapped_column(
        ForeignKey("hospitals.id"),
        index=True
    )

    doctor_id: Mapped[str] = mapped_column(
        ForeignKey("doctors.id"),
        index=True
    )

    start_at: Mapped[datetime] = mapped_column(
        DateTime
    )

    end_at: Mapped[datetime] = mapped_column(
        DateTime
    )

    # AVAILABLE | HELD | BOOKED | BLOCKED
    status: Mapped[str] = mapped_column(
        String,
        default="AVAILABLE"
    )

    appointment_type: Mapped[str] = mapped_column(
        String,
        default="CONSULTATION"
    )


# ---------------------------------------------------------- blocked periods ---
class BlockedPeriod(Base):
    __tablename__ = "blocked_periods"

    id: Mapped[str] = mapped_column(
        String,
        primary_key=True,
        default=uid
    )

    hospital_id: Mapped[str] = mapped_column(
        ForeignKey("hospitals.id"),
        index=True
    )

    doctor_id: Mapped[str] = mapped_column(
        ForeignKey("doctors.id"),
        index=True
    )

    start_at: Mapped[datetime] = mapped_column(
        DateTime
    )

    end_at: Mapped[datetime] = mapped_column(
        DateTime
    )

    reason: Mapped[str] = mapped_column(
        String,
        default="LEAVE"
    )


# ---------------------------------------------------------------- patient ---
class Patient(Base):
    __tablename__ = "patients"

    id: Mapped[str] = mapped_column(
        String,
        primary_key=True,
        default=uid
    )

    name: Mapped[str] = mapped_column(
        String
    )

    phone: Mapped[str] = mapped_column(
        String,
        index=True
    )

    email: Mapped[str | None] = mapped_column(
        String,
        nullable=True
    )

    date_of_birth: Mapped[date | None] = mapped_column(
        Date,
        nullable=True
    )

    communication_preference: Mapped[str] = mapped_column(
        String,
        default="SMS"
    )

    external_patient_id: Mapped[str | None] = mapped_column(
        String,
        nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now
    )


# ------------------------------------------------------- patient preferences ---
class PatientPreference(Base):
    __tablename__ = "patient_preferences"

    id: Mapped[str] = mapped_column(
        String,
        primary_key=True,
        default=uid
    )

    patient_id: Mapped[str] = mapped_column(
        ForeignKey("patients.id"),
        index=True
    )

    key: Mapped[str] = mapped_column(
        String
    )

    value: Mapped[str] = mapped_column(
        String
    )


# ------------------------------------------------------------ appointment ---
class Appointment(Base):
    __tablename__ = "appointments"

    id: Mapped[str] = mapped_column(
        String,
        primary_key=True,
        default=uid
    )

    hospital_id: Mapped[str] = mapped_column(
        ForeignKey("hospitals.id"),
        index=True
    )

    doctor_id: Mapped[str] = mapped_column(
        ForeignKey("doctors.id"),
        index=True
    )

    patient_id: Mapped[str] = mapped_column(
        ForeignKey("patients.id"),
        index=True
    )

    slot_id: Mapped[str] = mapped_column(
        ForeignKey("slots.id")
    )

    start_at: Mapped[datetime] = mapped_column(
        DateTime
    )

    end_at: Mapped[datetime] = mapped_column(
        DateTime
    )

    appointment_type: Mapped[str] = mapped_column(
        String,
        default="CONSULTATION"
    )

    # REQUESTED | PENDING | CONFIRMED | RESCHEDULED |
    # CANCELLED | COMPLETED | NO_SHOW | FAILED |
    # SYNCHRONIZATION_PENDING | RECONCILIATION_REQUIRED
    status: Mapped[str] = mapped_column(
        String,
        default="REQUESTED"
    )

    external_appointment_id: Mapped[str | None] = mapped_column(
        String,
        nullable=True
    )

    external_status: Mapped[str | None] = mapped_column(
        String,
        nullable=True
    )

    idempotency_key: Mapped[str] = mapped_column(
        String,
        unique=True,
        index=True
    )

    correlation_id: Mapped[str] = mapped_column(
        String,
        index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now,
        onupdate=now
    )


# ------------------------------------------------------ appointment history ---
class AppointmentHistory(Base):
    __tablename__ = "appointment_history"

    id: Mapped[str] = mapped_column(
        String,
        primary_key=True,
        default=uid
    )

    appointment_id: Mapped[str] = mapped_column(
        ForeignKey("appointments.id"),
        index=True
    )

    from_status: Mapped[str | None] = mapped_column(
        String,
        nullable=True
    )

    to_status: Mapped[str] = mapped_column(
        String
    )

    note: Mapped[str] = mapped_column(
        String,
        default=""
    )

    at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now
    )


# --------------------------------------------------------- questionnaires ---
class Questionnaire(Base):
    __tablename__ = "questionnaires"

    id: Mapped[str] = mapped_column(
        String,
        primary_key=True,
        default=uid
    )

    hospital_id: Mapped[str] = mapped_column(
        ForeignKey("hospitals.id"),
        index=True
    )

    specialty: Mapped[str | None] = mapped_column(
        String,
        nullable=True
    )

    name: Mapped[str] = mapped_column(
        String
    )

    # [{"key":"duration","text":"...","type":"SHORT_TEXT"}]
    questions: Mapped[list] = mapped_column(
        JSON,
        default=list
    )

    active: Mapped[bool] = mapped_column(
        Boolean,
        default=True
    )


# ------------------------------------------------ questionnaire responses ---
class QuestionnaireResponse(Base):
    __tablename__ = "questionnaire_responses"

    id: Mapped[str] = mapped_column(
        String,
        primary_key=True,
        default=uid
    )

    hospital_id: Mapped[str] = mapped_column(
        ForeignKey("hospitals.id"),
        index=True
    )

    questionnaire_id: Mapped[str] = mapped_column(
        ForeignKey("questionnaires.id")
    )

    appointment_id: Mapped[str] = mapped_column(
        ForeignKey("appointments.id"),
        index=True
    )

    patient_id: Mapped[str] = mapped_column(
        ForeignKey("patients.id")
    )

    answers: Mapped[dict] = mapped_column(
        JSON,
        default=dict
    )

    # ASSIGNED | COMPLETED
    status: Mapped[str] = mapped_column(
        String,
        default="ASSIGNED"
    )

    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True
    )


# -------------------------------------------------------------- AI + audit ---
class AIConversation(Base):
    __tablename__ = "ai_conversations"

    id: Mapped[str] = mapped_column(
        String,
        primary_key=True,
        default=uid
    )

    patient_id: Mapped[str | None] = mapped_column(
        String,
        nullable=True
    )

    channel: Mapped[str] = mapped_column(
        String,
        default="WEB_VOICE"
    )

    correlation_id: Mapped[str] = mapped_column(
        String,
        index=True
    )

    messages: Mapped[list] = mapped_column(
        JSON,
        default=list
    )

    context: Mapped[dict] = mapped_column(
        JSON,
        default=dict
    )

    status: Mapped[str] = mapped_column(
        String,
        default="ACTIVE"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now
    )


# --------------------------------------------------- capability executions ---
class CapabilityExecution(Base):
    __tablename__ = "capability_executions"

    id: Mapped[str] = mapped_column(
        String,
        primary_key=True,
        default=uid
    )

    conversation_id: Mapped[str | None] = mapped_column(
        String,
        index=True
    )

    correlation_id: Mapped[str] = mapped_column(
        String,
        index=True
    )

    capability: Mapped[str] = mapped_column(
        String
    )

    request: Mapped[dict] = mapped_column(
        JSON,
        default=dict
    )

    response: Mapped[dict] = mapped_column(
        JSON,
        default=dict
    )

    success: Mapped[bool] = mapped_column(
        Boolean,
        default=True
    )

    error: Mapped[str | None] = mapped_column(
        String,
        nullable=True
    )

    duration_ms: Mapped[int] = mapped_column(
        Integer,
        default=0
    )

    at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now
    )


# -------------------------------------------------------------- audit ---
class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(
        String,
        primary_key=True,
        default=uid
    )

    hospital_id: Mapped[str | None] = mapped_column(
        String,
        index=True,
        nullable=True
    )

    actor: Mapped[str] = mapped_column(
        String
    )

    actor_role: Mapped[str] = mapped_column(
        String,
        default="SYSTEM"
    )

    action: Mapped[str] = mapped_column(
        String
    )

    resource_type: Mapped[str] = mapped_column(
        String,
        default=""
    )

    resource_id: Mapped[str | None] = mapped_column(
        String,
        nullable=True
    )

    success: Mapped[bool] = mapped_column(
        Boolean,
        default=True
    )

    correlation_id: Mapped[str | None] = mapped_column(
        String,
        index=True,
        nullable=True
    )

    detail: Mapped[str] = mapped_column(
        Text,
        default=""
    )

    at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now
    )


# ------------------------------------------------------------ integration ---
class ExternalIdMapping(Base):
    __tablename__ = "external_id_mappings"

    __table_args__ = (
        UniqueConstraint(
            "entity_type",
            "internal_id",
            name="uq_map"
        ),
    )

    id: Mapped[str] = mapped_column(
        String,
        primary_key=True,
        default=uid
    )

    hospital_id: Mapped[str] = mapped_column(
        String,
        index=True
    )

    entity_type: Mapped[str] = mapped_column(
        String
    )  # PATIENT | PROVIDER | FACILITY | APPOINTMENT

    internal_id: Mapped[str] = mapped_column(
        String,
        index=True
    )

    external_id: Mapped[str] = mapped_column(
        String,
        index=True
    )

    system: Mapped[str] = mapped_column(
        String,
        default="MOCK_EHR"
    )


# ----------------------------------------------------- integration operation ---
class IntegrationOperation(Base):
    __tablename__ = "integration_operations"

    id: Mapped[str] = mapped_column(
        String,
        primary_key=True,
        default=uid
    )

    hospital_id: Mapped[str] = mapped_column(
        String,
        index=True
    )

    appointment_id: Mapped[str | None] = mapped_column(
        String,
        index=True,
        nullable=True
    )

    operation: Mapped[str] = mapped_column(
        String
    )  # CREATE | VERIFY | CANCEL | RESCHEDULE

    attempt: Mapped[int] = mapped_column(
        Integer,
        default=1
    )

    # SUCCESS | TIMEOUT | UNKNOWN | ERROR |
    # VALIDATION_ERROR | RATE_LIMITED | OUTAGE
    outcome: Mapped[str] = mapped_column(
        String
    )

    error_class: Mapped[str | None] = mapped_column(
        String,
        nullable=True
    )

    external_id: Mapped[str | None] = mapped_column(
        String,
        nullable=True
    )

    correlation_id: Mapped[str] = mapped_column(
        String,
        index=True
    )

    duration_ms: Mapped[int] = mapped_column(
        Integer,
        default=0
    )

    at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now
    )


# ------------------------------------------------ integration verification ---
class IntegrationVerification(Base):
    __tablename__ = "integration_verifications"

    id: Mapped[str] = mapped_column(
        String,
        primary_key=True,
        default=uid
    )

    appointment_id: Mapped[str] = mapped_column(
        String,
        index=True
    )

    external_id: Mapped[str | None] = mapped_column(
        String,
        nullable=True
    )

    verified: Mapped[bool] = mapped_column(
        Boolean,
        default=False
    )

    detail: Mapped[str] = mapped_column(
        String,
        default=""
    )

    correlation_id: Mapped[str] = mapped_column(
        String,
        index=True
    )

    at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now
    )


# ------------------------------------------------------ reconciliation ---
class ReconciliationRecord(Base):
    __tablename__ = "reconciliation_records"

    id: Mapped[str] = mapped_column(
        String,
        primary_key=True,
        default=uid
    )

    hospital_id: Mapped[str] = mapped_column(
        String,
        index=True
    )

    appointment_id: Mapped[str] = mapped_column(
        String,
        index=True
    )

    reason: Mapped[str] = mapped_column(
        String
    )

    # OPEN | RESOLVED
    status: Mapped[str] = mapped_column(
        String,
        default="OPEN"
    )

    correlation_id: Mapped[str] = mapped_column(
        String,
        index=True
    )

    detail: Mapped[str] = mapped_column(
        Text,
        default=""
    )

    at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now
    )

    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True
    )


# --------------------------------------------------- workflow + messaging ---
class WorkflowExecution(Base):
    __tablename__ = "workflow_executions"

    id: Mapped[str] = mapped_column(
        String,
        primary_key=True,
        default=uid
    )

    hospital_id: Mapped[str] = mapped_column(
        String,
        index=True
    )

    workflow: Mapped[str] = mapped_column(
        String
    )

    appointment_id: Mapped[str | None] = mapped_column(
        String,
        index=True,
        nullable=True
    )

    # SCHEDULED | RUNNING | COMPLETED | FAILED
    state: Mapped[str] = mapped_column(
        String,
        default="SCHEDULED"
    )

    run_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now
    )

    attempts: Mapped[int] = mapped_column(
        Integer,
        default=0
    )

    idempotency_key: Mapped[str] = mapped_column(
        String,
        unique=True
    )

    correlation_id: Mapped[str] = mapped_column(
        String,
        index=True
    )

    history: Mapped[list] = mapped_column(
        JSON,
        default=list
    )

    last_error: Mapped[str | None] = mapped_column(
        String,
        nullable=True
    )


# ------------------------------------------------------------ notifications ---
class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[str] = mapped_column(
        String,
        primary_key=True,
        default=uid
    )

    hospital_id: Mapped[str | None] = mapped_column(
        String,
        index=True,
        nullable=True
    )

    # PATIENT | DOCTOR | HOSPITAL
    recipient_type: Mapped[str] = mapped_column(
        String
    )

    recipient_id: Mapped[str] = mapped_column(
        String,
        index=True
    )

    channel: Mapped[str] = mapped_column(
        String,
        default="SMS"
    )

    template: Mapped[str] = mapped_column(
        String
    )

    body: Mapped[str] = mapped_column(
        Text
    )

    status: Mapped[str] = mapped_column(
        String,
        default="SENT"
    )

    idempotency_key: Mapped[str] = mapped_column(
        String,
        unique=True
    )

    correlation_id: Mapped[str] = mapped_column(
        String,
        index=True
    )

    at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now
    )


# ----------------------------------------------------- operational metrics ---
class OperationalMetric(Base):
    __tablename__ = "operational_metrics"

    id: Mapped[str] = mapped_column(
        String,
        primary_key=True,
        default=uid
    )

    name: Mapped[str] = mapped_column(
        String,
        index=True
    )

    value: Mapped[float] = mapped_column(
        Float,
        default=0
    )

    labels: Mapped[dict] = mapped_column(
        JSON,
        default=dict
    )

    at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now
    )