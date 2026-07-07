"""Internal API for the transcription worker.

Authenticated with a shared token (TABLECAST_WORKER_TOKEN). The worker polls
for pending audio chunks, transcribes them, and posts segments back; the
backend stores them and pushes them to the live room.
"""

import hmac
import logging
import threading
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import config, models, ws
from ..db import SessionLocal
from ..deps import DbDep
from ..services import entities, search

router = APIRouter(prefix="/internal")


def _check_token(token: str | None) -> None:
    if not config.WORKER_TOKEN or not token or not hmac.compare_digest(token, config.WORKER_TOKEN):
        raise HTTPException(401, "Invalid worker token")


WorkerToken = Annotated[str | None, Header(alias="X-Worker-Token")]


def _initial_prompt(db, session_id: int) -> str:
    """Vocabulary hint for whisper: campaign proper nouns transcribe far
    better when they appear in the prompt."""
    game = db.get(models.GameSession, session_id)
    if game is None:
        return ""
    campaign = game.campaign
    names = [m.user.name for m in campaign.members]
    prompt = (
        f"Tabletop RPG session of the campaign {campaign.name}, "
        f"session {game.title}. Players: {', '.join(names)}."
    )
    known = entities.prompt_names(db, campaign.id)
    if known:
        prompt += f" Known names: {', '.join(known)}."
    return prompt[:500]


def pending_count(db, session_id: int) -> int:
    return (
        db.query(models.AudioChunk)
        .filter(
            models.AudioChunk.session_id == session_id,
            models.AudioChunk.transcribe_status.in_(("pending", "processing")),
        )
        .count()
    )


@router.post("/jobs/claim")
def claim_job(db: DbDep, token: WorkerToken = None):
    _check_token(token)
    chunk = (
        db.query(models.AudioChunk)
        .filter_by(transcribe_status="pending")
        .order_by(models.AudioChunk.id)
        .first()
    )
    if chunk is None:
        return {"job": None}
    chunk.transcribe_status = "processing"
    db.commit()
    return {"job": {
        "id": chunk.id,
        "session_id": chunk.session_id,
        "offset_s": chunk.offset_s,
        "initial_prompt": _initial_prompt(db, chunk.session_id),
    }}


@router.get("/jobs/{chunk_id}/audio")
def job_audio(db: DbDep, chunk_id: int, token: WorkerToken = None):
    _check_token(token)
    chunk = db.get(models.AudioChunk, chunk_id)
    if chunk is None:
        raise HTTPException(404, "No such chunk")
    return FileResponse(chunk.path, media_type="audio/webm")


class Segment(BaseModel):
    start: float
    end: float
    text: str


class JobResult(BaseModel):
    status: str  # done | failed
    segments: list[Segment] = []


@router.post("/jobs/{chunk_id}/result")
async def job_result(db: DbDep, chunk_id: int, result: JobResult, token: WorkerToken = None):
    _check_token(token)
    chunk = db.get(models.AudioChunk, chunk_id)
    if chunk is None:
        raise HTTPException(404, "No such chunk")

    chunk.transcribe_status = "done" if result.status == "done" else "failed"
    game = db.get(models.GameSession, chunk.session_id)
    new_segments = []
    for seg in result.segments:
        text = seg.text.strip()
        if not text:
            continue
        row = models.TranscriptSegment(
            session_id=chunk.session_id,
            user_id=chunk.user_id,
            start_s=chunk.offset_s + seg.start,
            end_s=chunk.offset_s + seg.end,
            text=text,
        )
        db.add(row)
        new_segments.append(row)
        search.index_text(db, game.campaign_id, chunk.session_id, "transcript",
                          chunk.user.name, text)
    db.commit()

    # Once an ended session's queue drains, refresh its campaign-memory
    # entities in the background.
    if game.status == "ended" and pending_count(db, chunk.session_id) == 0:
        threading.Thread(
            target=_refresh_entities_bg, args=(chunk.session_id,), daemon=True
        ).start()

    if new_segments:
        user = db.get(models.User, chunk.user_id)
        await ws.manager.broadcast(chunk.session_id, {
            "type": "transcript",
            "segments": [
                {"user_id": s.user_id, "name": user.name if user else "?",
                 "start_s": s.start_s, "end_s": s.end_s, "text": s.text}
                for s in new_segments
            ],
        })
    await ws.manager.broadcast(chunk.session_id, {
        "type": "transcribe_queue", "pending": pending_count(db, chunk.session_id),
    })
    return {"ok": True}


def _refresh_entities_bg(session_id: int) -> None:
    db = SessionLocal()
    try:
        game = db.get(models.GameSession, session_id)
        if game is not None:
            entities.refresh_session_entities(db, game)
    except Exception:
        logging.getLogger("tablecast.entities").exception(
            "entity refresh failed for session %s", session_id)
    finally:
        db.close()


@router.post("/jobs/requeue-stale")
def requeue_stale(db: DbDep, token: WorkerToken = None):
    """Worker calls this on startup to recover chunks stuck in 'processing'."""
    _check_token(token)
    count = (
        db.query(models.AudioChunk)
        .filter_by(transcribe_status="processing")
        .update({"transcribe_status": "pending"})
    )
    db.commit()
    return {"requeued": count}
