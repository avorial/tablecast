import json
import secrets
import threading
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response

from .. import config, models
from ..db import SessionLocal
from ..deps import DbDep, UserDep, require_member, templates
from ..services import audio, craig_import, entities, export, github_export
from ..services import search as search_service

MAX_CRAIG_ZIP_BYTES = 600 * 1024 * 1024

router = APIRouter()


def _base_url(request: Request) -> str:
    if config.PUBLIC_BASE_URL:
        return config.PUBLIC_BASE_URL
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc))
    return f"{scheme}://{host}"


@router.get("/")
def dashboard(request: Request, db: DbDep, user: UserDep):
    memberships = (
        db.query(models.CampaignMember)
        .filter_by(user_id=user.id)
        .join(models.Campaign)
        .order_by(models.Campaign.created_at.desc())
        .all()
    )
    return templates.TemplateResponse(
        request, "dashboard.html", {"user": user, "memberships": memberships}
    )


@router.post("/campaigns")
def create_campaign(
    db: DbDep, user: UserDep,
    name: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
):
    name = name.strip()[:120]
    if not name:
        raise HTTPException(400, "Campaign name required")
    campaign = models.Campaign(name=name, description=description.strip(), gm_id=user.id)
    db.add(campaign)
    db.flush()
    db.add(models.CampaignMember(campaign_id=campaign.id, user_id=user.id, role="gm"))
    db.commit()
    return RedirectResponse(f"/campaigns/{campaign.id}", status_code=303)


@router.post("/campaigns/join")
def join_campaign(db: DbDep, user: UserDep, join_code: Annotated[str, Form()]):
    campaign = db.query(models.Campaign).filter_by(join_code=join_code.strip()).first()
    if campaign is None:
        raise HTTPException(404, "No campaign with that invite code")
    existing = (
        db.query(models.CampaignMember)
        .filter_by(campaign_id=campaign.id, user_id=user.id)
        .first()
    )
    if existing is None:
        db.add(models.CampaignMember(campaign_id=campaign.id, user_id=user.id, role="player"))
        db.commit()
    return RedirectResponse(f"/campaigns/{campaign.id}", status_code=303)


@router.get("/campaigns/{campaign_id}")
def campaign_page(request: Request, db: DbDep, user: UserDep, campaign_id: int):
    campaign, member = require_member(db, campaign_id, user)
    sessions = (
        db.query(models.GameSession)
        .filter_by(campaign_id=campaign.id)
        .order_by(models.GameSession.id.desc())
        .all()
    )
    upcoming = [s for s in sessions if s.status in ("scheduled", "live")]
    past = [s for s in sessions if s.status == "ended"]

    glossary = entities.campaign_glossary(db, campaign.id, limit=30)

    # Timeline: past sessions in play order with their scene markers.
    timeline = []
    for s in reversed(past):
        markers = (
            db.query(models.SessionEvent)
            .filter_by(session_id=s.id, kind="marker")
            .order_by(models.SessionEvent.id)
            .all()
        )
        timeline.append({
            "session": s,
            "markers": [json.loads(m.payload) for m in markers],
        })

    feed_url = f"{_base_url(request)}/feeds/{campaign.feed_token}/podcast.xml"
    gh = db.query(models.GithubExport).filter_by(campaign_id=campaign.id).first()
    return templates.TemplateResponse(
        request, "campaign.html",
        {"user": user, "campaign": campaign, "member": member,
         "upcoming": upcoming, "past": past,
         "glossary": glossary, "timeline": timeline, "feed_url": feed_url,
         "github": gh},
    )


@router.get("/campaigns/{campaign_id}/search")
def search_campaign(request: Request, db: DbDep, user: UserDep, campaign_id: int, q: str = ""):
    campaign, _member = require_member(db, campaign_id, user)
    results = search_service.search(db, campaign_id, q)
    # attach session titles for display
    titles = {
        s.id: s.title for s in
        db.query(models.GameSession).filter_by(campaign_id=campaign_id).all()
    }
    grouped: dict[int, list[dict]] = {}
    for r in results:
        grouped.setdefault(int(r["session_id"]), []).append(r)
    return templates.TemplateResponse(
        request, "search.html",
        {"user": user, "campaign": campaign, "q": q,
         "grouped": grouped, "titles": titles, "total": len(results)},
    )


