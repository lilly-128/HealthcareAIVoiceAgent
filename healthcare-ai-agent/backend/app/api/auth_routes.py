from fastapi import APIRouter, Depends, HTTPException

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import (
    PATIENT,
    HOSPITAL_ADMIN,
    hash_password,
    make_token,
    verify_password,
)
from ..db import get_db
from ..models import Hospital, Patient, User
from ..schemas import (
    LoginIn,
    HospitalRegisterIn,
    PatientRegisterIn,
    TokenOut,
)
from ..services.audit import audit


router = APIRouter(prefix="/auth", tags=["auth"])


# ---------------------------------------------------------------- login ---
@router.post("/login", response_model=TokenOut)
def login(
    body: LoginIn,
    db: Session = Depends(get_db)
):
    user = db.scalar(
        select(User).where(
            User.email == body.email.lower()
        )
    )

    print("EMAIL:", body.email)
    print("USER FOUND:", user is not None)

    if not user or not verify_password(
        body.password,
        user.password_hash
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials"
        )

    # Hospital Admin can login only after hospital approval
    if (
        user.role == HOSPITAL_ADMIN
        and user.hospital_id
    ):
        hospital = db.get(
            Hospital,
            user.hospital_id
        )

        if hospital and hospital.status != "APPROVED":
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Hospital is not approved yet. "
                    f"Current status: {hospital.status}"
                )
            )

    audit(
        db,
        actor=user.id,
        actor_role=user.role,
        action="LOGIN",
        hospital_id=user.hospital_id
    )

    return TokenOut(
        access_token=make_token(user),
        role=user.role,
        hospital_id=user.hospital_id,
        patient_id=user.patient_id,
        doctor_id=user.doctor_id
    )


# ------------------------------------------------------ hospital register ---
@router.post(
    "/hospitals/register",
    response_model=TokenOut
)
def register_hospital(
    body: HospitalRegisterIn,
    db: Session = Depends(get_db)
):
    """
    Register a hospital and create its Hospital Admin account.

    Hospital starts with SUBMITTED status.

    Flow:

        Hospital Registration
                ↓
             Hospital
          status=SUBMITTED
                ↓
        Hospital Admin User
        role=HOSPITAL_ADMIN
                ↓
        Platform Admin Approval
                ↓
             APPROVED
    """

    # ------------------------------------------------------------
    # Check whether admin email already exists
    # ------------------------------------------------------------
    existing_user = db.scalar(
        select(User).where(
            User.email == body.admin_email.lower()
        )
    )

    if existing_user:
        raise HTTPException(
            status_code=409,
            detail="Admin email already exists"
        )

    try:
        # --------------------------------------------------------
        # 1. Create Hospital
        # --------------------------------------------------------
        hospital = Hospital(
            name=body.hospital_name,
            address=body.address,
            city=body.city,
            phone=body.phone,
            status="SUBMITTED"
        )

        db.add(hospital)

        # Generate hospital.id before creating User
        db.flush()

        # --------------------------------------------------------
        # 2. Create Hospital Admin User
        # --------------------------------------------------------
        admin_user = User(
            email=body.admin_email.lower(),
            password_hash=hash_password(
                body.admin_password
            ),
            role=HOSPITAL_ADMIN,
            hospital_id=hospital.id
        )

        db.add(admin_user)

        # --------------------------------------------------------
        # 3. Create departments
        # --------------------------------------------------------
        from ..models import Department

        for department_name in body.departments:
            department_name = department_name.strip()

            if department_name:
                department = Department(
                    hospital_id=hospital.id,
                    name=department_name
                )

                db.add(department)

        # --------------------------------------------------------
        # 4. Create specialties
        # --------------------------------------------------------
        # Specialty table has unique names.
        # To avoid duplicate-specialty errors, only create a
        # specialty if it does not already exist.
        from ..models import Specialty

        for specialty_name in body.specialties:
            specialty_name = specialty_name.strip()

            if not specialty_name:
                continue

            existing_specialty = db.scalar(
                select(Specialty).where(
                    Specialty.name == specialty_name
                )
            )

            if not existing_specialty:
                specialty = Specialty(
                    name=specialty_name
                )

                db.add(specialty)

        # --------------------------------------------------------
        # 5. Save everything together
        # --------------------------------------------------------
        db.commit()

        # Refresh objects so IDs are available
        db.refresh(hospital)
        db.refresh(admin_user)

        # --------------------------------------------------------
        # 6. Audit registration
        # --------------------------------------------------------
        audit(
            db,
            actor=admin_user.id,
            actor_role=HOSPITAL_ADMIN,
            action="HOSPITAL_REGISTERED",
            hospital_id=hospital.id,
            resource_type="hospital",
            resource_id=hospital.id
        )

        # --------------------------------------------------------
        # IMPORTANT:
        # Do NOT automatically login the Hospital Admin here.
        #
        # Hospital must first be approved by Platform Admin.
        # --------------------------------------------------------
        return TokenOut(
            access_token="",
            role=HOSPITAL_ADMIN,
            hospital_id=hospital.id
        )

    except Exception as e:
        db.rollback()

        raise HTTPException(
            status_code=500,
            detail=f"Hospital registration failed: {str(e)}"
        )


# -------------------------------------------------------- patient register ---
@router.post(
    "/patients/register",
    response_model=TokenOut
)
def register_patient(
    body: PatientRegisterIn,
    db: Session = Depends(get_db)
):
    email = (
        body.email or body.phone
    ).lower()

    # Check existing account
    if db.scalar(
        select(User).where(
            User.email == email
        )
    ):
        raise HTTPException(
            status_code=409,
            detail="Account already exists"
        )

    try:
        # --------------------------------------------------------
        # 1. Create patient
        # --------------------------------------------------------
        patient = Patient(
            name=body.name,
            phone=body.phone,
            email=body.email,
            date_of_birth=body.date_of_birth
        )

        db.add(patient)

        # Generate patient.id
        db.flush()

        # --------------------------------------------------------
        # 2. Create patient login account
        # --------------------------------------------------------
        user = User(
            email=email,
            password_hash=hash_password(
                body.password
            ),
            role=PATIENT,
            patient_id=patient.id
        )

        db.add(user)

        db.commit()

        db.refresh(user)
        db.refresh(patient)

        # --------------------------------------------------------
        # 3. Audit
        # --------------------------------------------------------
        audit(
            db,
            actor=user.id,
            actor_role=PATIENT,
            action="PATIENT_REGISTERED",
            resource_type="patient",
            resource_id=patient.id
        )

        # --------------------------------------------------------
        # 4. Patient can login immediately
        # --------------------------------------------------------
        return TokenOut(
            access_token=make_token(user),
            role=PATIENT,
            patient_id=patient.id
        )

    except Exception as e:
        db.rollback()

        raise HTTPException(
            status_code=500,
            detail=f"Patient registration failed: {str(e)}"
        )