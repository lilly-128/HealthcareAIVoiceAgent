import asyncio
import contextlib
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import admin, ai_routes, auth_routes, hospitals, patients, doctors
from .config import settings
from .db import Base, SessionLocal, engine
from .seed import seed
from .workflows.engine import tick

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("healthcare")

app = FastAPI(title="Healthcare AI Voice Agent", version="1.0")

# Updated CORS configuration
origins = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://healthcare-ai-voice-agent-two.vercel.app",
    ],
    allow_origin_regex=r"https://[a-zA-Z0-9-]+-portfolio-a1d5beeb\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for r in (
    auth_routes.router,
    hospitals.router,
    admin.router,
    patients.router,
    ai_routes.router,
    doctors.router,
):
    app.include_router(r)


@app.get("/health")
def health():
    return {"status": "ok"}


async def _scheduler():
    """Runs due workflows every 20s so reminders fire without a queue broker."""
    while True:
        await asyncio.sleep(20)

        db = SessionLocal()

        try:
            ran = tick(db)

            if ran:
                log.info("workflow tick executed %s", ran)

        except Exception as e:
            log.warning("workflow tick failed: %s", e)

        finally:
            db.close()


@app.on_event("startup")
async def startup():
    Base.metadata.create_all(engine)

    if settings.SEED_ON_START:
        db = SessionLocal()

        try:
            seed(db)

        finally:
            db.close()

    app.state.scheduler = asyncio.create_task(_scheduler())


@app.on_event("shutdown")
async def shutdown():
    task = getattr(app.state, "scheduler", None)

    if task:
        task.cancel()

        with contextlib.suppress(asyncio.CancelledError):
            await task
