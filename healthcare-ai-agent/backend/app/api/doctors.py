from datetime import time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import (
    PLATFORM_ADMIN,
    HOSPITAL_ADMIN,
    DOCTOR,
    PATIENT,
    current_principal,
    hash_password,
)
from ..db import get_db
from ..models import Doctor, Hospital, User, Calendar
from ..schemas import DoctorRegisterIn
from ..services import scheduling


router = APIRouter(
    prefix="/doctors",
    tags=["doctors"]
)


# ==========================================================
# REGISTER DOCTOR
# ==========================================================

@router.post("/register")
def register_doctor(
    body: DoctorRegisterIn,
    db: Session = Depends(get_db),
    current_user=Depends(current_principal)
):
    """
    Platform Admin:
        Can register a doctor for ANY hospital.

    Hospital Admin:
        Can register a doctor only for THEIR OWN hospital.

    After doctor registration:
        1. Doctor is created
        2. Doctor login account is created
        3. Default calendar is created
        4. Real availability slots are generated
    """

    # ------------------------------------------------------
    # 1. Only Platform Admin or Hospital Admin
    # ------------------------------------------------------

    if current_user.role not in (
        PLATFORM_ADMIN,
        HOSPITAL_ADMIN
    ):
        raise HTTPException(
            status_code=403,
            detail=(
                "Only Platform Admin or Hospital Admin "
                "can register doctors"
            )
        )

    # ------------------------------------------------------
    # 2. Check hospital exists
    # ------------------------------------------------------

    hospital = db.get(
        Hospital,
        body.hospital_id
    )

    if not hospital:
        raise HTTPException(
            status_code=404,
            detail="Hospital not found"
        )

    # ------------------------------------------------------
    # 3. Hospital Admin can only register doctors
    #    for their own hospital
    # ------------------------------------------------------

    if current_user.role == HOSPITAL_ADMIN:

        if not current_user.hospital_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Hospital Admin is not linked "
                    "to a hospital"
                )
            )

        if body.hospital_id != current_user.hospital_id:
            raise HTTPException(
                status_code=403,
                detail=(
                    "You cannot register a doctor "
                    "for another hospital"
                )
            )

    # ------------------------------------------------------
    # 4. Hospital must be approved
    # ------------------------------------------------------

    if hospital.status != "APPROVED":
        raise HTTPException(
            status_code=403,
            detail=(
                f"Hospital is not approved. "
                f"Current status: {hospital.status}"
            )
        )

    # ------------------------------------------------------
    # 5. Clean doctor email
    # ------------------------------------------------------

    email = body.email.lower().strip()

    # ------------------------------------------------------
    # 6. Check whether email already exists
    # ------------------------------------------------------

    existing_user = db.scalar(
        select(User).where(
            User.email == email
        )
    )

    if existing_user:
        raise HTTPException(
            status_code=409,
            detail="Doctor email already exists"
        )

    try:

        # ==================================================
        # 7. Create Doctor
        # ==================================================

        doctor = Doctor(
            hospital_id=body.hospital_id,
            name=body.name,
            specialty=body.specialty,
            department=body.department,
            qualification=body.qualifications,
            experience_years=body.experience,
            languages=body.languages or "English",
            consultation_minutes=30,
            status="ACTIVE"
        )

        db.add(doctor)

        # Generate doctor.id
        db.flush()

        # ==================================================
        # 8. Create Doctor Login Account
        # ==================================================

        doctor_user = User(
            email=email,
            password_hash=hash_password(
                body.password
            ),
            role=DOCTOR,
            hospital_id=body.hospital_id,
            doctor_id=doctor.id
        )

        db.add(doctor_user)

        # ==================================================
        # 9. Create Default Doctor Calendar
        #
        # Monday-Saturday:
        # Morning   = 09:00 - 13:00
        # Afternoon = 14:00 - 17:00
        #
        # Calendar.weekday:
        # 0 = Monday
        # 1 = Tuesday
        # ...
        # 5 = Saturday
        # ==================================================

        for weekday in range(0, 6):

            # Morning calendar
            db.add(
                Calendar(
                    hospital_id=doctor.hospital_id,
                    doctor_id=doctor.id,
                    weekday=weekday,
                    start_time=time(9, 0),
                    end_time=time(13, 0),
                    active=True
                )
            )

            # Afternoon calendar
            db.add(
                Calendar(
                    hospital_id=doctor.hospital_id,
                    doctor_id=doctor.id,
                    weekday=weekday,
                    start_time=time(14, 0),
                    end_time=time(17, 0),
                    active=True
                )
            )

        # Make sure Calendar rows are available
        # before generating slots.
        db.flush()

        # ==================================================
        # 10. Generate REAL Availability Slots
        #
        # scheduling.generate_slots() reads the calendar
        # and creates actual AVAILABLE Slot rows.
        # ==================================================

        slots_generated = scheduling.generate_slots(
            db,
            doctor.id
        )

        # ==================================================
        # 11. Save Everything
        # ==================================================

        db.commit()

        db.refresh(doctor)
        db.refresh(doctor_user)

        # ==================================================
        # 12. Return Success
        # ==================================================

        return {
            "message": "Doctor registered successfully",
            "doctor_id": doctor.id,
            "hospital_id": doctor.hospital_id,
            "name": doctor.name,
            "email": doctor_user.email,
            "specialty": doctor.specialty,
            "department": doctor.department,
            "status": doctor.status,
            "slots_generated": slots_generated
        }

    # ------------------------------------------------------
    # Handle HTTP errors
    # ------------------------------------------------------

    except HTTPException:
        db.rollback()
        raise

    # ------------------------------------------------------
    # Handle unexpected errors
    # ------------------------------------------------------

    except Exception as e:
        db.rollback()

        raise HTTPException(
            status_code=500,
            detail=f"Doctor registration failed: {str(e)}"
        )


