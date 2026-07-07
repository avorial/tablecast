"""Session room WebSocket hub.

One socket per participant. Message types (client → server):
  chat     {text}                      — text chat
  whisper  {to, text}                  — private message to one participant
  roll     {expression}                — dice roll
  marker   {label, note?}              — GM scene marker
  record   {action: "start"|"stop"}    — GM recording control
  moderate {target, action}            — GM mute/unmute/deafen/undeafen a player
  rtc      {to, data}                  — WebRTC signaling relay (opaque)
  state    {muted}                     — presence state updates

Server → client additionally sends: presence, peers, history, transcript,
transcribe_queue, image, error.
"""

import asyncio
import json
import logging
from datetime import timezone

from fastapi import WebSocket
from sqlalchemy.orm import Session

from . import models
from .db import SessionLocal
from .services import dice, search

log = logging.getLogger("tablecast.ws")


class Room:
    def __init__(self, session_id: int):
        self.session_id = session_id
        self.sockets: dict[int, WebSocket] = {}  # user_id -> socket
        self.names: dict[int, str] = {}
        self.muted: dict[int, bool] = {}
        # GM moderation state — persists across the target reconnecting.
        self.force_muted: set[int] = set()
        self.force_deafened: set[int] = set()

    async def broadcast(self, message: dict, exclude: int | None = None) -> None:
        data = json.dumps(message)
        for user_id, socket in list(self.sockets.items()):
            if user_id == exclude:
                continue
            try:
                await socket.send_text(data)
            except Exception:
                self.sockets.pop(user_id, None)

    async def send_to(self, user_id: int, message: dict) -> None:
        socket = self.sockets.get(user_id)
        if socket is None:
            return
        try:
            await socket.send_text(json.dumps(message))
        except Exception:
            self.sockets.pop(user_id, None)

    def presence(self) -> list[dict]:
        return [
            {"user_id": uid, "name": self.names.get(uid, "?"),
             "muted": self.muted.get(uid, False),
             "gm_muted": uid in self.force_muted,
             "gm_deafened": uid in self.force_deafened}
            for uid in self.sockets
        ]


class RoomManager:
    def __init__(self):
        self.rooms: dict[int, Room] = {}
        self.lock = asyncio.Lock()

    async def get(self, session_id: int) -> Room:
        async with self.lock:
            if session_id not in self.rooms:
                self.rooms[session_id] = Room(session_id)
            return self.rooms[session_id]

    async def broadcast(self, session_id: int, message: dict) -> None:
        room = self.rooms.get(session_id)
        if room:
            await room.broadcast(message)


manager = RoomManager()


def _recording_offset(game: models.GameSession) -> float | None:
    if not game.recording_active or game.recording_started_at is None:
        return None
    started = game.recording_started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return (models.utcnow() - started).total_seconds()


def _store_event(
    db: Session, game: models.GameSession, user_id: int | None, kind: str, payload: dict
) -> dict:
    event = models.SessionEvent(
        session_id=game.id,
        user_id=user_id,
        kind=kind,
        payload=json.dumps(payload),
        at_seconds=_recording_offset(game),
    )
    db.add(event)
    if kind in ("chat", "marker"):
        body = payload.get("text") or " ".join(
            filter(None, [payload.get("label"), payload.get("note")])
        )
        speaker = db.get(models.User, user_id).name if user_id else None
        search.index_text(db, game.campaign_id, game.id, kind, speaker, body)
    db.commit()
    return {"at_seconds": event.at_seconds, "created_at": event.created_at.isoformat()}


HISTORY_EVENTS = 300
HISTORY_SEGMENTS = 500


def _history(db: Session, game_id: int, viewer_id: int) -> dict:
    """Recent room activity, replayed to (re)connecting participants so a
    reload or late join doesn't land in a blank room. Whispers are only
    replayed to their sender and recipient."""
    events = (
        db.query(models.SessionEvent)
        .filter(
            models.SessionEvent.session_id == game_id,
            models.SessionEvent.kind.in_(
                ("chat", "whisper", "roll", "marker", "system", "image")
            ),
        )
        .order_by(models.SessionEvent.id.desc())
        .limit(HISTORY_EVENTS)
        .all()
    )
    events = [
        e for e in events
        if e.kind != "whisper"
        or e.user_id == viewer_id
        or json.loads(e.payload).get("to_user_id") == viewer_id
    ]
    segments = (
        db.query(models.TranscriptSegment)
        .filter_by(session_id=game_id)
        .order_by(models.TranscriptSegment.id.desc())
        .limit(HISTORY_SEGMENTS)
        .all()
    )
    return {
        "type": "history",
        "events": [
            {"kind": e.kind, "user_id": e.user_id,
             "name": e.user.name if e.user else None,
             "at_seconds": e.at_seconds, "payload": json.loads(e.payload)}
            for e in reversed(events)
        ],
        "segments": [
            {"user_id": s.user_id, "name": s.user.name,
             "start_s": s.start_s, "end_s": s.end_s, "text": s.text}
            for s in reversed(segments)
        ],
    }


