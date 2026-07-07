import json
import threading
from datetime import timezone
from typing import Annotated

from fastapi import (
    APIRouter,
    Form,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse

from .. import config, models, security, ws
from ..db import SessionLocal
from ..deps import DbDep, UserDep, require_session_member, templates
from ..services import audio, entities, export, recap

router = APIRouter()

MAX_CHUNK_BYTES = 25 * 1024 * 1024
CHUNK_GRACE_SECONDS = 60


@router.get("/sessions/{session_id}")
def session_page(request: Request, db: DbDep, user: UserDep, session_id: int):
    game, member = require_session_member(db, session_id, user)
    if game.status == "ended":
        return _archive_page(request, db, user, game, member)
    return templates.TemplateResponse(
        request, "room.html",
        {"user": user, "game": game, "campaign": game.campaign,
         "is_gm": member.role == "gm",
         "ice_servers": json.dumps(config.ICE_SERVERS)},
    )


def _archive_page(request, db, user, game, member):
    events = (
        db.query(models.SessionEvent)
        .filter_by(session_id=game.id)
        .order_by(models.SessionEvent.created_at)
        .all()
    )
    segments = (
        db.query(models.TranscriptSegment)
        .filter_by(session_id=game.id)
        .order_by(models.TranscriptSegment.start_s)
        .all()
    )
    recordings = db.query(models.Recording).filter_by(session_id=game.id).all()
    attendees = sorted({e.user.name for e in events if e.user} | {s.user.name for s in segments})
    parsed_events = [
        {"kind": e.kind, "user": e.user.name if e.user else None,
         "at_seconds": e.at_seconds, "payload": json.loads(e.payload)}
        for e in events
    ]
    merged = export.merged_segments(segments)
    speakers = sorted({seg["name"] for seg in merged})

    # Campaign memory: names mentioned this session, with cross-references
    # to the other sessions where they appear. Lazily (re)extract if the
    # transcript exists but no mentions were recorded (e.g. worker finished
    # after the end-of-session refresh).
    mentions = db.query(models.EntityMention).filter_by(session_id=game.id).all()
    if segments and not mentions:
        entities.refresh_session_entities(db, game)
        mentions = db.query(models.EntityMention).filter_by(session_id=game.id).all()
    connections = []
    for m in sorted(mentions, key=lambda m: -m.count):
        others = [
            {"id": o.session.id, "title": o.session.title}
            for o in m.entity.mentions if o.session_id != game.id
        ]
        connections.append({
            "name": m.entity.name, "count": m.count,
            "others": sorted(others, key=lambda s: s["id"]),
        })

    return templates.TemplateResponse(
        request, "archive.html",
        {"user": user, "game": game, "campaign": game.campaign,
         "is_gm": member.role == "gm", "events": parsed_events,
         "segments": merged, "speakers": speakers,
         "recordings": recordings, "attendees": attendees,
         "connections": connections,
         "summary": recap.get_summary(db, game.id),
         "llm_enabled": config.LLM_ENABLED},
    )


@router.post("/sessions/{session_id}/recap")
def generate_recap(db: DbDep, user: UserDep, session_id: int):
    game, member = require_session_member(db, session_id, user)
    if member.role != "gm":
        raise HTTPException(403, "Only the GM can generate recaps")
    if game.status != "ended":
        raise HTTPException(409, "Recaps are generated after the session ends")
    try:
        recap.generate(db, game)
    except recap.RecapError as exc:
        raise HTTPException(502, str(exc))
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


@router.post("/sessions/{session_id}/start")
def start_session(db: DbDep, user: UserDep, session_id: int):
    game, member = require_session_member(db, session_id, user)
    if member.role != "gm":
        raise HTTPException(403, "Only the GM can start the session")
    if game.status == "scheduled":
        game.status = "live"
        game.started_at = models.utcnow()
        db.commit()
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


@router.post("/sessions/{session_id}/end")
async def end_session(db: DbDep, user: UserDep, session_id: int):
    game, member = require_session_member(db, session_id, user)
    if member.role != "gm":
        raise HTTPException(403, "Only the GM can end the session")
    if game.status != "ended":
        game.status = "ended"
        game.recording_active = False
        game.ended_at = models.utcnow()
        db.commit()
        await ws.manager.broadcast(session_id, {"type": "ended"})
        threading.Thread(
            target=audio.finalize_session_audio, args=(session_id,), daemon=True
        ).start()
        threading.Thread(
            target=_refresh_entities_later, args=(session_id,), daemon=True
        ).start()
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


def _refresh_entities_later(session_id: int, delay_s: float = 15.0) -> None:
    """First campaign-memory pass shortly after session end; the worker's
    queue-drain hook re-runs it when late transcripts finish."""
    import time
    time.sleep(delay_s)
    db = SessionLocal()
    try:
        game = db.get(models.GameSession, session_id)
        if game is not None:
            entities.refresh_session_entities(db, game)
    except Exception:
        import logging
        logging.getLogger("tablecast.entities").exception(
            "entity refresh failed for session %s", session_id)
    finally:
        db.close()


@router.post("/sessions/{session_id}/chunks")
async def upload_chunk(
    db: DbDep, user: UserDep, session_id: int,
    file: UploadFile,
    seq: Annotated[int, Form()],
    offset: Annotated[float, Form()],
):
    game, _member = require_session_member(db, session_id, user)
    # Clients flush their last chunk right after the GM ends the session, so
    # accept uploads for a short grace window while finalization is pending.
    in_grace = (
        game.status == "ended"
        and not game.recordings_ready
        and game.ended_at is not None
        and (models.utcnow() - game.ended_at.replace(tzinfo=game.ended_at.tzinfo or timezone.utc)).total_seconds() < CHUNK_GRACE_SECONDS
    )
    if game.status != "live" and not in_grace:
        raise HTTPException(409, "Session is not live")
    data = await file.read()
    if len(data) > MAX_CHUNK_BYTES:
        raise HTTPException(413, "Chunk too large")
    if not data:
        return {"ok": True, "skipped": "empty"}

    chunk_dir = audio.session_audio_dir(session_id) / "chunks" / str(user.id)
    chunk_dir.mkdir(parents=True, exist_ok=True)
    path = chunk_dir / f"{seq:06d}.webm"
    path.write_bytes(data)

    db.add(models.AudioChunk(
        session_id=session_id, user_id=user.id, seq=seq,
        path=str(path), offset_s=max(0.0, offset),
    ))
    db.commit()
    from .internal import pending_count
    await ws.manager.broadcast(session_id, {
        "type": "transcribe_queue", "pending": pending_count(db, session_id),
    })
    return {"ok": True}


@router.get("/sessions/{session_id}/export.md")
def export_markdown(db: DbDep, user: UserDep, session_id: int):
    game, _member = require_session_member(db, session_id, user)
    markdown = export.session_markdown(db, game)
    filename = f"session-{game.id}.md"
    return PlainTextResponse(
        markdown, media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/sessions/{session_id}/recordings/{recording_id}")
def download_recording(db: DbDep, user: UserDep, session_id: int, recording_id: int):
    _game, _member = require_session_member(db, session_id, user)
    recording = db.get(models.Recording, recording_id)
    if recording is None or recording.session_id != session_id:
        raise HTTPException(404, "Recording not found")
    return FileResponse(recording.path, filename=recording.filename)


@router.websocket("/ws/sessions/{session_id}")
async def room_socket(socket: WebSocket, session_id: int):
    token = socket.cookies.get(config.SESSION_COOKIE, "")
    user_id = security.verify_session_token(token)
    if user_id is None:
        await socket.close(code=4401)
        return

    db = SessionLocal()
    try:
        user = db.get(models.User, user_id)
        game = db.get(models.GameSession, session_id)
        member = None
        if user and game:
            member = (
                db.query(models.CampaignMember)
                .filter_by(campaign_id=game.campaign_id, user_id=user.id)
                .first()
            )
        if user is None or game is None or member is None or game.status == "ended":
            await socket.close(code=4403)
            return
        is_gm = member.role == "gm"
    finally:
        db.close()

    await socket.accept()
    try:
        await ws.handle_room_socket(socket, session_id, user, is_gm)
    except WebSocketDisconnect:
        pass
