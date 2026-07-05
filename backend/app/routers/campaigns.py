from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import models
from ..deps import DbDep, UserDep, require_member, templates

router = APIRouter()


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
    return templates.TemplateResponse(
        request, "campaign.html",
        {"user": user, "campaign": campaign, "member": member,
         "upcoming": upcoming, "past": past},
    )


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
