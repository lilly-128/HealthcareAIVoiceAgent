import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..ai.graph import run_turn
from ..auth import PATIENT, Principal, require_roles
from ..db import get_db
from ..models import AIConversation, Patient
from ..schemas import ChatIn
from ..services.audit import audit, new_correlation_id


log = logging.getLogger("healthcare")

router = APIRouter(
    prefix="/ai",
    tags=["ai"],
)


@router.post("/chat")
def chat(
    body: ChatIn,
    p: Principal = Depends(require_roles(PATIENT)),
    db: Session = Depends(get_db),
):
    """
    Handle one patient AI conversation turn.

    The browser handles speech-to-text and text-to-speech.
    The backend keeps the conversation state in AIConversation.
    """

    # ============================================================
    # 1. Get the logged-in patient's ID
    # ============================================================

    patient_id = getattr(p, "patient_id", None)

    if not patient_id:
        patient = (
            db.query(Patient)
            .filter(Patient.user_id == p.user_id)
            .first()
        )

        if not patient:
            raise HTTPException(
                status_code=400,
                detail="No patient profile found for this account.",
            )

        patient_id = patient.id

    # ============================================================
    # 2. Get existing conversation OR create a new conversation
    # ============================================================

    if body.conversation_id:

        conv = db.get(
            AIConversation,
            body.conversation_id,
        )

        if not conv:
            raise HTTPException(
                status_code=404,
                detail="Conversation not found.",
            )

        # Patient can only access their own conversation
        if conv.patient_id != patient_id:
            raise HTTPException(
                status_code=403,
                detail="Not your conversation.",
            )

        # Make sure old conversations always have usable state
        if conv.messages is None:
            conv.messages = []

        if conv.context is None:
            conv.context = {}

    else:

        # Persistent conversation state
        initial_context = {
            "specialty": None,
            "city": None,
            "hospital_id": None,
            "hospital_name": None,
            "doctor_id": None,
            "doctor_name": None,
            "date": None,
            "time": None,
        }

        conv = AIConversation(
            patient_id=patient_id,
            channel=body.channel or "WEB_VOICE",
            correlation_id=new_correlation_id(),
            messages=[],
            context=initial_context,
            status="ACTIVE",
        )

        db.add(conv)
        db.commit()
        db.refresh(conv)

        # Audit conversation creation
        try:
            audit(
                db,
                actor=p.user_id,
                actor_role=PATIENT,
                action="AI_CONVERSATION_STARTED",
                resource_type="conversation",
                resource_id=conv.id,
                correlation_id=conv.correlation_id,
            )
        except Exception as audit_err:
            log.warning(
                "Audit logging skipped: %s",
                audit_err,
            )

    # ============================================================
    # 3. Validate the message
    # ============================================================

    message = (body.message or "").strip()

    if not message:
        raise HTTPException(
            status_code=400,
            detail="Message cannot be empty.",
        )

    # ============================================================
    # 4. Run the AI turn
    # ============================================================

    try:

        result = run_turn(
            db,
            conv,
            message,
        )

        # ========================================================
        # 5. Persist conversation state
        #
        # run_turn() is responsible for updating:
        #
        # conv.messages
        # conv.context
        #
        # We commit them here so the next request can use them.
        # ========================================================

        db.add(conv)
        db.commit()
        db.refresh(conv)

        return {
            "conversation_id": conv.id,
            **result,
        }

    except HTTPException:
        db.rollback()
        raise

    except Exception as e:

        db.rollback()

        log.error(
            "AI Agent execution error for conversation %s: %s",
            conv.id,
            e,
            exc_info=True,
        )

        raise HTTPException(
            status_code=500,
            detail="AI agent could not process your request.",
        )


# ================================================================
# Get conversation history
# ================================================================

@router.get("/conversations/{conversation_id}")
def get_conversation(
    conversation_id: str,
    p: Principal = Depends(require_roles(PATIENT)),
    db: Session = Depends(get_db),
):
    """
    Return the authenticated patient's conversation history
    and current AI context.
    """

    conv = db.get(
        AIConversation,
        conversation_id,
    )

    if not conv:
        raise HTTPException(
            status_code=404,
            detail="Conversation not found.",
        )

    patient_id = getattr(
        p,
        "patient_id",
        None,
    )

    if not patient_id:
        patient = (
            db.query(Patient)
            .filter(Patient.user_id == p.user_id)
            .first()
        )

        if not patient:
            raise HTTPException(
                status_code=400,
                detail="No patient profile found for this account.",
            )

        patient_id = patient.id

    # Patient can only access their own conversation
    if conv.patient_id != patient_id:
        raise HTTPException(
            status_code=403,
            detail="Not your conversation.",
        )

    return {
        "conversation_id": conv.id,
        "messages": conv.messages or [],
        "context": conv.context or {},
        "correlation_id": conv.correlation_id,
        "status": conv.status,
    }