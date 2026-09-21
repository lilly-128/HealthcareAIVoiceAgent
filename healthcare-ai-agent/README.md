# Healthcare AI Voice Agent

Multi-tenant patient access platform. Hospitals configure doctors and calendars,
patients talk to a voice agent, the agent books through controlled capabilities,
every booking is verified in an external system before anyone hears the word
"confirmed", and failures leave a recoverable trail.

Stack: PostgreSQL · FastAPI · LangGraph · React (Vite) · Docker.

---

## Run it

```bash
cp .env.example .env          # put your OPENAI_API_KEY in it
docker compose up --build
```

| Service | URL |
|---|---|
| Frontend | http://localhost:5173 |
| Backend API + Swagger | http://localhost:8000/docs |
| Mock EHR | http://localhost:9000/health |
| Postgres | localhost:5432 (postgres/postgres) |

The database schema is created and seeded on first boot. Demo logins, all with
password `demo1234`:

| Role | Email |
|---|---|
| Patient | `lilly@patient.test` |
| Doctor | `doctor11@hospital.test` |
| Hospital admin | `admin1@hospital.test` |
| Platform admin | `admin@platform.test` |

Run the tests:

```bash
docker compose exec backend pytest -q
```

Reset everything:

```bash
docker compose down -v && docker compose up --build
```

---

## Where each API key goes

| Key | Used by | Purpose | Required? |
|---|---|---|---|
| `OPENAI_API_KEY` | `backend/app/ai/graph.py` | The LLM behind the LangGraph agent | Yes |
| `JWT_SECRET` | `backend/app/auth.py` | Signs login tokens | Yes |
| `DATABASE_URL` | `backend/app/config.py` | Postgres connection | Set by compose |
| `EHR_BASE_URL` | `backend/app/integrations/mock_ehr.py` | Mock EHR base URL; point at a real connector later | Set by compose |
| Browser Web Speech API | `frontend/src/pages/Patient.jsx` | Speech-to-text and text-to-speech, no key needed | No |
| `DEEPGRAM_API_KEY` / `ELEVENLABS_API_KEY` | not wired yet | Streaming STT/TTS when you move beyond the browser | Optional |
| `TWILIO_*` | not wired yet | Inbound phone calls | Optional |

Nothing is hardcoded — everything reads from `.env`, and `.env` is gitignored.

---

## Backend API map

| Method | Path | Who | What it does |
|---|---|---|---|
| POST | `/auth/login` | anyone | Token for any role |
| POST | `/auth/patients/register` | anyone | Patient signup |
| POST | `/hospitals/register` | anyone | Hospital applies (status `SUBMITTED`) |
| POST | `/admin/hospitals/{id}/approve` | platform admin | Approve the application |
| POST | `/hospitals/{id}/doctors` | hospital admin | Create a doctor |
| POST | `/hospitals/{id}/doctors/{id}/calendar` | admin/doctor | Set working hours and generate slots |
| POST | `/hospitals/{id}/doctors/{id}/block` | admin/doctor | Block leave or a period |
| GET | `/hospitals/{id}/doctors/{id}/slots` | admin/doctor | Slot board |
| POST | `/hospitals/{id}/questionnaires` | hospital admin | Configure approved pre-visit questions |
| GET | `/discovery/doctors` | anyone | Doctor search |
| GET | `/discovery/doctors/{id}/availability` | anyone | Real open slots |
| POST | `/ai/chat` | patient | One voice/text turn with the agent |
| GET | `/ai/conversations/{id}` | patient | Transcript and context |
| POST | `/appointments` | patient | Book without the AI (same service) |
| GET | `/appointments` | patient | Their appointments |
| POST | `/appointments/{id}/cancel` | patient | Cancel and release the slot |
| POST | `/appointments/{id}/reschedule` | patient | Move to a new slot |
| GET | `/questionnaires/mine` | patient | Assigned questions |
| POST | `/questionnaires/{id}` | patient | Save answers |
| GET | `/notifications/mine` | any | Message log |
| GET | `/doctor/appointments` | doctor | Today, upcoming, pre-visit answers |
| GET | `/admin/overview` | admins | Counts and operational health |
| GET | `/admin/appointments` | admins | Tenant-scoped appointment list |
| GET | `/admin/integration-operations` | admins | Every EHR call, attempt and outcome |
| GET | `/admin/reconciliations` | admins | Escalation queue |
| POST | `/admin/reconciliations/{id}/resolve` | admins | Close one |
| GET | `/admin/audit` | admins | Audit events |
| GET | `/admin/trace/{correlation_id}` | admins | One booking end to end |
| POST | `/admin/workflows/tick` | admins | Run due reminders now |

Mock EHR (separate service): `POST /ehr/patients`, `POST /ehr/providers/search`,
`POST|GET|PUT|DELETE /ehr/appointments`, and `POST /admin/fault` for the failure demo.

---

## How a booking flows

```
patient speech → /ai/chat → LangGraph agent
    → search_doctors        (scheduling service)
    → check_availability    (real slots only)
    → create_appointment    (confirmed_by_patient=true)
         ↓
   lock slot (SELECT FOR UPDATE) → internal PENDING
         ↓
   integration layer → connector → mock EHR
         ↓
   verify_external_appointment  ← the record is re-read, not assumed
         ↓
   synchronize_state → CONFIRMED
         ↓
   workflow engine → confirmation, questionnaire, 24h reminder
```

The agent never reaches the database or the EHR. It calls capabilities in
`backend/app/ai/capabilities.py`, which validate, authorize, execute and audit.

---

## Failure demo (this is the part evaluators look for)

1. Sign in as `admin@platform.test`, open the admin tab.
2. Press **Timeout after create**. The mock EHR will now write the appointment
   and then hang, so the backend gets a timeout with an unknown outcome.
3. Sign in as `lilly@patient.test` and book a slot through the assistant.
4. Watch what happens: the backend classifies the timeout, refuses to retry the
   create, queries the EHR by idempotency key, finds the record, synchronizes,
   and only then confirms. No duplicate appointment.
5. Back in the admin tab, press **Trace** on that appointment to see
   capability → integration attempt → verification → workflow → notification.
6. For the unrecoverable case, press **Outage** and book again: retries are
   exhausted, a reconciliation record opens, the slot is released, and the
   escalation shows in the queue.

`timeout_before_create` gives you the third case — nothing landed externally,
so retrying is safe.

---

## Layout

```
backend/app/
  api/          HTTP routes (auth, hospitals, admin, patients, ai)
  ai/           prompts, capability layer, LangGraph graph
  services/     scheduling, appointment orchestration, audit
  integrations/ connector interface, mock EHR connector, verification
  workflows/    event engine, reminders, notifications
  models.py     all tables
  auth.py       RBAC + tenant isolation
mock-ehr/       separate external system with fault injection
frontend/src/   login shell + patient, doctor, admin pages
docs/           architecture, AI, integration, failure notes
```

---

## Known limitations

- Voice uses the browser's Web Speech API (Chrome/Edge). No barge-in, no
  telephony; the `/ai/chat` contract is designed so a streaming provider drops in.
- The workflow engine is an in-process loop, not Celery or Temporal. Reminders
  fire on a 20-second tick.
- Notifications are stored, not actually sent.
- `Base.metadata.create_all` stands in for Alembic migrations.
- The seed creates 14 days of slots; re-run the calendar endpoint to extend.
