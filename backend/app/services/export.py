"""Markdown export for a finished session — the wiki-ready artifact."""

import json

from sqlalchemy.orm import Session

from .. import models


def _hms(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def session_markdown(db: Session, game: models.GameSession) -> str:
    campaign = game.campaign
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
    attendees = sorted(
        {e.user.name for e in events if e.user}
        | {s.user.name for s in segments}
    )

    date = (game.started_at or game.scheduled_at)
    lines = [
        f"# {game.title}",
        "",
        f"**Campaign:** {campaign.name}  ",
        f"**Date:** {date.date().isoformat() if date else 'unscheduled'}  ",
        f"**Attending:** {', '.join(attendees) if attendees else '—'}",
        "",
        "## Recap",
        "",
        "_(Phase 2: AI-generated recap will land here.)_",
        "",
        "## Important Events",
        "",
    ]

    markers = [e for e in events if e.kind == "marker"]
    if markers:
        for e in markers:
            payload = json.loads(e.payload)
            who = e.user.name if e.user else "GM"
            lines.append(
                f"- `{_hms(e.at_seconds)}` **{payload.get('label', 'Marker')}**"
                + (f" — {payload['note']}" if payload.get("note") else "")
                + f" _({who})_"
            )
    else:
        lines.append("_No scene markers recorded._")

    lines += ["", "## NPCs Introduced", "", "_(Phase 2)_", "",
              "## Locations Visited", "", "_(Phase 2)_", "",
              "## Open Threads", "", "_(Phase 2)_", ""]

    rolls = [e for e in events if e.kind == "roll"]
    if rolls:
        lines += ["## Dice Rolls", ""]
        for e in rolls:
            payload = json.loads(e.payload)
            who = e.user.name if e.user else "?"
            lines.append(
                f"- `{_hms(e.at_seconds)}` {who} rolled **{payload['expression']}** "
                f"→ **{payload['total']}** ({', '.join(map(str, payload['rolls']))})"
            )
        lines.append("")

    chats = [e for e in events if e.kind == "chat"]
    if chats:
        lines += ["## Chat Log", ""]
        for e in chats:
            payload = json.loads(e.payload)
            who = e.user.name if e.user else "?"
            lines.append(f"- **{who}:** {payload['text']}")
        lines.append("")

    lines += ["## Transcript", ""]
    if segments:
        for seg in segments:
            lines.append(f"**[{_hms(seg.start_s)}] {seg.user.name}:** {seg.text.strip()}")
            lines.append("")
    else:
        lines.append("_No transcript available (transcription worker not running?)._")
        lines.append("")

    return "\n".join(lines)
