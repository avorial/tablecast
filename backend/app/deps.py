from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, status
from fastapi.templating import Jinja2Templates
from pathlib import Path
from sqlalchemy.orm import Session

from . import config, models, security
from .db import SessionLocal

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


DbDep = Annotated[Session, Depends(get_db)]


def get_current_user(
    db: DbDep,
    tablecast_session: Annotated[str | None, Cookie(alias=config.SESSION_COOKIE)] = None,
) -> models.User | None:
    if not tablecast_session:
        return None
    user_id = security.verify_session_token(tablecast_session)
    if user_id is None:
        return None
    return db.get(models.User, user_id)


def require_user(
    user: Annotated[models.User | None, Depends(get_current_user)],
) -> models.User:
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
        )
    return user


UserDep = Annotated[models.User, Depends(require_user)]


def require_member(
    db: Session, campaign_id: int, user: models.User
) -> tuple[models.Campaign, models.CampaignMember]:
    campaign = db.get(models.Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(404, "Campaign not found")
    member = (
        db.query(models.CampaignMember)
        .filter_by(campaign_id=campaign_id, user_id=user.id)
        .first()
    )
    if member is None:
        raise HTTPException(403, "Not a member of this campaign")
    return campaign, member


def require_session_member(
    db: Session, session_id: int, user: models.User
) -> tuple[models.GameSession, models.CampaignMember]:
    game = db.get(models.GameSession, session_id)
    if game is None:
        raise HTTPException(404, "Session not found")
    _campaign, member = require_member(db, game.campaign_id, user)
    return game, member
