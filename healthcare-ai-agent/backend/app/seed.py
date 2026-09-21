"""
Demo seed — hospitals configure themselves (PRD Option 1), no scraping.

Creates two hospitals, doctors, calendars, slots,
and demo logins for every role.
"""

from datetime import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import (
    DOCTOR,
    HOSPITAL_ADMIN,
    PATIENT,
    PLATFORM_ADMIN,
    hash_password,
)
from .models import Calendar, Doctor, Hospital, Patient, Questionnaire, User
from .services import scheduling


DEMO_PASSWORD = "demo1234"


QUESTIONS = [
    {
        "key": "duration",
        "text": "How long have you had this issue?",
        "type": "SHORT_TEXT",
    },
    {
        "key": "previous_visit",
        "text": "Have you seen a doctor for this before?",
        "type": "YES_NO",
    },
    {
        "key": "current_medication",
        "text": "Are you currently taking any medication for it?",
        "type": "YES_NO",
    },
    {
        "key": "notes",
        "text": "Anything else the doctor should know before the visit?",
        "type": "LONG_TEXT",
    },
]


def _user(
    db: Session,
    email: str,
    role: str,
    **kw,
) -> User:

    existing = db.scalar(
        select(User).where(User.email == email)
    )

    if existing:
        existing.password_hash = hash_password(
            DEMO_PASSWORD
        )
        db.commit()
        return existing

    user = User(
        email=email,
        password_hash=hash_password(DEMO_PASSWORD),
        role=role,
        **kw,
    )

    db.add(user)
    db.commit()

    return user


def seed(db: Session) -> None:

    # =========================================================
    # PLATFORM ADMIN
    # =========================================================

    _user(
        db,
        "admin@platform.test",
        PLATFORM_ADMIN,
    )

    # =========================================================
    # DEMO HOSPITALS + DOCTORS
    # =========================================================

    specs = {

        "City Care Hospital": [
            ("Dr. Rao", "Orthopedics", 12),
            ("Dr. Kumar", "Orthopedics", 8),
            ("Dr. Ravi", "Cardiology", 15),
        ],

        "Harbour General": [
            ("Dr. Priya", "Cardiology", 10),
            ("Dr. Meena", "Dermatology", 6),
        ],
    }

    # =========================================================
    # CREATE HOSPITALS
    # =========================================================

    for idx, (hospital_name, doctors) in enumerate(
        specs.items(),
        start=1,
    ):

        # -----------------------------------------------------
        # Avoid creating duplicate hospitals
        # -----------------------------------------------------

        hospital = db.scalar(
            select(Hospital).where(
                Hospital.name == hospital_name
            )
        )

        if hospital is None:

            hospital = Hospital(
                name=hospital_name,
                city="Visakhapatnam",
                address=f"Block {idx}, Beach Road",
                phone="0891-000000",
                status="APPROVED",
                ehr_enabled=True,
            )

            db.add(hospital)
            db.commit()

        # =====================================================
        # HOSPITAL ADMIN
        # =====================================================

        _user(
            db,
            f"admin{idx}@hospital.test",
            HOSPITAL_ADMIN,
            hospital_id=hospital.id,
        )

        # =====================================================
        # QUESTIONNAIRE
        # =====================================================

        questionnaire = db.scalar(
            select(Questionnaire).where(
                Questionnaire.hospital_id == hospital.id,
                Questionnaire.name == "General pre-visit",
            )
        )

        if questionnaire is None:

            questionnaire = Questionnaire(
                hospital_id=hospital.id,
                name="General pre-visit",
                questions=QUESTIONS,
            )

            db.add(questionnaire)
            db.commit()

        # =====================================================
        # DOCTORS
        # =====================================================

        for doctor_number, (
            doctor_name,
            specialty,
            experience,
        ) in enumerate(
            doctors,
            start=1,
        ):

            # -------------------------------------------------
            # Find existing doctor
            # -------------------------------------------------

            doctor = db.scalar(
                select(Doctor).where(
                    Doctor.hospital_id == hospital.id,
                    Doctor.name == doctor_name,
                )
            )

            # -------------------------------------------------
            # Create doctor
            # -------------------------------------------------

            if doctor is None:

                doctor = Doctor(
                    hospital_id=hospital.id,
                    name=doctor_name,
                    specialty=specialty,
                    department=specialty,
                    experience_years=experience,
                    qualification="MBBS, MD",
                    consultation_minutes=30,
                    status="ACTIVE",
                )

                db.add(doctor)
                db.commit()

            else:

                # Make sure existing demo doctors are usable.
                doctor.status = "ACTIVE"
                doctor.consultation_minutes = 30

                db.commit()

            # =================================================
            # CALENDAR
            # =================================================
            #
            # Monday-Saturday
            #
            # Morning:
            #     09:00 - 13:00
            #
            # Afternoon:
            #     14:00 - 17:00
            #
            # With 30-minute consultations:
            #
            # 14:00
            # 14:30
            # 15:00   <-- available
            # 15:30
            # 16:00
            # 16:30
            #
            # =================================================

            for weekday in range(0, 6):

                # ---------------------------------------------
                # Morning calendar
                # ---------------------------------------------

                morning = db.scalar(
                    select(Calendar).where(
                        Calendar.hospital_id == hospital.id,
                        Calendar.doctor_id == doctor.id,
                        Calendar.weekday == weekday,
                        Calendar.start_time == time(9, 0),
                        Calendar.end_time == time(13, 0),
                    )
                )

                if morning is None:

                    morning = Calendar(
                        hospital_id=hospital.id,
                        doctor_id=doctor.id,
                        weekday=weekday,
                        start_time=time(9, 0),
                        end_time=time(13, 0),
                        active=True,
                    )

                    db.add(morning)

                else:

                    morning.active = True

                # ---------------------------------------------
                # Afternoon calendar
                # ---------------------------------------------

                afternoon = db.scalar(
                    select(Calendar).where(
                        Calendar.hospital_id == hospital.id,
                        Calendar.doctor_id == doctor.id,
                        Calendar.weekday == weekday,
                        Calendar.start_time == time(14, 0),
                        Calendar.end_time == time(17, 0),
                    )
                )

                if afternoon is None:

                    afternoon = Calendar(
                        hospital_id=hospital.id,
                        doctor_id=doctor.id,
                        weekday=weekday,
                        start_time=time(14, 0),
                        end_time=time(17, 0),
                        active=True,
                    )

                    db.add(afternoon)

                else:

                    afternoon.active = True

            db.commit()

            # =================================================
            # GENERATE SLOTS
            # =================================================

            scheduling.generate_slots(
                db,
                doctor.id,
                days=30,
            )

            # =================================================
            # DOCTOR LOGIN
            # =================================================

            _user(
                db,
                f"doctor{idx}{doctor_number}@hospital.test",
                DOCTOR,
                hospital_id=hospital.id,
                doctor_id=doctor.id,
            )

    # =========================================================
    # DEMO PATIENT
    # =========================================================

    patient = db.scalar(
        select(Patient).where(
            Patient.email == "lilly@patient.test"
        )
    )

    if patient is None:

        patient = Patient(
            name="Lilly",
            phone="9876543210",
            email="lilly@patient.test",
            communication_preference="SMS",
        )

        db.add(patient)
        db.commit()

    _user(
        db,
        "lilly@patient.test",
        PATIENT,
        patient_id=patient.id,
    )

    print(
        "Seed complete. All demo passwords:",
        DEMO_PASSWORD,
    )