async def handle_room_socket(socket: WebSocket, game_id: int, user: models.User, is_gm: bool):
    room = await manager.get(game_id)
    room.sockets[user.id] = socket
    room.names[user.id] = user.name
    room.muted.setdefault(user.id, False)

    # Tell the newcomer who is already here (they initiate WebRTC offers),
    # replay recent history, then announce them to the room.
    db = SessionLocal()
    try:
        game = db.get(models.GameSession, game_id)
        await socket.send_text(json.dumps({
            "type": "peers",
            "you": user.id,
            "peers": [p for p in room.presence() if p["user_id"] != user.id],
            "recording_active": game.recording_active,
            "recording_started_at": (
                game.recording_started_at.isoformat() if game.recording_started_at else None
            ),
            "gm_muted": user.id in room.force_muted,
            "gm_deafened": user.id in room.force_deafened,
        }))
        await socket.send_text(json.dumps(_history(db, game_id, user.id)))
        # Presence events drive the attendance list — lurkers count too,
        # not just people who typed something.
        _store_event(db, game, user.id, "presence", {"action": "join"})
    finally:
        db.close()
    await room.broadcast(
        {"type": "presence", "action": "join", "user_id": user.id, "name": user.name,
         "peers": room.presence()},
        exclude=user.id,
    )

    try:
        while True:
            raw = await socket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            await _dispatch(socket, room, user, is_gm, msg)
    except Exception:
        pass
    finally:
        room.sockets.pop(user.id, None)
        room.names.pop(user.id, None)
        db = SessionLocal()
        try:
            game = db.get(models.GameSession, game_id)
            if game is not None:
                _store_event(db, game, user.id, "presence", {"action": "leave"})
        finally:
            db.close()
        await room.broadcast(
            {"type": "presence", "action": "leave", "user_id": user.id, "name": user.name,
             "peers": room.presence()}
        )


async def _dispatch(socket: WebSocket, room: Room, user: models.User, is_gm: bool, msg: dict):
    mtype = msg.get("type")

    if mtype == "rtc":
        target = room.sockets.get(msg.get("to"))
        if target:
            await target.send_text(json.dumps(
                {"type": "rtc", "from": user.id, "data": msg.get("data")}
            ))
        return

    if mtype == "state":
        room.muted[user.id] = bool(msg.get("muted"))
        await room.broadcast({"type": "presence", "action": "state", "peers": room.presence()})
        return

    if mtype == "moderate":
        if not is_gm:
            await socket.send_text(json.dumps(
                {"type": "error", "message": "Only the GM can moderate players"}))
            return
        target = msg.get("target")
        action = msg.get("action")
        if target not in room.sockets or target == user.id:
            return
        if action == "mute":
            room.force_muted.add(target)
        elif action == "unmute":
            room.force_muted.discard(target)
        elif action == "deafen":
            room.force_deafened.add(target)
        elif action == "undeafen":
            room.force_deafened.discard(target)
        else:
            return
        await room.broadcast({
            "type": "moderate", "target": target, "action": action,
            "target_name": room.names.get(target, "?"), "by": user.name,
            "peers": room.presence(),
        })
        return

    db = SessionLocal()
    try:
        game = db.get(models.GameSession, room.session_id)
        if game is None or game.status == "ended":
            return

        if mtype == "chat":
            text = str(msg.get("text", "")).strip()[:2000]
            if not text:
                return
            meta = _store_event(db, game, user.id, "chat", {"text": text})
            await room.broadcast({"type": "chat", "user_id": user.id, "name": user.name,
                                  "text": text, **meta})

        elif mtype == "whisper":
            to = msg.get("to")
            text = str(msg.get("text", "")).strip()[:2000]
            if not text or to not in room.sockets or to == user.id:
                await socket.send_text(json.dumps(
                    {"type": "error", "message": "Whisper target not in the room"}))
                return
            to_name = room.names.get(to, "?")
            # Stored (kind=whisper) so it replays for the two participants,
            # but never indexed for search and never exported.
            meta = _store_event(db, game, user.id, "whisper",
                                {"text": text, "to_user_id": to, "to_name": to_name})
            message = {"type": "whisper", "user_id": user.id, "name": user.name,
                       "to_user_id": to, "to_name": to_name, "text": text, **meta}
            await room.send_to(to, message)
            await room.send_to(user.id, message)

        elif mtype == "roll":
            try:
                result = dice.roll(str(msg.get("expression", "")))
            except dice.DiceError as exc:
                await socket.send_text(json.dumps({"type": "error", "message": str(exc)}))
                return
            meta = _store_event(db, game, user.id, "roll", result)
            await room.broadcast({"type": "roll", "user_id": user.id, "name": user.name,
                                  **result, **meta})

        elif mtype == "marker":
            if not is_gm:
                await socket.send_text(json.dumps(
                    {"type": "error", "message": "Only the GM can add scene markers"}))
                return
            payload = {"label": str(msg.get("label", "Marker"))[:80],
                       "note": str(msg.get("note", ""))[:500]}
            meta = _store_event(db, game, user.id, "marker", payload)
            await room.broadcast({"type": "marker", "name": user.name, **payload, **meta})

        elif mtype == "record":
            if not is_gm:
                await socket.send_text(json.dumps(
                    {"type": "error", "message": "Only the GM controls recording"}))
                return
            action = msg.get("action")
            if action == "start" and not game.recording_active:
                game.recording_active = True
                if game.recording_started_at is None:
                    game.recording_started_at = models.utcnow()
                db.commit()
                _store_event(db, game, user.id, "system", {"text": "Recording started"})
                await room.broadcast({
                    "type": "record", "action": "start",
                    "recording_started_at": game.recording_started_at.isoformat(),
                })
            elif action == "stop" and game.recording_active:
                game.recording_active = False
                db.commit()
                _store_event(db, game, user.id, "system", {"text": "Recording stopped"})
                await room.broadcast({"type": "record", "action": "stop"})
    finally:
        db.close()
