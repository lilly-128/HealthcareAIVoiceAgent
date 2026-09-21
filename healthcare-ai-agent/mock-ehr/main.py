"""Mock EHR / hospital information system.

Separate service on purpose: it has its own IDs, its own store, and its own
failure modes. Fault injection lives here so the backend's recovery path can be
demonstrated without touching backend code.

Fault modes (POST /admin/fault {"mode": "..."}):
  none               normal behaviour
  timeout_after_create  record IS created, then the response hangs → unknown outcome
  timeout_before_create the request hangs and nothing is created → safe retry
  outage             500 on every write
  slow               2s delay, still succeeds
"""
import time
import uuid
from datetime import datetime

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

app = FastAPI(title="Mock EHR", version="1.0")

APPOINTMENTS: dict[str, dict] = {}
BY_IDEMPOTENCY: dict[str, str] = {}
PATIENTS: dict[str, dict] = {}
PROVIDERS: dict[str, dict] = {}

FAULT = {"mode": "none", "remaining": 0}


class Fault(BaseModel):
    mode: str = "none"
    remaining: int = 1  # how many requests the fault applies to


def _consume_fault() -> str:
    if FAULT["remaining"] <= 0:
        return "none"
    FAULT["remaining"] -= 1
    return FAULT["mode"]


@app.get("/health")
def health():
    return {"status": "ok", "appointments": len(APPOINTMENTS), "fault": FAULT}


@app.post("/admin/fault")
def set_fault(body: Fault):
    FAULT["mode"], FAULT["remaining"] = body.mode, body.remaining
    return FAULT


@app.post("/ehr/patients")
async def upsert_patient(request: Request):
    body = await request.json()
    for pid, p in PATIENTS.items():
        if p.get("phone") == body.get("phone"):
            return {"external_patient_id": pid}
    pid = "EHR-P-" + uuid.uuid4().hex[:6].upper()
    PATIENTS[pid] = body
    return {"external_patient_id": pid}


@app.post("/ehr/providers/search")
async def find_provider(request: Request):
    body = await request.json()
    for pid, p in PROVIDERS.items():
        if p.get("name") == body.get("name"):
            return {"external_provider_id": pid}
    pid = "EHR-D-" + uuid.uuid4().hex[:6].upper()
    PROVIDERS[pid] = body
    return {"external_provider_id": pid}


@app.post("/ehr/appointments")
async def create_appointment(request: Request,
                             idempotency_key: str | None = Header(None, alias="Idempotency-Key")):
    body = await request.json()
    mode = _consume_fault()

    if mode == "timeout_before_create":
        time.sleep(30)  # client times out first; nothing was written
        raise HTTPException(504, "gateway timeout")
    if mode == "outage":
        raise HTTPException(503, "EHR unavailable")
    if mode == "slow":
        time.sleep(2)

    if idempotency_key and idempotency_key in BY_IDEMPOTENCY:
        return APPOINTMENTS[BY_IDEMPOTENCY[idempotency_key]]

    # Double-booking guard on the external side too.
    for a in APPOINTMENTS.values():
        if (a["provider_id"] == body["provider_id"]
                and a["start_at"] == body["start_at"] and a["status"] != "CANCELLED"):
            raise HTTPException(409, "provider already booked at that time")

    ext_id = "EHR-A-" + uuid.uuid4().hex[:6].upper()
    record = {
        "external_appointment_id": ext_id, "status": "BOOKED",
        "created_at": datetime.utcnow().isoformat(), **body,
    }
    APPOINTMENTS[ext_id] = record
    if idempotency_key:
        BY_IDEMPOTENCY[idempotency_key] = ext_id

    if mode == "timeout_after_create":
        # The record exists, but the caller will never see this response.
        time.sleep(30)
    return record


@app.get("/ehr/appointments")
def find_by_key(idempotency_key: str | None = None):
    if idempotency_key and idempotency_key in BY_IDEMPOTENCY:
        return APPOINTMENTS[BY_IDEMPOTENCY[idempotency_key]]
    return {}


@app.get("/ehr/appointments/{external_id}")
def get_appointment(external_id: str):
    return APPOINTMENTS.get(external_id, {})


@app.put("/ehr/appointments/{external_id}")
async def update_appointment(external_id: str, request: Request):
    body = await request.json()
    if external_id not in APPOINTMENTS:
        raise HTTPException(404, "not found")
    APPOINTMENTS[external_id].update(body)
    APPOINTMENTS[external_id]["status"] = "RESCHEDULED"
    return APPOINTMENTS[external_id]


@app.delete("/ehr/appointments/{external_id}")
def cancel_appointment(external_id: str):
    if external_id not in APPOINTMENTS:
        raise HTTPException(404, "not found")
    APPOINTMENTS[external_id]["status"] = "CANCELLED"
    return APPOINTMENTS[external_id]
