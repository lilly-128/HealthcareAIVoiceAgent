"""
Scheduling service — single source of truth for bookable slots.

Rules:
- Only ACTIVE doctors can have slots.
- Only doctors belonging to APPROVED hospitals can have slots.
- Only ACTIVE calendar entries create slots.
- BOOKED / HELD / BLOCKED slots are never overwritten.
- Availability always comes from the database.
- AI must never invent availability.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import (
    BlockedPeriod,
    Calendar,
    Doctor,
    Hospital,
    Slot,
)


# =============================================================
# TIMEZONE
# =============================================================

APP_TIMEZONE = ZoneInfo("Asia/Kolkata")


def _now() -> datetime:
    """
    Current India-local time.

    Database scheduling uses naive India-local datetimes.
    """

    return datetime.now(APP_TIMEZONE).replace(
        tzinfo=None
    )


def _today() -> date:
    return _now().date()


# =============================================================
# NORMALIZATION
# =============================================================

def _normalize_text(
    value: str | None,
) -> str:

    if not value:
        return ""

    return " ".join(
        value.strip().lower().split()
    )


def _normalize_specialty(
    value: str | None,
) -> str:

    value = _normalize_text(value)

    aliases = {
        "gynecologist": "gynecology",
        "gynaecologist": "gynecology",
        "gynaecology": "gynecology",
        "obgyn": "gynecology",
        "ob gyn": "gynecology",
        "ob-gyn": "gynecology",
        "ob/gyn": "gynecology",

        "cardiologist": "cardiology",

        "dermatologist": "dermatology",

        "neurologist": "neurology",

        "orthopedist": "orthopedics",
        "orthopaedist": "orthopedics",
        "orthopedic": "orthopedics",
        "orthopaedic": "orthopedics",

        "pediatrician": "pediatrics",
        "paediatrician": "pediatrics",

        "psychiatrist": "psychiatry",

        "urologist": "urology",

        "dentist": "dentistry",

        "ophthalmologist": "ophthalmology",
        "eye doctor": "ophthalmology",

        "ent specialist": "ent",
        "ent doctor": "ent",
        "ent specialist doctor": "ent",
    }

    return aliases.get(
        value,
        value,
    )


def _specialty_matches(
    requested: str | None,
    actual: str | None,
) -> bool:

    requested = _normalize_specialty(
        requested
    )

    actual = _normalize_specialty(
        actual
    )

    if not requested or not actual:
        return False

    # City matching is exact after normalization. Substring matching can
    # return doctors from the wrong city.
    return requested == actual


def _city_matches(
    requested: str | None,
    actual: str | None,
) -> bool:

    requested = _normalize_text(
        requested
    )

    actual = _normalize_text(
        actual
    )

    if not requested or not actual:
        return False

    return (
        requested in actual
        or actual in requested
    )


# =============================================================
# GENERATE SLOTS
# =============================================================

def generate_slots(
    db: Session,
    doctor_id: str,
    days: int = 14,
    start_date: datetime | None = None,
) -> int:
    """
    Generate slots from the doctor's active calendar.

    Existing slots are NEVER overwritten.

    Example for a 30-minute consultation:

        Calendar: 14:00 - 17:00

        14:00
        14:30
        15:00
        15:30
        16:00
        16:30
    """

    # =========================================================
    # 1. DOCTOR
    # =========================================================

    doctor = db.get(
        Doctor,
        doctor_id,
    )

    if not doctor:
        return 0

    if doctor.status != "ACTIVE":
        return 0

    # =========================================================
    # 2. HOSPITAL
    # =========================================================

    hospital = db.get(
        Hospital,
        doctor.hospital_id,
    )

    if not hospital:
        return 0

    if hospital.status != "APPROVED":
        return 0

    # =========================================================
    # 3. ACTIVE CALENDARS
    # =========================================================

    calendars = db.scalars(
        select(Calendar).where(
            Calendar.doctor_id == doctor_id,
            Calendar.active.is_(True),
        )
    ).all()

    if not calendars:
        return 0

    # =========================================================
    # 4. BLOCKED PERIODS
    # =========================================================

    blocks = db.scalars(
        select(BlockedPeriod).where(
            BlockedPeriod.doctor_id == doctor_id
        )
    ).all()

    # =========================================================
    # 5. EXISTING SLOTS
    # =========================================================

    existing = {
        slot.start_at
        for slot in db.scalars(
            select(Slot).where(
                Slot.doctor_id == doctor_id
            )
        ).all()
    }

    # =========================================================
    # 6. CONSULTATION DURATION
    # =========================================================

    consultation_minutes = (
        doctor.consultation_minutes
        or settings.SLOT_MINUTES
    )

    if consultation_minutes <= 0:
        return 0

    step = timedelta(
        minutes=consultation_minutes
    )

    # =========================================================
    # 7. FIRST DATE
    # =========================================================

    if start_date is None:

        first_day = _today()

    elif isinstance(start_date, datetime):

        first_day = start_date.date()

    else:

        first_day = start_date

    # =========================================================
    # 8. NUMBER OF DAYS
    # =========================================================

    total_days = max(
        days,
        0,
    )

    if total_days == 0:
        return 0

    # =========================================================
    # 9. GENERATE
    # =========================================================

    created = 0

    for day_offset in range(total_days):

        day = (
            first_day
            + timedelta(
                days=day_offset
            )
        )

        weekday = day.weekday()

        matching_calendars = [
            calendar
            for calendar in calendars
            if calendar.weekday == weekday
        ]

        if not matching_calendars:
            continue

        for calendar in matching_calendars:

            if calendar.start_time is None:
                continue

            if calendar.end_time is None:
                continue

            # -------------------------------------------------
            # Calendar start/end
            # -------------------------------------------------

            cursor = datetime.combine(
                day,
                calendar.start_time,
            )

            end = datetime.combine(
                day,
                calendar.end_time,
            )

            if end <= cursor:
                continue

            # -------------------------------------------------
            # Generate each consultation slot
            # -------------------------------------------------

            while cursor + step <= end:

                slot_end = cursor + step

                # =================================================
                # EXISTING SLOT
                # =================================================

                if cursor in existing:

                    cursor += step
                    continue

                # =================================================
                # BLOCKED PERIOD
                # =================================================

                blocked = any(
                    block.start_at < slot_end
                    and block.end_at > cursor
                    for block in blocks
                )

                # =================================================
                # CREATE SLOT
                # =================================================

                slot = Slot(
                    hospital_id=doctor.hospital_id,
                    doctor_id=doctor_id,
                    start_at=cursor,
                    end_at=slot_end,
                    status=(
                        "BLOCKED"
                        if blocked
                        else "AVAILABLE"
                    ),
                )

                db.add(slot)

                existing.add(
                    cursor
                )

                created += 1

                cursor += step

    # =========================================================
    # SAVE
    # =========================================================

    if created > 0:
        db.commit()

    return created


# =============================================================
# SEARCH DOCTORS
# =============================================================

def search_doctors(
    db: Session,
    specialty: str | None = None,
    city: str | None = None,
    hospital_id: str | None = None,
    limit: int = 10,
) -> list[dict]:

    specialty_value = _normalize_specialty(
        specialty
    )

    city_value = _normalize_text(
        city
    )

    hospital_value = _normalize_text(
        hospital_id
    )

    query = (
        select(
            Doctor,
            Hospital,
        )
        .join(
            Hospital,
            Doctor.hospital_id == Hospital.id,
        )
        .where(
            Doctor.status == "ACTIVE",
            Hospital.status == "APPROVED",
        )
    )

    if hospital_value:

        query = query.where(
            Hospital.id == hospital_value
        )

    if city_value:

        query = query.where(
            func.lower(func.trim(Hospital.city)) == city_value
        )

    rows = db.execute(
        query.order_by(
            Doctor.name
        )
    ).all()

    results = []

    for doctor, hospital in rows:

        if specialty_value:

            if not _specialty_matches(
                specialty_value,
                doctor.specialty,
            ):
                continue

        if city_value:

            if not _city_matches(
                city_value,
                hospital.city,
            ):
                continue

        results.append(
            {
                "doctor_id": doctor.id,
                "doctor_name": doctor.name,
                "name": doctor.name,
                "specialty": doctor.specialty,
                "department": doctor.department,
                "qualification": doctor.qualification,
                "experience_years": doctor.experience_years,
                "languages": doctor.languages,
                "consultation_minutes": (
                    doctor.consultation_minutes
                ),
                "hospital_id": hospital.id,
                "hospital_name": hospital.name,
                "hospital": hospital.name,
                "city": hospital.city,
                "address": hospital.address,
                "phone": hospital.phone,
                "status": doctor.status,
            }
        )

        if len(results) >= limit:
            break

    return results


# =============================================================
# SEARCH HOSPITALS
# =============================================================

def search_hospitals(
    db: Session,
    city: str | None = None,
    specialty: str | None = None,
    limit: int = 10,
) -> list[dict]:

    city_value = _normalize_text(
        city
    )

    specialty_value = _normalize_specialty(
        specialty
    )

    query = select(
        Hospital
    ).where(
        Hospital.status == "APPROVED"
    )

    if city_value:

        query = query.where(
            func.lower(func.trim(Hospital.city)) == city_value
        )

    hospitals = db.scalars(
        query.order_by(
            Hospital.name
        )
    ).all()

    output = []

    for hospital in hospitals:

        if city_value:

            if not _city_matches(
                city_value,
                hospital.city,
            ):
                continue

        doctors = db.scalars(
            select(Doctor).where(
                Doctor.hospital_id == hospital.id,
                Doctor.status == "ACTIVE",
            )
        ).all()

        specialties = sorted(
            {
                doctor.specialty.strip()
                for doctor in doctors
                if doctor.specialty
                and doctor.specialty.strip()
            }
        )

        if specialty_value:

            matching_doctors = [
                doctor
                for doctor in doctors
                if _specialty_matches(
                    specialty_value,
                    doctor.specialty,
                )
            ]

            if not matching_doctors:
                continue

        output.append(
            {
                "hospital_id": hospital.id,
                "name": hospital.name,
                "city": hospital.city,
                "address": hospital.address,
                "phone": hospital.phone,
                "specialties": specialties,
            }
        )

        if len(output) >= limit:
            break

    return output


# =============================================================
# CHECK AVAILABILITY
# =============================================================

def check_availability(
    db: Session,
    doctor_id: str,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = 100,
) -> list[dict]:
    """
    Return only REAL AVAILABLE slots from the database.

    No availability is invented.
    """

    # =========================================================
    # 1. DOCTOR
    # =========================================================

    doctor = db.get(
        Doctor,
        doctor_id,
    )

    if not doctor:
        return []

    if doctor.status != "ACTIVE":
        return []

    # =========================================================
    # 2. HOSPITAL
    # =========================================================

    hospital = db.get(
        Hospital,
        doctor.hospital_id,
    )

    if not hospital:
        return []

    if hospital.status != "APPROVED":
        return []

    # =========================================================
    # 3. LIMIT
    # =========================================================

    limit = max(
        1,
        min(limit, 100),
    )

    # =========================================================
    # 4. REQUESTED WINDOW
    # =========================================================

    now = _now()

    if date_from is None:

        requested_start = datetime.combine(
            _today(),
            datetime.min.time(),
        )

        requested_end = (
            requested_start
            + timedelta(days=1)
        )

    else:

        requested_start = date_from.replace(
            second=0,
            microsecond=0,
        )

        if date_to is not None:

            requested_end = date_to.replace(
                second=0,
                microsecond=0,
            )

        else:

            requested_end = (
                requested_start
                + timedelta(hours=1)
            )

    # =========================================================
    # 5. INVALID WINDOW
    # =========================================================

    if requested_end <= requested_start:
        return []

    # =========================================================
    # 6. PAST WINDOW
    # =========================================================

    if requested_end <= now:
        return []

    # =========================================================
    # 7. QUERY START
    # =========================================================

    if requested_start.date() == now.date():

        query_start = max(
            requested_start,
            now,
        )

    else:

        query_start = requested_start

    # =========================================================
    # 8. GENERATE MISSING SLOTS
    # =========================================================

    generation_start = datetime.combine(
        requested_start.date(),
        datetime.min.time(),
    )

    generation_end = (
        requested_end
        - timedelta(
            microseconds=1
        )
    )

    generation_end_date = (
        generation_end.date()
    )

    generation_days = (
        generation_end_date
        - generation_start.date()
    ).days + 1

    if generation_days > 0:

        generate_slots(
            db=db,
            doctor_id=doctor_id,
            days=generation_days,
            start_date=generation_start,
        )

    # =========================================================
    # 9. QUERY AVAILABLE SLOTS
    # =========================================================

    conditions = [
        Slot.doctor_id == doctor_id,
        Slot.hospital_id == hospital.id,
        Slot.status == "AVAILABLE",
        Slot.start_at >= query_start,
        Slot.start_at < requested_end,
    ]

    slots = db.scalars(
        select(Slot)
        .where(
            and_(*conditions)
        )
        .order_by(
            Slot.start_at
        )
        .limit(limit)
    ).all()

    # =========================================================
    # 10. RETURN DATABASE DATA
    # =========================================================

    return [
        {
            "slot_id": slot.id,
            "start_at": slot.start_at.isoformat(),
            "end_at": slot.end_at.isoformat(),
            "hospital_id": slot.hospital_id,
            "doctor_id": slot.doctor_id,
            "status": slot.status,
        }
        for slot in slots
    ]


# =============================================================
# LOCK SLOT FOR BOOKING
# =============================================================

def lock_slot_for_booking(
    db: Session,
    slot_id: str,
) -> Slot:
    """
    Lock an available slot before booking.

    SELECT FOR UPDATE prevents concurrent booking.
    """

    slot = db.execute(
        select(Slot)
        .where(
            Slot.id == slot_id
        )
        .with_for_update()
    ).scalar_one_or_none()

    if slot is None:

        raise ValueError(
            "SLOT_NOT_FOUND"
        )

    if slot.status != "AVAILABLE":

        raise ValueError(
            "SLOT_NOT_AVAILABLE"
        )

    if slot.start_at < _now():

        raise ValueError(
            "SLOT_IN_PAST"
        )

    # =========================================================
    # DOCTOR
    # =========================================================

    doctor = db.get(
        Doctor,
        slot.doctor_id,
    )

    if not doctor:

        raise ValueError(
            "DOCTOR_NOT_FOUND"
        )

    if doctor.status != "ACTIVE":

        raise ValueError(
            "DOCTOR_NOT_ACTIVE"
        )

    # =========================================================
    # HOSPITAL
    # =========================================================

    hospital = db.get(
        Hospital,
        slot.hospital_id,
    )

    if not hospital:

        raise ValueError(
            "HOSPITAL_NOT_FOUND"
        )

    if hospital.status != "APPROVED":

        raise ValueError(
            "HOSPITAL_NOT_APPROVED"
        )

    # =========================================================
    # CONSISTENCY
    # =========================================================

    if doctor.hospital_id != slot.hospital_id:

        raise ValueError(
            "SLOT_HOSPITAL_MISMATCH"
        )

    return slot


# =============================================================
# RELEASE SLOT
# =============================================================

def release_slot(
    db: Session,
    slot_id: str,
) -> None:
    """
    Release a BOOKED or HELD slot.
    """

    slot = db.get(
        Slot,
        slot_id,
    )

    if not slot:
        return

    if slot.status in (
        "BOOKED",
        "HELD",
    ):

        slot.status = "AVAILABLE"

        db.commit()
