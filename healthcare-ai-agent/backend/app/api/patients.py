from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import DOCTOR, PATIENT, Principal, current_principal, require_roles
from ..db import get_db
from ..models import (
    Appointment, Doctor, Hospital, Patient, Questionnaire, QuestionnaireResponse,
    Notification,
)
from ..schemas import AnswersIn, BookIn, RescheduleIn
from ..services import appointment as appointment_service
from ..services import scheduling
from ..services.audit import new_correlation_id
from ..workflows.engine import emit_event

router = APIRouter(tags=["patient"])


# ------------------------------------------------------------- discovery ---
@router.get("/discovery/doctors")
def discover_doctors(specialty: str | None = None, city: str | None = None,
                     db: Session = Depends(get_db)):
    return scheduling.search_doctors(db, specialty=specialty, city=city)


@router.get("/discovery/doctors/{doctor_id}/availability")
def discover_availability(doctor_id: str, db: Session = Depends(get_db)):
    return scheduling.check_availability(db, doctor_id)


# ---------------------------------------------------------- appointments ---
@router.post("/appointments")
def book(body: BookIn, p: Principal = Depends(require_roles(PATIENT)),
         db: Session = Depends(get_db)):
    """Same service the AI calls — the UI gets no shortcuts."""
    try:
        return appointment_service.create_appointment(
            db, patient_id=p.patient_id, slot_id=body.slot_id,
            correlation_id=new_correlation_id(), actor=p.user_id)
    except appointment_service.BookingError as e:
        raise HTTPException(409, str(e))


@router.get("/appointments")
def my_appointments(p: Principal = Depends(require_roles(PATIENT)),
                    db: Session = Depends(get_db)):
    rows = db.scalars(select(Appointment)
                      .where(Appointment.patient_id == p.patient_id)
                      .order_by(Appointment.start_at)).all()
    return [{"id": a.id, "status": a.status, "when": a.start_at.isoformat(),
             "doctor": db.get(Doctor, a.doctor_id).name,
             "hospital": db.get(Hospital, a.hospital_id).name,
             "external_id": a.external_appointment_id,
             "correlation_id": a.correlation_id} for a in rows]


@router.post("/appointments/{appointment_id}/cancel")
def cancel(appointment_id: str, p: Principal = Depends(require_roles(PATIENT)),
           db: Session = Depends(get_db)):
    appt = db.get(Appointment, appointment_id)
    if not appt or appt.patient_id != p.patient_id:
        raise HTTPException(403, "Not your appointment")
    return appointment_service.cancel_appointment(
        db, appointment_id, new_correlation_id(), actor=p.user_id)


@router.post("/appointments/{appointment_id}/reschedule")
def reschedule(appointment_id: str, body: RescheduleIn,
               p: Principal = Depends(require_roles(PATIENT)),
               db: Session = Depends(get_db)):
    appt = db.get(Appointment, appointment_id)
    if not appt or appt.patient_id != p.patient_id:
        raise HTTPException(403, "Not your appointment")
    return appointment_service.reschedule_appointment(
        db, appointment_id, body.new_slot_id, new_correlation_id(), actor=p.user_id)


# --------------------------------------------------------- questionnaire ---
@router.get("/questionnaires/mine")
def my_questionnaires(p: Principal = Depends(require_roles(PATIENT)),
                      db: Session = Depends(get_db)):
    rows = db.scalars(select(QuestionnaireResponse)
                      .where(QuestionnaireResponse.patient_id == p.patient_id)).all()
    out = []
    for r in rows:
        q = db.get(Questionnaire, r.questionnaire_id)
        out.append({"response_id": r.id, "appointment_id": r.appointment_id,
                    "name": q.name, "questions": q.questions,
                    "answers": r.answers, "status": r.status})
    return out


@router.post("/questionnaires/{response_id}")
def answer(response_id: str, body: AnswersIn,
           p: Principal = Depends(require_roles(PATIENT)), db: Session = Depends(get_db)):
    r = db.get(QuestionnaireResponse, response_id)
    if not r or r.patient_id != p.patient_id:
        raise HTTPException(403, "Not your questionnaire")
    merged = dict(r.answers or {})
    merged.update(body.answers)
    r.answers = merged
    q = db.get(Questionnaire, r.questionnaire_id)
    if all(item["key"] in merged for item in q.questions):
        from datetime import datetime
        r.status, r.completed_at = "COMPLETED", datetime.utcnow()
        emit_event(db, "QUESTIONNAIRE_COMPLETED", db.get(Appointment, r.appointment_id))
    db.commit()
    return {"status": r.status}


@router.get("/notifications/mine")
def my_notifications(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    rid = p.patient_id or p.doctor_id or p.hospital_id
    rows = db.scalars(select(Notification)
                      .where(Notification.recipient_id == rid)
                      .order_by(Notification.at.desc()).limit(20)).all()
    return [{"template": n.template, "body": n.body, "at": n.at.isoformat()} for n in rows]


# ------------------------------------------------------------ doctor view ---
@router.get("/doctor/appointments")
def doctor_appointments(p: Principal = Depends(require_roles(DOCTOR)),
                        db: Session = Depends(get_db)):
    rows = db.scalars(select(Appointment)
                      .where(Appointment.doctor_id == p.doctor_id,
                             Appointment.hospital_id == p.hospital_id)
                      .order_by(Appointment.start_at)).all()
    out = []
    for a in rows:
        patient = db.get(Patient, a.patient_id)
        qr = db.scalar(select(QuestionnaireResponse)
                       .where(QuestionnaireResponse.appointment_id == a.id))
        questions = []
        if qr:
            q = db.get(Questionnaire, qr.questionnaire_id)
            questions = [{"text": item["text"], "answer": (qr.answers or {}).get(item["key"])}
                         for item in q.questions]
        out.append({"id": a.id, "when": a.start_at.isoformat(), "status": a.status,
                    "patient": patient.name, "pre_visit": questions,
                    "questionnaire_status": qr.status if qr else "NONE"})
    return out