# ==========================================================
# LIST / SEARCH DOCTORS
# ==========================================================

@router.get("")
def list_doctors(
    specialty: str | None = None,
    hospital_id: str | None = None,
    db: Session = Depends(get_db),
    current_user=Depends(current_principal)
):
    """
    Platform Admin:
        Can see doctors from any hospital.

    Hospital Admin:
        Can see only doctors from their own hospital.

    Patient:
        Can see active doctors and search by specialty.

    Doctor:
        Cannot use this endpoint.
    """

    # ------------------------------------------------------
    # 1. Start with ACTIVE doctors
    # ------------------------------------------------------

    query = select(Doctor).where(
        Doctor.status == "ACTIVE"
    )

    # ------------------------------------------------------
    # 2. Hospital Admin
    # ------------------------------------------------------

    if current_user.role == HOSPITAL_ADMIN:

        if not current_user.hospital_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Hospital Admin is not linked "
                    "to a hospital"
                )
            )

        query = query.where(
            Doctor.hospital_id ==
            current_user.hospital_id
        )

    # ------------------------------------------------------
    # 3. Platform Admin
    # ------------------------------------------------------

    elif current_user.role == PLATFORM_ADMIN:

        if hospital_id:
            query = query.where(
                Doctor.hospital_id == hospital_id
            )

    # ------------------------------------------------------
    # 4. Patient
    # ------------------------------------------------------

    elif current_user.role == PATIENT:

        if hospital_id:
            query = query.where(
                Doctor.hospital_id == hospital_id
            )

    # ------------------------------------------------------
    # 5. Other roles are not allowed
    # ------------------------------------------------------

    else:
        raise HTTPException(
            status_code=403,
            detail="You are not allowed to view doctors"
        )

    # ------------------------------------------------------
    # 6. Filter by specialty
    # ------------------------------------------------------

    if specialty and specialty.strip():

        query = query.where(
            Doctor.specialty.ilike(
                f"%{specialty.strip()}%"
            )
        )

    # ------------------------------------------------------
    # 7. Get doctors
    # ------------------------------------------------------

    doctors = db.scalars(
        query.order_by(Doctor.name)
    ).all()

    # ------------------------------------------------------
    # 8. Return doctors
    # ------------------------------------------------------

    return [
        {
            "id": doctor.id,
            "name": doctor.name,
            "specialty": doctor.specialty,
            "department": doctor.department,
            "qualification": doctor.qualification,
            "experience_years": doctor.experience_years,
            "languages": doctor.languages,
            "consultation_minutes":
                doctor.consultation_minutes,
            "status": doctor.status,
            "hospital_id": doctor.hospital_id
        }
        for doctor in doctors
    ]


