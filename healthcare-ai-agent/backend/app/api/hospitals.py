from fastapi import APIRouter, Depends, HTTPException

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import (
    DOCTOR,
    HOSPITAL_ADMIN,
    PLATFORM_ADMIN,
    Principal,
    current_principal,
    require_hospital_access,
    require_roles,
)
from ..db import get_db
from ..models import (
    BlockedPeriod,
    Calendar,
    Doctor,
    Hospital,
    Questionnaire,
    Slot,
)
from ..schemas import (
    BlockIn,
    CalendarIn,
    QuestionnaireIn,
)
from ..services import scheduling
from ..services.audit import audit


router = APIRouter(
    prefix="/hospitals",
    tags=["hospitals"]
)


# --------------------------------------------------------- list hospitals ---
@router.get("")
def list_hospitals(
    p: Principal = Depends(current_principal),
    db: Session = Depends(get_db)
):
    """
    Platform Admin can see all hospitals.

    Hospital Admin can see only their own hospital.
    """

    q = select(Hospital)

    if p.role != PLATFORM_ADMIN:
        if not p.hospital_id:
            raise HTTPException(
                status_code=400,
                detail="User is not linked to a hospital"
            )

        q = q.where(
            Hospital.id == p.hospital_id
        )

    hospitals = db.scalars(q).all()

    return [
        {
            "id": h.id,
            "name": h.name,
            "address": h.address,
            "city": h.city,
            "phone": h.phone,
            "status": h.status,
            "ehr_enabled": h.ehr_enabled,
            "external_facility_id": h.external_facility_id,
        }
        for h in hospitals
    ]


# --------------------------------------------------------- hospital details ---
@router.get("/{hospital_id}")
def get_hospital(
    hospital_id: str,
    p: Principal = Depends(current_principal),
    db: Session = Depends(get_db)
):
    """
    Get hospital details.

    Platform Admin can access any hospital.
    Hospital Admin can access only their own hospital.
    """

    require_hospital_access(
        p,
        hospital_id
    )

    hospital = db.get(
        Hospital,
        hospital_id
    )

    if not hospital:
        raise HTTPException(
            status_code=404,
            detail="Hospital not found"
        )

    return {
        "id": hospital.id,
        "name": hospital.name,
        "address": hospital.address,
        "city": hospital.city,
        "phone": hospital.phone,
        "status": hospital.status,
        "ehr_enabled": hospital.ehr_enabled,
        "external_facility_id": hospital.external_facility_id,
    }


# ---------------------------------------------------------- hospital doctors ---
@router.get("/{hospital_id}/doctors")
def list_hospital_doctors(
    hospital_id: str,
    p: Principal = Depends(current_principal),
    db: Session = Depends(get_db)
):
    """
    List doctors belonging to a hospital.

    Hospital Admin can see only doctors from their hospital.
    Platform Admin can see doctors from any hospital.
    """

    require_hospital_access(
        p,
        hospital_id
    )

    doctors = db.scalars(
        select(Doctor)
        .where(
            Doctor.hospital_id == hospital_id
        )
        .order_by(Doctor.name)
    ).all()

    return [
        {
            "id": doctor.id,
            "name": doctor.name,
            "specialty": doctor.specialty,
            "department": doctor.department,
            "qualification": doctor.qualification,
            "experience_years": doctor.experience_years,
            "languages": doctor.languages,
            "consultation_minutes": doctor.consultation_minutes,
            "status": doctor.status,
            "hospital_id": doctor.hospital_id,
        }
        for doctor in doctors
    ]


