"""RSS podcast feed for a campaign.

Emits an iTunes-compatible RSS 2.0 feed listing every session that has a
built podcast episode (kind="podcast"), newest first. The feed is public
but unguessable (campaign.feed_token); episode enclosures point at a
token-authenticated media route so the audio is reachable by podcast apps
without a login.
"""

from email.utils import format_datetime
from xml.sax.saxutils import escape, quoteattr

from sqlalchemy.orm import Session

from .. import models
from . import recap as recap_service


def _episode_bytes(path: str) -> int:
    import os
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _duration_hms(seconds: float | None) -> str:
    if not seconds:
        return ""
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _episode_description(db: Session, session_id: int) -> str:
    summary = recap_service.get_summary(db, session_id)
    if summary and summary.get("recap"):
        return summary["recap"]
    return ""


def campaign_feed_xml(db: Session, campaign: models.Campaign, base_url: str) -> str:
    base = base_url.rstrip("/")
    feed_url = f"{base}/feeds/{campaign.feed_token}/podcast.xml"

    # Sessions with a podcast episode, newest first.
    episodes = (
        db.query(models.Recording, models.GameSession)
        .join(models.GameSession, models.Recording.session_id == models.GameSession.id)
        .filter(models.GameSession.campaign_id == campaign.id,
                models.Recording.kind == "podcast")
        .order_by(models.GameSession.id.desc())
        .all()
    )

    items = []
    for rec, game in episodes:
        media_url = f"{base}/feeds/{campaign.feed_token}/episodes/{rec.id}.m4a"
        pub = game.ended_at or game.started_at or game.scheduled_at
        pubdate = format_datetime(pub) if pub else ""
        desc = _episode_description(db, game.id)
        chapters = (
            db.query(models.SessionEvent)
            .filter_by(session_id=game.id, kind="marker")
            .count()
        )
        subtitle = f"{chapters} scene marker(s)" if chapters else ""
        items.append(f"""    <item>
      <title>{escape(game.title)}</title>
      <description>{escape(desc)}</description>
      <itunes:subtitle>{escape(subtitle)}</itunes:subtitle>
      <enclosure url={quoteattr(media_url)} type="audio/mp4" length="{_episode_bytes(rec.path)}"/>
      <guid isPermaLink="false">tablecast-episode-{rec.id}</guid>
      <pubDate>{pubdate}</pubDate>
      <itunes:explicit>false</itunes:explicit>
    </item>""")

    campaign_desc = escape(campaign.description or f"Actual-play sessions of {campaign.name}.")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>{escape(campaign.name)}</title>
    <link>{escape(base)}</link>
    <language>en</language>
    <description>{campaign_desc}</description>
    <itunes:author>Tablecast</itunes:author>
    <itunes:summary>{campaign_desc}</itunes:summary>
    <itunes:explicit>false</itunes:explicit>
    <itunes:category text="Leisure"><itunes:category text="Games"/></itunes:category>
    <atom:link xmlns:atom="http://www.w3.org/2005/Atom" href={quoteattr(feed_url)} rel="self" type="application/rss+xml"/>
{chr(10).join(items)}
  </channel>
</rss>
"""
