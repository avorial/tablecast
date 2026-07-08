"""Public, token-authenticated RSS podcast feed + episode media.

No login: podcast apps authenticate with the unguessable campaign
feed_token in the path. Only finished podcast episodes are exposed.
"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response

from .. import config, models
from ..deps import DbDep
from ..services import feed

router = APIRouter(prefix="/feeds")


def _base_url(request: Request) -> str:
    if config.PUBLIC_BASE_URL:
        return config.PUBLIC_BASE_URL
    # Honor a reverse proxy's forwarded scheme/host when present.
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc))
    return f"{scheme}://{host}"


def _campaign_by_token(db, token: str) -> models.Campaign:
    campaign = db.query(models.Campaign).filter_by(feed_token=token).first()
    if campaign is None:
        raise HTTPException(404, "No such feed")
    return campaign


@router.get("/{token}/podcast.xml")
def podcast_feed(request: Request, db: DbDep, token: str):
    campaign = _campaign_by_token(db, token)
    xml = feed.campaign_feed_xml(db, campaign, _base_url(request))
    return Response(xml, media_type="application/rss+xml")


@router.get("/{token}/episodes/{recording_id}.m4a")
def feed_episode(db: DbDep, token: str, recording_id: int):
    campaign = _campaign_by_token(db, token)
    rec = db.get(models.Recording, recording_id)
    if rec is None or rec.kind != "podcast":
        raise HTTPException(404, "Episode not found")
    game = db.get(models.GameSession, rec.session_id)
    if game is None or game.campaign_id != campaign.id:
        raise HTTPException(404, "Episode not found")
    return FileResponse(rec.path, media_type="audio/mp4", filename=f"{game.title}.m4a")