# ----------------------------------------------------------- set calendar ---
@router.post("/{hospital_id}/doctors/{doctor_id}/calendar")
def set_calendar(
    hospital_id: str,
    doctor_id: str,
    entries: list[CalendarIn],
    p: Principal = Depends(
        require_roles(
            HOSPITAL_ADMIN,
            DOCTOR,
            PLATFORM_ADMIN
        )
    ),
    db: Session = Depends(get_db)
):
    """
    Create/update a doctor's weekly calendar.

    Hospital Admin:
        Can manage doctors in their hospital.

    Doctor:
        Can manage only their own calendar.

    Platform Admin:
        Can manage any hospital.
    """

    require_hospital_access(
        p,
        hospital_id
    )

    # --------------------------------------------------------
    # Check doctor belongs to this hospital
    # --------------------------------------------------------
    doctor = db.scalar(
        select(Doctor).where(
            Doctor.id == doctor_id,
            Doctor.hospital_id == hospital_id
        )
    )

    if not doctor:
        raise HTTPException(
            status_code=404,
            detail="Doctor not found in this hospital"
        )

    # --------------------------------------------------------
    # Doctor can modify only own calendar
    # --------------------------------------------------------
    if p.role == DOCTOR and p.doctor_id != doctor_id:
        raise HTTPException(
            status_code=403,
            detail="Doctors manage only their own calendar"
        )

    # --------------------------------------------------------
    # Hospital must be approved
    # --------------------------------------------------------
    hospital = db.get(
        Hospital,
        hospital_id
    )

    if not hospital:
        raise HTTPException(
            status_code=404,
            detail="Hospital not found"
        )

    if hospital.status != "APPROVED":
        raise HTTPException(
            status_code=403,
            detail="Hospital must be approved before managing availability"
        )

    # --------------------------------------------------------
    # Delete old calendar
    # --------------------------------------------------------
    db.query(Calendar).filter(
        Calendar.hospital_id == hospital_id,
        Calendar.doctor_id == doctor_id
    ).delete()

    # --------------------------------------------------------
    # Add new calendar entries
    # --------------------------------------------------------
    for entry in entries:
        db.add(
            Calendar(
                hospital_id=hospital_id,
                doctor_id=doctor_id,
                **entry.model_dump()
            )
        )

    db.commit()

    # --------------------------------------------------------
    # Generate real bookable slots
    # --------------------------------------------------------
    created = scheduling.generate_slots(
        db,
        doctor_id
    )

    # --------------------------------------------------------
    # Audit
    # --------------------------------------------------------
    audit(
        db,
        actor=p.user_id,
        actor_role=p.role,
        action="CALENDAR_UPDATED",
        hospital_id=hospital_id,
        resource_type="doctor",
        resource_id=doctor_id,
        detail=f"{created} slots generated"
    )

    return {
        "calendar_entries": len(entries),
        "slots_generated": created
    }


# ----------------------------------------------------------- block period ---
@router.post("/{hospital_id}/doctors/{doctor_id}/block")
def block_period(
    hospital_id: str,
    doctor_id: str,
    body: BlockIn,
    p: Principal = Depends(
        require_roles(
            HOSPITAL_ADMIN,
            DOCTOR,
            PLATFORM_ADMIN
        )
    ),
    db: Session = Depends(get_db)
):
    """
    Block a doctor's availability.

    Example:
        Doctor leave
        Lunch break
        Holiday
        Emergency block
    """

    require_hospital_access(
        p,
        hospital_id
    )

    # --------------------------------------------------------
    # Check doctor belongs to hospital
    # --------------------------------------------------------
    doctor = db.scalar(
        select(Doctor).where(
            Doctor.id == doctor_id,
            Doctor.hospital_id == hospital_id
        )
    )

    if not doctor:
        raise HTTPException(
            status_code=404,
            detail="Doctor not found in this hospital"
        )

    # --------------------------------------------------------
    # Doctor can block only own availability
    # --------------------------------------------------------
    if p.role == DOCTOR and p.doctor_id != doctor_id:
        raise HTTPException(
            status_code=403,
            detail="Doctors can block only their own availability"
        )

    # --------------------------------------------------------
    # Validate time
    # --------------------------------------------------------
    if body.end_at <= body.start_at:
        raise HTTPException(
            status_code=400,
            detail="End time must be after start time"
        )

    # --------------------------------------------------------
    # Create blocked period
    # --------------------------------------------------------
    blocked = BlockedPeriod(
        hospital_id=hospital_id,
        doctor_id=doctor_id,
        **body.model_dump()
    )

    db.add(blocked)

    # --------------------------------------------------------
    # Block available slots affected by this period
    # --------------------------------------------------------
    affected = db.scalars(
        select(Slot).where(
            Slot.hospital_id == hospital_id,
            Slot.doctor_id == doctor_id,
            Slot.start_at >= body.start_at,
            Slot.start_at < body.end_at,
            Slot.status == "AVAILABLE"
        )
    ).all()

    for slot in affected:
        slot.status = "BLOCKED"

    db.commit()

    # --------------------------------------------------------
    # Audit
    # --------------------------------------------------------
    audit(
        db,
        actor=p.user_id,
        actor_role=p.role,
        action="DOCTOR_AVAILABILITY_BLOCKED",
        hospital_id=hospital_id,
        resource_type="doctor",
        resource_id=doctor_id,
        detail=f"{len(affected)} slots blocked"
    )

    return {
        "blocked_slots": len(affected)
    }


