"""AI session recap via any OpenAI-compatible chat-completions endpoint.

Self-hosted friendly: point TABLECAST_LLM_BASE_URL at Ollama, LM Studio, or
a hosted API. Produces the structured sections the Markdown export and
archive page have placeholders for: recap, bullet highlights, NPCs,
locations, and open threads.
"""

import json
import logging
import re

import httpx
from sqlalchemy.orm import Session

from .. import config, models
from . import export as export_service

log = logging.getLogger("tablecast.recap")

# Keep the source material within a small model's context. When the
# transcript is too long, keep the head and tail (openings and cliffhangers
# matter most) and elide the middle.
MAX_SOURCE_CHARS = 18_000

SYSTEM_PROMPT = (
    "You summarize tabletop RPG sessions for the game group. You are given "
    "scene markers, the text chat (including dice rolls), and a speech "
    "transcript. The transcript is machine-generated and may contain "
    "recognition errors; prefer names as they appear in chat or markers. "
    "Respond with ONLY a JSON object, no code fences, with exactly these "
    "keys: recap (2-4 paragraph story summary, in-world events only), "
    "bullets (5-10 short highlight strings), npcs (non-player characters "
    "who appeared or were discussed), locations (places visited or "
    "discussed), open_threads (unresolved hooks, mysteries, promises). "
    "npcs, locations and open_threads are arrays of short strings. Use an "
    "empty array when a section has nothing."
)


class RecapError(RuntimeError):
    pass


def _source_text(db: Session, game: models.GameSession) -> str:
    events = (
        db.query(models.SessionEvent)
        .filter_by(session_id=game.id)
        .order_by(models.SessionEvent.created_at)
        .all()
    )
    parts: list[str] = [f"Campaign: {game.campaign.name}", f"Session: {game.title}", ""]

    markers = [e for e in events if e.kind == "marker"]
    if markers:
        parts.append("Scene markers:")
        for e in markers:
            payload = json.loads(e.payload)
            note = f" — {payload['note']}" if payload.get("note") else ""
            parts.append(f"- {payload.get('label', 'Marker')}{note}")
        parts.append("")

    chat_lines = []
    for e in events:
        payload = json.loads(e.payload)
        who = e.user.name if e.user else "?"
        if e.kind == "chat":
            chat_lines.append(f"{who}: {payload['text']}")
        elif e.kind == "roll":
            chat_lines.append(f"{who} rolled {payload['expression']} -> {payload['total']}")
    if chat_lines:
        parts += ["Chat log:"] + chat_lines + [""]

    segments = (
        db.query(models.TranscriptSegment)
        .filter_by(session_id=game.id)
        .order_by(models.TranscriptSegment.start_s)
        .all()
    )
    if segments:
        lines = [
            f"{seg['name']}: {seg['text']}"
            for seg in export_service.merged_segments(segments)
        ]
        transcript = "\n".join(lines)
        budget = MAX_SOURCE_CHARS - sum(len(p) + 1 for p in parts)
        if len(transcript) > budget > 0:
            head = transcript[: int(budget * 0.6)]
            tail = transcript[-int(budget * 0.35):]
            transcript = f"{head}\n[… middle of session elided …]\n{tail}"
        parts += ["Transcript:", transcript]

    return "\n".join(parts)


def _parse_payload(raw: str) -> dict:
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Model didn't return clean JSON — keep the text as the recap rather
        # than losing the work.
        return {"recap": raw.strip(), "bullets": [],
                "npcs": [], "locations": [], "open_threads": []}
    return {
        "recap": str(data.get("recap", "")).strip(),
        "bullets": [str(b) for b in data.get("bullets", [])][:12],
        "npcs": [str(n) for n in data.get("npcs", [])][:25],
        "locations": [str(loc) for loc in data.get("locations", [])][:25],
        "open_threads": [str(t) for t in data.get("open_threads", [])][:15],
    }


def generate(db: Session, game: models.GameSession) -> dict:
    if not config.LLM_ENABLED:
        raise RecapError("No LLM configured (set TABLECAST_LLM_BASE_URL and TABLECAST_LLM_MODEL)")

    headers = {"Content-Type": "application/json"}
    if config.LLM_API_KEY:
        headers["Authorization"] = f"Bearer {config.LLM_API_KEY}"

    try:
        response = httpx.post(
            f"{config.LLM_BASE_URL}/chat/completions",
            headers=headers,
            json={
                "model": config.LLM_MODEL,
                "temperature": 0.3,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": _source_text(db, game)},
                ],
            },
            timeout=300,
        )
        response.raise_for_status()
        raw = response.json()["choices"][0]["message"]["content"]
    except httpx.HTTPError as exc:
        raise RecapError(f"LLM request failed: {exc}") from exc
    except (KeyError, IndexError, ValueError) as exc:
        raise RecapError(f"Unexpected LLM response shape: {exc}") from exc

    payload = _parse_payload(raw)
    summary = db.query(models.SessionSummary).filter_by(session_id=game.id).first()
    if summary is None:
        summary = models.SessionSummary(session_id=game.id, model=config.LLM_MODEL, payload="")
        db.add(summary)
    summary.model = config.LLM_MODEL
    summary.payload = json.dumps(payload)
    db.commit()
    log.info("recap generated for session %s via %s", game.id, config.LLM_MODEL)
    return payload


def get_summary(db: Session, session_id: int) -> dict | None:
    row = db.query(models.SessionSummary).filter_by(session_id=session_id).first()
    if row is None:
        return None
    try:
        return json.loads(row.payload)
    except json.JSONDecodeError:
        return None
