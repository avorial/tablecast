"""Heuristic proper-noun extraction — the campaign's memory.

Mines capitalized name runs ("Judith Dumont", "Fort Robespierre", "Port
Sainte Jeanne") out of transcripts and chat, minus player names and common
sentence-starters. Deliberately no ML: it runs anywhere, instantly. Phase 5
replaces the extractor with an LLM behind the same tables.
"""

import json
import logging
import re

from sqlalchemy.orm import Session

from .. import models

log = logging.getLogger("tablecast.entities")

# Capitalized word runs, allowing lowercase connectors inside
# ("Fort Robespierre", "Port Sainte Jeanne", "Order of the Silver Flame").
_NAME_RUN = re.compile(
    r"\b[A-Z][a-z]+(?:(?:\s+(?:of|the|de|du|des|la|le|von|van|al|ibn))*\s+[A-Z][a-z]+)+\b"
    r"|\b[A-Z][a-z]{2,}\b"
)

# Single capitalized words that are almost always sentence grammar, not names.
_STOPWORDS = {
    "The", "This", "That", "There", "Then", "They", "Their", "These", "Those",
    "And", "But", "Also", "After", "Before", "When", "Where", "What", "Who",
    "Why", "How", "Yes", "Yeah", "Okay", "Right", "Well", "Now", "Here",
    "You", "Your", "She", "Him", "Her", "His", "Its", "Our", "Ours", "Mine",
    "Let", "Lets", "Come", "Look", "Wait", "Stop", "Everyone", "Everybody",
    "Something", "Anything", "Nothing", "Maybe", "Just", "Still", "Once",
    "First", "Second", "Next", "Last", "Today", "Tomorrow", "Tonight",
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
    "January", "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December",
}

MIN_SINGLE_WORD_MENTIONS = 3  # single capitalized words need repetition to count
MAX_ENTITIES_PER_SESSION = 60


def _extract_names(texts: list[str], exclude: set[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for text in texts:
        for match in _NAME_RUN.finditer(text):
            name = re.sub(r"\s+", " ", match.group(0).strip())
            if name in exclude or name in _STOPWORDS:
                continue
            counts[name] = counts.get(name, 0) + 1
    # Single words are noisy: require repetition. Multi-word runs are almost
    # always genuine names, keep them at any count.
    return {
        name: n for name, n in counts.items()
        if " " in name or n >= MIN_SINGLE_WORD_MENTIONS
    }


def refresh_session_entities(db: Session, game: models.GameSession) -> int:
    """Recompute this session's entity mentions (idempotent)."""
    texts: list[str] = [
        s.text for s in
        db.query(models.TranscriptSegment).filter_by(session_id=game.id).all()
    ]
    for e in (
        db.query(models.SessionEvent)
        .filter(models.SessionEvent.session_id == game.id,
                models.SessionEvent.kind.in_(("chat", "marker")))
        .all()
    ):
        payload = json.loads(e.payload)
        body = payload.get("text") or " ".join(
            filter(None, [payload.get("label"), payload.get("note")])
        )
        if body:
            texts.append(body)

    exclude = {m.user.name for m in game.campaign.members}
    # also exclude each word of player names ("GM Greta" -> "Greta")
    for name in list(exclude):
        exclude.update(name.split())

    counts = _extract_names(texts, exclude)
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:MAX_ENTITIES_PER_SESSION]

    (
        db.query(models.EntityMention)
        .filter(models.EntityMention.session_id == game.id)
        .delete(synchronize_session=False)
    )
    for name, count in top:
        entity = (
            db.query(models.CampaignEntity)
            .filter_by(campaign_id=game.campaign_id, name=name)
            .first()
        )
        if entity is None:
            entity = models.CampaignEntity(campaign_id=game.campaign_id, name=name)
            db.add(entity)
            db.flush()
        db.add(models.EntityMention(entity_id=entity.id, session_id=game.id, count=count))
    db.commit()
    log.info("session %s: %d entities", game.id, len(top))
    return len(top)


def campaign_glossary(db: Session, campaign_id: int, limit: int = 100) -> list[dict]:
    """Entities across the whole campaign, most-mentioned first."""
    entities = (
        db.query(models.CampaignEntity)
        .filter_by(campaign_id=campaign_id)
        .all()
    )
    out = []
    for entity in entities:
        total = sum(m.count for m in entity.mentions)
        if total == 0:
            continue
        out.append({
            "id": entity.id,
            "name": entity.name,
            "total": total,
            "sessions": sorted(
                ({"id": m.session.id, "title": m.session.title, "count": m.count}
                 for m in entity.mentions),
                key=lambda s: s["id"],
            ),
        })
    out.sort(key=lambda e: -e["total"])
    return out[:limit]


def prompt_names(db: Session, campaign_id: int, limit: int = 15) -> list[str]:
    """Top entity names, fed into whisper's initial_prompt so recurring
    campaign vocabulary transcribes correctly."""
    return [e["name"] for e in campaign_glossary(db, campaign_id, limit=limit)]
