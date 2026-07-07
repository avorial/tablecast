"""Full-text search across transcripts, chat, and markers (SQLite FTS5).

The index is maintained at the write points (segment inserts, chat/marker
events) rather than with triggers, so it stays visible in Python. On a
non-SQLite database everything degrades to a LIKE scan.
"""

import json
import logging

from markupsafe import escape
from sqlalchemy import text
from sqlalchemy.orm import Session

from .. import models
from ..db import engine

log = logging.getLogger("tablecast.search")

_FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS fts_content USING fts5(
    body,
    kind UNINDEXED,
    campaign_id UNINDEXED,
    session_id UNINDEXED,
    speaker UNINDEXED
)
"""


def is_sqlite() -> bool:
    return engine.dialect.name == "sqlite"


def create_index() -> None:
    if not is_sqlite():
        log.info("non-SQLite database: full-text search uses LIKE fallback")
        return
    with engine.begin() as conn:
        conn.execute(text(_FTS_DDL))


def index_text(
    db: Session, campaign_id: int, session_id: int, kind: str, speaker: str | None, body: str
) -> None:
    if not is_sqlite() or not body.strip():
        return
    db.execute(
        text(
            "INSERT INTO fts_content (body, kind, campaign_id, session_id, speaker) "
            "VALUES (:body, :kind, :campaign_id, :session_id, :speaker)"
        ),
        {"body": body, "kind": kind, "campaign_id": campaign_id,
         "session_id": session_id, "speaker": speaker or ""},
    )


def _fts_query(q: str) -> str:
    # Quote each term so user input can't inject FTS5 syntax; terms are
    # implicitly ANDed, so "judith fort" matches rows containing both.
    terms = [t.replace('"', "") for t in q.split() if t.replace('"', "")]
    return " ".join(f'"{t}"' for t in terms)


def search(db: Session, campaign_id: int, q: str, limit: int = 60) -> list[dict]:
    q = q.strip()
    if not q:
        return []
    if is_sqlite():
        # Sentinel characters mark the highlights; the body is HTML-escaped
        # BEFORE the sentinels become <mark> tags, so user content can't
        # smuggle markup into the results page.
        rows = db.execute(
            text(
                "SELECT kind, session_id, speaker, "
                "snippet(fts_content, 0, char(1), char(2), '…', 18) AS snip "
                "FROM fts_content "
                "WHERE fts_content MATCH :q AND campaign_id = :cid "
                "ORDER BY rank LIMIT :limit"
            ),
            {"q": _fts_query(q), "cid": campaign_id, "limit": limit},
        ).mappings().all()
        out = []
        for r in rows:
            snip = str(escape(r["snip"])).replace("\x01", "<mark>").replace("\x02", "</mark>")
            out.append({**dict(r), "snip": snip})
        return out

    # LIKE fallback for non-SQLite deployments.
    like = f"%{q}%"
    segs = (
        db.query(models.TranscriptSegment)
        .join(models.GameSession, models.TranscriptSegment.session_id == models.GameSession.id)
        .filter(models.GameSession.campaign_id == campaign_id,
                models.TranscriptSegment.text.ilike(like))
        .limit(limit)
        .all()
    )
    return [{"kind": "transcript", "session_id": s.session_id,
             "speaker": s.user.name, "snip": str(escape(s.text[:200]))} for s in segs]


def rebuild_if_empty() -> None:
    """Backfill the index for deployments that predate FTS."""
    if not is_sqlite():
        return
    from ..db import SessionLocal

    db = SessionLocal()
    try:
        count = db.execute(text("SELECT count(*) FROM fts_content")).scalar()
        if count:
            return
        segments = db.query(models.TranscriptSegment).all()
        events = (
            db.query(models.SessionEvent)
            .filter(models.SessionEvent.kind.in_(("chat", "marker")))
            .all()
        )
        if not segments and not events:
            return
        log.info("backfilling FTS index: %d segments, %d events", len(segments), len(events))
        for seg in segments:
            game = db.get(models.GameSession, seg.session_id)
            index_text(db, game.campaign_id, seg.session_id, "transcript",
                       seg.user.name, seg.text)
        for e in events:
            payload = json.loads(e.payload)
            body = payload.get("text") or " ".join(
                filter(None, [payload.get("label"), payload.get("note")])
            )
            game = db.get(models.GameSession, e.session_id)
            index_text(db, game.campaign_id, e.session_id, e.kind,
                       e.user.name if e.user else None, body)
        db.commit()
    finally:
        db.close()
