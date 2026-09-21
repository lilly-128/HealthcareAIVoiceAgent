"""Core business behaviour: availability, double booking, idempotency,
verification, and the unknown-outcome recovery path.

Run: docker compose exec backend pytest -q
"""
from datetime import datetime, time, timedelta

import pytest
from sqlalchemy import select

from app.db import Base, SessionLocal, engine
from app.integrations.base import IntegrationError
from app.models import Appointment, Calendar, Doctor, Hospital, Patient, Slot
from app.services import appointment as appointment_service
from app.services import scheduling
from app.services.audit import new_correlation_id


@pytest.fixture
def db():
    Base.metadata.create_all(engine)
    s = SessionLocal()
    yield s
    s.close()


@pytest.fixture
def setup(db):
    h = Hospital(name="Test Hospital", city="Testville", status="APPROVED", ehr_enabled=False)
    db.add(h); db.commit()
    d = Doctor(hospital_id=h.id, name="Dr. Test", specialty="Orthopedics", status="ACTIVE")
    db.add(d); db.commit()
    for weekday in range(7):
        db.add(Calendar(hospital_id=h.id, doctor_id=d.id, weekday=weekday,
                        start_time=time(9, 0), end_time=time(12, 0)))
    db.commit()
    scheduling.generate_slots(db, d.id, days=7)
    p = Patient(name="Test Patient", phone="1" + datetime.utcnow().strftime("%H%M%S%f"))
    db.add(p); db.commit()
    return h, d, p


def test_availability_only_returns_open_future_slots(db, setup):
    _, d, _ = setup
    slots = scheduling.check_availability(db, d.id)
    assert slots
    assert all(datetime.fromisoformat(s["start_at"]) > datetime.utcnow() for s in slots)


def test_blocked_slot_is_not_available(db, setup):
    _, d, _ = setup
    slot = db.scalar(select(Slot).where(Slot.doctor_id == d.id, Slot.status == "AVAILABLE"))
    slot.status = "BLOCKED"
    db.commit()
    ids = [s["slot_id"] for s in scheduling.check_availability(db, d.id, limit=50)]
    assert slot.id not in ids


def test_booking_marks_slot_and_confirms(db, setup):
    _, d, p = setup
    slot = scheduling.check_availability(db, d.id)[0]
    result = appointment_service.create_appointment(
        db, patient_id=p.id, slot_id=slot["slot_id"], correlation_id=new_correlation_id())
    assert result["status"] == "CONFIRMED"
    assert db.get(Slot, slot["slot_id"]).status == "BOOKED"


def test_double_booking_is_rejected(db, setup):
    _, d, p = setup
    other = Patient(name="Second", phone="99" + datetime.utcnow().strftime("%H%M%S%f"))
    db.add(other); db.commit()
    slot = scheduling.check_availability(db, d.id)[0]
    appointment_service.create_appointment(
        db, patient_id=p.id, slot_id=slot["slot_id"], correlation_id=new_correlation_id())
    with pytest.raises(appointment_service.BookingError):
        appointment_service.create_appointment(
            db, patient_id=other.id, slot_id=slot["slot_id"],
            correlation_id=new_correlation_id())


def test_same_patient_same_slot_is_idempotent(db, setup):
    _, d, p = setup
    slot = scheduling.check_availability(db, d.id)[0]
    a = appointment_service.create_appointment(
        db, patient_id=p.id, slot_id=slot["slot_id"], correlation_id=new_correlation_id())
    b = appointment_service.create_appointment(
        db, patient_id=p.id, slot_id=slot["slot_id"], correlation_id=new_correlation_id())
    assert a["appointment_id"] == b["appointment_id"]


def test_cancel_releases_the_slot(db, setup):
    _, d, p = setup
    slot = scheduling.check_availability(db, d.id)[0]
    appt = appointment_service.create_appointment(
        db, patient_id=p.id, slot_id=slot["slot_id"], correlation_id=new_correlation_id())
    appointment_service.cancel_appointment(db, appt["appointment_id"], new_correlation_id())
    assert db.get(Slot, slot["slot_id"]).status == "AVAILABLE"


def test_unknown_outcome_verifies_instead_of_duplicating(db, setup, monkeypatch):
    """EHR created the record, then the response timed out. We must find the
    existing record and confirm it, never create a second one."""
    h, d, p = setup
    h.ehr_enabled = True
    db.commit()

    class FlakyConnector:
        created: list[str] = []

        def upsert_patient(self, patient): return "EHR-P-1"
        def find_provider(self, hint): return "EHR-D-1"

        def create_appointment(self, payload, idempotency_key):
            self.created.append(idempotency_key)  # it DID land externally
            raise IntegrationError("TIMEOUT", "response lost")

        def get_appointment(self, external_id):
            from app.integrations.base import ExternalAppointment
            return ExternalAppointment(external_id, "BOOKED", {})

        def find_by_idempotency_key(self, key):
            from app.integrations.base import ExternalAppointment
            if key in self.created:
                return ExternalAppointment("EHR-A-EXISTING", "BOOKED", {})
            return None

    connector = FlakyConnector()
    monkeypatch.setattr(appointment_service, "get_connector", lambda hospital=None: connector)

    slot = scheduling.check_availability(db, d.id)[0]
    result = appointment_service.create_appointment(
        db, patient_id=p.id, slot_id=slot["slot_id"], correlation_id=new_correlation_id())

    assert result["status"] == "CONFIRMED"
    assert result["recovery"] == "timeout_verified_no_duplicate"
    assert len(connector.created) == 1  # exactly one external write attempt
    assert db.scalar(select(Appointment).where(
        Appointment.id == result["appointment_id"])).external_appointment_id == "EHR-A-EXISTING"


def test_unrecoverable_failure_opens_reconciliation(db, setup, monkeypatch):
    h, d, p = setup
    h.ehr_enabled = True
    db.commit()

    class DeadConnector:
        def upsert_patient(self, patient): return "EHR-P-2"
        def find_provider(self, hint): return "EHR-D-2"
        def create_appointment(self, payload, key): raise IntegrationError("OUTAGE", "down")
        def get_appointment(self, external_id): return None
        def find_by_idempotency_key(self, key): return None

    monkeypatch.setattr(appointment_service, "get_connector", lambda hospital=None: DeadConnector())
    slot = scheduling.check_availability(db, d.id)[0]
    result = appointment_service.create_appointment(
        db, patient_id=p.id, slot_id=slot["slot_id"], correlation_id=new_correlation_id())

    assert result["status"] == "RECONCILIATION_REQUIRED"
    assert db.get(Slot, slot["slot_id"]).status == "AVAILABLE"  # slot handed back