# ==========================================================
# GET DOCTOR BY ID
# ==========================================================

@router.get("/{doctor_id}")
def get_doctor(
    doctor_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(current_principal)
):
    """
    Hospital Admin can view a doctor only if that doctor
    belongs to the admin's hospital.
    """

    # ------------------------------------------------------
    # 1. Only Hospital Admin
    # ------------------------------------------------------

    if current_user.role != HOSPITAL_ADMIN:
        raise HTTPException(
            status_code=403,
            detail=(
                "Only Hospital Admin can "
                "view doctor details"
            )
        )

    # ------------------------------------------------------
    # 2. Hospital Admin must belong to a hospital
    # ------------------------------------------------------

    if not current_user.hospital_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "Hospital Admin is not linked "
                "to a hospital"
            )
        )

    # ------------------------------------------------------
    # 3. Find doctor inside this hospital
    # ------------------------------------------------------

    doctor = db.scalar(
        select(Doctor).where(
            Doctor.id == doctor_id,
            Doctor.hospital_id ==
            current_user.hospital_id
        )
    )

    if not doctor:
        raise HTTPException(
            status_code=404,
            detail="Doctor not found in your hospital"
        )

    # ------------------------------------------------------
    # 4. Return doctor
    # ------------------------------------------------------

    return {
        "id": doctor.id,
        "name": doctor.name,
        "specialty": doctor.specialty,
        "department": doctor.department,
        "qualification": doctor.qualification,
        "experience_years":
            doctor.experience_years,
        "languages": doctor.languages,
        "consultation_minutes":
            doctor.consultation_minutes,
        "status": doctor.status,
        "hospital_id": doctor.hospital_id
    }


# ==========================================================
# DEACTIVATE DOCTOR
# ==========================================================

@router.patch("/{doctor_id}/deactivate")
def deactivate_doctor(
    doctor_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(current_principal)
):
    """
    Hospital Admin can deactivate only their own
    hospital's doctor.
    """

    # ------------------------------------------------------
    # 1. Only Hospital Admin
    # ------------------------------------------------------

    if current_user.role != HOSPITAL_ADMIN:
        raise HTTPException(
            status_code=403,
            detail=(
                "Only Hospital Admin can "
                "deactivate doctors"
            )
        )

    # ------------------------------------------------------
    # 2. Hospital Admin must belong to a hospital
    # ------------------------------------------------------

    if not current_user.hospital_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "Hospital Admin is not linked "
                "to a hospital"
            )
        )

    # ------------------------------------------------------
    # 3. Find doctor inside this hospital
    # ------------------------------------------------------

    doctor = db.scalar(
        select(Doctor).where(
            Doctor.id == doctor_id,
            Doctor.hospital_id ==
            current_user.hospital_id
        )
    )

    if not doctor:
        raise HTTPException(
            status_code=404,
            detail="Doctor not found in your hospital"
        )

    # ------------------------------------------------------
    # 4. Deactivate doctor
    # ------------------------------------------------------

    doctor.status = "INACTIVE"

    db.commit()
    db.refresh(doctor)

    # ------------------------------------------------------
    # 5. Return success
    # ------------------------------------------------------

    return {
        "message": "Doctor deactivated successfully",
        "doctor_id": doctor.id,
        "status": doctor.status
    }