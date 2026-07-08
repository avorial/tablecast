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


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def _text_page(name: str, html: str, level: int = 1) -> dict:
    """A Foundry VTT v10+ JournalEntryPage of type 'text' (HTML content)."""
    return {
        "name": name,
        "type": "text",
        "title": {"show": True, "level": level},
        "text": {"format": 1, "content": html},  # format 1 = HTML
    }


def campaign_foundry_json(db: Session, campaign: models.Campaign) -> str:
    """Foundry VTT import: an array of JournalEntry documents (v10+), one per
    finished session plus a campaign 'Cast & Places' entry. Import via a
    world's Journal directory or a compendium."""
    sessions = [s for s in campaign.sessions if s.status == "ended"]
    sessions.sort(key=lambda s: s.id)

    entries: list[dict] = []

    for game in sessions:
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
        pages: list[dict] = []

        summary = None
        srow = db.query(models.SessionSummary).filter_by(session_id=game.id).first()
        if srow:
            try:
                summary = json.loads(srow.payload)
            except json.JSONDecodeError:
                summary = None
        if summary and summary.get("recap"):
            recap_html = "".join(
                f"<p>{_html_escape(p)}</p>" for p in summary["recap"].split("\n\n")
            )
            if summary.get("bullets"):
                recap_html += "<ul>" + "".join(
                    f"<li>{_html_escape(b)}</li>" for b in summary["bullets"]
                ) + "</ul>"
            pages.append(_text_page("Recap", recap_html))
            for label, key in (("NPCs", "npcs"), ("Locations", "locations"),
                               ("Open Threads", "open_threads")):
                items = summary.get(key) or []
                if items:
                    html = "<ul>" + "".join(
                        f"<li>{_html_escape(i)}</li>" for i in items) + "</ul>"
                    pages.append(_text_page(label, html, level=2))

        markers = [e for e in events if e.kind == "marker"]
        if markers:
            rows = []
            for e in markers:
                payload = json.loads(e.payload)
                note = f" — {_html_escape(payload['note'])}" if payload.get("note") else ""
                rows.append(f"<li><strong>{_html_escape(payload.get('label', 'Marker'))}"
                            f"</strong>{note}</li>")
            pages.append(_text_page("Scene Markers", "<ul>" + "".join(rows) + "</ul>", level=2))

        if segments:
            body = "".join(
                f"<p><strong>{_html_escape(seg['name'])}:</strong> "
                f"{_html_escape(seg['text'])}</p>"
                for seg in merged_segments(segments)
            )
            pages.append(_text_page("Transcript", body, level=2))

        if not pages:
            pages.append(_text_page("Session", "<p>No content recorded.</p>"))

        entries.append({"name": _session_page_name(game), "pages": pages})

    # Cast & Places entry: one page per entity with its appearances.
    entity_rows = [
        e for e in db.query(models.CampaignEntity).filter_by(campaign_id=campaign.id).all()
        if e.mentions
    ]
    entity_rows.sort(key=lambda e: -sum(m.count for m in e.mentions))
    if entity_rows:
        pages = []
        for entity in entity_rows:
            total = sum(m.count for m in entity.mentions)
            appearances = "".join(
                f"<li>{_html_escape(m.session.title)} — {m.count}×</li>"
                for m in sorted(entity.mentions, key=lambda m: m.session_id)
            )
            html = f"<p>Mentioned {total}× across the campaign.</p><ul>{appearances}</ul>"
            pages.append(_text_page(entity.name, html, level=2))
        entries.append({"name": f"{campaign.name} — Cast & Places", "pages": pages})

    return json.dumps(entries, indent=2)
