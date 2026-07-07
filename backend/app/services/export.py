"""Markdown export for a finished session — the wiki-ready artifact —
plus the campaign-wide Obsidian vault zip."""

import io
import json
import re
import zipfile

from sqlalchemy.orm import Session

from .. import models


def _hms(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


# Whisper splits sentences across the ~20s chunk seams; stitch consecutive
# segments from the same speaker back together when the gap is small.
STITCH_GAP_S = 2.0


def merged_segments(segments: list[models.TranscriptSegment]) -> list[dict]:
    merged: list[dict] = []
    for seg in segments:
        prev = merged[-1] if merged else None
        if (
            prev is not None
            and prev["user_id"] == seg.user_id
            and seg.start_s - prev["end_s"] <= STITCH_GAP_S
        ):
            prev["text"] = f"{prev['text']} {seg.text.strip()}"
            prev["end_s"] = seg.end_s
        else:
            merged.append({
                "user_id": seg.user_id,
                "name": seg.user.name,
                "start_s": seg.start_s,
                "end_s": seg.end_s,
                "text": seg.text.strip(),
            })
    return merged


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

    summary = None
    summary_row = (
        db.query(models.SessionSummary).filter_by(session_id=game.id).first()
    )
    if summary_row:
        try:
            summary = json.loads(summary_row.payload)
        except json.JSONDecodeError:
            summary = None

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
    ]
    if summary and summary.get("recap"):
        lines += [summary["recap"], ""]
        if summary.get("bullets"):
            lines += [f"- {b}" for b in summary["bullets"]] + [""]
    else:
        lines += ["_(No AI recap yet — configure TABLECAST_LLM_BASE_URL or "
                  "use the Generate recap button.)_", ""]
    lines += [
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

    def _list_section(title: str, key: str) -> list[str]:
        items = (summary or {}).get(key) or []
        body = [f"- {i}" for i in items] if items else ["_None recorded._"]
        return ["", f"## {title}", ""] + body

    lines += _list_section("NPCs Introduced", "npcs")
    lines += _list_section("Locations Visited", "locations")
    lines += _list_section("Open Threads", "open_threads")
    lines += [""]

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

    images = [e for e in events if e.kind == "image"]
    if images:
        lines += ["## Handouts", ""]
        for e in images:
            payload = json.loads(e.payload)
            who = e.user.name if e.user else "?"
            lines.append(f"- {who}: [{payload['filename']}]({payload['url']})")
        lines.append("")

    lines += ["## Transcript", ""]
    if segments:
        for seg in merged_segments(segments):
            lines.append(f"**[{_hms(seg['start_s'])}] {seg['name']}:** {seg['text']}")
            lines.append("")
    else:
        lines.append("_No transcript available (transcription worker not running?)._")
        lines.append("")

    return "\n".join(lines)


def safe_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9 _-]+", "", name).strip() or "untitled"


def _session_page_name(game: models.GameSession) -> str:
    title = safe_filename(game.title)
    if title.lower().startswith("session"):
        return title
    return f"Session {game.id} - {title}"


def campaign_vault_zip(db: Session, campaign: models.Campaign) -> bytes:
    """Obsidian-shaped vault: an index page, one page per session, and one
    page per campaign entity, cross-linked with [[wikilinks]]."""
    sessions = [s for s in campaign.sessions if s.status == "ended"]
    sessions.sort(key=lambda s: s.id)

    entity_rows = (
        db.query(models.CampaignEntity)
        .filter_by(campaign_id=campaign.id)
        .all()
    )
    entity_rows = [e for e in entity_rows if e.mentions]
    entity_rows.sort(key=lambda e: -sum(m.count for m in e.mentions))

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # session pages, with a Names section linking entity pages
        session_names = {}
        for game in sessions:
            page_name = _session_page_name(game)
            session_names[game.id] = page_name
            body = session_markdown(db, game)
            mentioned = [
                m.entity.name
                for m in db.query(models.EntityMention).filter_by(session_id=game.id).all()
            ]
            if mentioned:
                links = " · ".join(f"[[{safe_filename(n)}]]" for n in sorted(mentioned))
                body += f"\n## Names Mentioned\n\n{links}\n"
            zf.writestr(f"Sessions/{page_name}.md", body)

        # entity pages
        for entity in entity_rows:
            lines = [f"# {entity.name}", "", f"Mentioned {sum(m.count for m in entity.mentions)}× across the campaign.", "", "## Appearances", ""]
            for m in sorted(entity.mentions, key=lambda m: m.session_id):
                page = session_names.get(m.session_id)
                if page:
                    lines.append(f"- [[{page}]] — {m.count}×")
            lines.append("")
            zf.writestr(f"Entities/{safe_filename(entity.name)}.md", "\n".join(lines))

        # index page
        idx = [f"# {campaign.name}", ""]
        if campaign.description:
            idx += [campaign.description, ""]
        idx += ["## Sessions", ""]
        idx += [f"- [[{session_names[s.id]}]]" for s in sessions]
        idx += ["", "## Cast & Places", ""]
        idx += [f"- [[{safe_filename(e.name)}]] ({sum(m.count for m in e.mentions)}×)"
                for e in entity_rows[:50]]
        idx.append("")
        zf.writestr(f"{safe_filename(campaign.name)}.md", "\n".join(idx))

    return buf.getvalue()
