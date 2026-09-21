from datetime import date, datetime, time
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------- login ---
class LoginIn(BaseModel):
    email: str
    password: str


# ---------------------------------------------------------------- token ---
class TokenOut(BaseModel):
    access_token: str
    role: str
    hospital_id: str | None = None
    patient_id: str | None = None
    doctor_id: str | None = None


# ---------------------------------------------------------------- AI chat ---
class ChatRequest(BaseModel):
    message: str
    conversation_id: Optional[int] = None
    channel: Optional[str] = "WEB_VOICE"


class ChatIn(BaseModel):
    conversation_id: str | None = None
    message: str
    channel: str = "WEB_VOICE"


# ------------------------------------------------------ hospital register ---
class HospitalRegisterIn(BaseModel):
    # Hospital details
    hospital_name: str
    address: str = ""
    city: str
    phone: str = ""

    departments: list[str] = []
    specialties: list[str] = []

    # Hospital Admin details
    admin_name: str
    admin_email: str
    admin_phone: str = ""
    admin_password: str


# -------------------------------------------------------- doctor register ---
class DoctorRegisterIn(BaseModel):
    # Doctor details
    name: str
    email: str
    phone: str = ""

    specialty: str
    department: str = ""

    qualifications: str = ""
    experience: int = 0
    languages: str = ""

    password: str

    # Hospital details
    hospital_id: str= ""


# ---------------------------------------------------------- doctor details ---
class DoctorIn(BaseModel):
    name: str
    specialty: str
    department: str = ""
    qualification: str = ""
    experience_years: int = 0
    languages: str = "English"
    consultation_minutes: int = 30


# --------------------------------------------------------------- calendar ---
class CalendarIn(BaseModel):
    weekday: int = Field(ge=0, le=6)
    start_time: time
    end_time: time


# ------------------------------------------------------------ blocked time ---
class BlockIn(BaseModel):
    start_at: datetime
    end_at: datetime
    reason: str = "LEAVE"


# --------------------------------------------------------------- patient ---
class PatientRegisterIn(BaseModel):
    name: str
    phone: str
    email: str | None = None
    password: str
    date_of_birth: date | None = None


# ----------------------------------------------------------- appointment ---
class BookIn(BaseModel):
    slot_id: str


class RescheduleIn(BaseModel):
    new_slot_id: str


# --------------------------------------------------------- questionnaire ---
class QuestionnaireIn(BaseModel):
    name: str
    specialty: str | None = None
    questions: list[dict]


class AnswersIn(BaseModel):
    answers: dict