@router.get("/campaigns/{campaign_id}/export.zip")
def export_vault(db: DbDep, user: UserDep, campaign_id: int):
    campaign, _member = require_member(db, campaign_id, user)
    payload = export.campaign_vault_zip(db, campaign)
    filename = f"{export.safe_filename(campaign.name)}-vault.zip"
    return Response(
        payload, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/campaigns/{campaign_id}/foundry.json")
def export_foundry(db: DbDep, user: UserDep, campaign_id: int):
    campaign, _member = require_member(db, campaign_id, user)
    payload = export.campaign_foundry_json(db, campaign)
    filename = f"{export.safe_filename(campaign.name)}-foundry.json"
    return Response(
        payload, media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/campaigns/{campaign_id}/feed/rotate")
def rotate_feed(db: DbDep, user: UserDep, campaign_id: int):
    campaign, member = require_member(db, campaign_id, user)
    if member.role != "gm":
        raise HTTPException(403, "Only the GM can rotate the feed URL")
    campaign.feed_token = secrets.token_urlsafe(18)
    db.commit()
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


@router.post("/campaigns/{campaign_id}/import/craig")
async def import_craig(
    db: DbDep, user: UserDep, campaign_id: int,
    file: UploadFile,
    title: Annotated[str, Form()],
    date: Annotated[str, Form()] = "",
):
    campaign, member = require_member(db, campaign_id, user)
    if member.role != "gm":
        raise HTTPException(403, "Only the GM can import recordings")
    data = await file.read()
    if len(data) > MAX_CRAIG_ZIP_BYTES:
        raise HTTPException(413, "Zip too large")
    started_at = None
    if date:
        try:
            started_at = datetime.fromisoformat(date)
        except ValueError:
            raise HTTPException(400, "Invalid date")
    try:
        result = craig_import.import_craig_zip(
            db, campaign, title, data, started_at=started_at
        )
    except craig_import.CraigImportError as exc:
        raise HTTPException(400, str(exc))

    sid = result["session_id"]
    # Build aligned tracks + mixdown now; the worker transcribes the chunks
    # and the queue-drain hook then extracts entities + recap.
    threading.Thread(
        target=audio.finalize_session_audio, args=(sid, 0), daemon=True
    ).start()
    skipped = result["skipped"]
    suffix = f"?imported={len(result['matched'])}"
    if skipped:
        suffix += f"&skipped={len(skipped)}"
    return RedirectResponse(f"/sessions/{sid}{suffix}", status_code=303)


@router.post("/campaigns/{campaign_id}/github/config")
def configure_github(
    db: DbDep, user: UserDep, campaign_id: int,
    repo: Annotated[str, Form()],
    token: Annotated[str, Form()] = "",
    branch: Annotated[str, Form()] = "main",
    path_prefix: Annotated[str, Form()] = "",
    api_base: Annotated[str, Form()] = "https://api.github.com",
):
    _campaign, member = require_member(db, campaign_id, user)
    if member.role != "gm":
        raise HTTPException(403, "Only the GM can configure GitHub export")
    repo = repo.strip()
    if repo.count("/") != 1:
        raise HTTPException(400, "Repo must be in owner/name form")
    cfg = db.query(models.GithubExport).filter_by(campaign_id=campaign_id).first()
    if cfg is None:
        cfg = models.GithubExport(campaign_id=campaign_id, repo=repo, token=token)
        db.add(cfg)
    cfg.repo = repo
    cfg.branch = branch.strip() or "main"
    cfg.path_prefix = path_prefix.strip()
    cfg.api_base = api_base.strip() or "https://api.github.com"
    # Keep the existing token if the field was left blank on re-save.
    if token.strip():
        cfg.token = token.strip()
    db.commit()
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


@router.post("/campaigns/{campaign_id}/github/push")
def push_github(db: DbDep, user: UserDep, campaign_id: int):
    _campaign, member = require_member(db, campaign_id, user)
    if member.role != "gm":
        raise HTTPException(403, "Only the GM can push to GitHub")
    cfg = db.query(models.GithubExport).filter_by(campaign_id=campaign_id).first()
    if cfg is None or not cfg.token:
        raise HTTPException(409, "Configure GitHub export first")
    cfg.last_status = "Pushing…"
    db.commit()
    threading.Thread(
        target=github_export.run_export, args=(SessionLocal, campaign_id), daemon=True
    ).start()
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


@router.post("/campaigns/{campaign_id}/sessions")
def create_session(
    db: DbDep, user: UserDep, campaign_id: int,
    title: Annotated[str, Form()],
    scheduled_at: Annotated[str, Form()] = "",
):
    _campaign, member = require_member(db, campaign_id, user)
    if member.role != "gm":
        raise HTTPException(403, "Only the GM can schedule sessions")
    title = title.strip()[:200]
    if not title:
        raise HTTPException(400, "Session title required")
    when = None
    if scheduled_at:
        try:
            when = datetime.fromisoformat(scheduled_at)
        except ValueError:
            raise HTTPException(400, "Invalid date/time")
    game = models.GameSession(campaign_id=campaign_id, title=title, scheduled_at=when)
    db.add(game)
    db.commit()
    return RedirectResponse(f"/sessions/{game.id}", status_code=303)