# ------------------------------------------------------------- doctor slots ---
@router.get("/{hospital_id}/doctors/{doctor_id}/slots")
def doctor_slots(
    hospital_id: str,
    doctor_id: str,
    p: Principal = Depends(current_principal),
    db: Session = Depends(get_db)
):
    """
    Get the materialised slots for a doctor.

    Only slots belonging to the requested hospital and doctor
    are returned.
    """

    require_hospital_access(
        p,
        hospital_id
    )

    # --------------------------------------------------------
    # Check doctor belongs to hospital
    # --------------------------------------------------------
    doctor = db.scalar(
        select(Doctor).where(
            Doctor.id == doctor_id,
            Doctor.hospital_id == hospital_id
        )
    )

    if not doctor:
        raise HTTPException(
            status_code=404,
            detail="Doctor not found in this hospital"
        )

    # --------------------------------------------------------
    # Get slots
    # --------------------------------------------------------
    slots = db.scalars(
        select(Slot)
        .where(
            Slot.hospital_id == hospital_id,
            Slot.doctor_id == doctor_id
        )
        .order_by(Slot.start_at)
        .limit(80)
    ).all()

    return [
        {
            "slot_id": slot.id,
            "start_at": slot.start_at.isoformat(),
            "end_at": slot.end_at.isoformat(),
            "status": slot.status,
            "appointment_type": slot.appointment_type,
        }
        for slot in slots
    ]


# ------------------------------------------------------ questionnaire create ---
@router.post("/{hospital_id}/questionnaires")
def create_questionnaire(
    hospital_id: str,
    body: QuestionnaireIn,
    p: Principal = Depends(
        require_roles(
            HOSPITAL_ADMIN,
            PLATFORM_ADMIN
        )
    ),
    db: Session = Depends(get_db)
):
    """
    Create a questionnaire for a hospital.
    """

    require_hospital_access(
        p,
        hospital_id
    )

    # --------------------------------------------------------
    # Hospital must exist
    # --------------------------------------------------------
    hospital = db.get(
        Hospital,
        hospital_id
    )

    if not hospital:
        raise HTTPException(
            status_code=404,
            detail="Hospital not found"
        )

    # --------------------------------------------------------
    # Hospital must be approved
    # --------------------------------------------------------
    if hospital.status != "APPROVED":
        raise HTTPException(
            status_code=403,
            detail="Hospital must be approved before creating questionnaires"
        )

    questionnaire = Questionnaire(
        hospital_id=hospital_id,
        **body.model_dump()
    )

    db.add(questionnaire)
    db.commit()

    audit(
        db,
        actor=p.user_id,
        actor_role=p.role,
        action="QUESTIONNAIRE_CREATED",
        hospital_id=hospital_id,
        resource_type="questionnaire",
        resource_id=questionnaire.id
    )

    return {
        "questionnaire_id": questionnaire.id
    }