import secrets
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(80))
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    memberships: Mapped[list["CampaignMember"]] = relationship(back_populates="user")


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    gm_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    join_code: Mapped[str] = mapped_column(
        String(12), unique=True, default=lambda: secrets.token_urlsafe(6)
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    gm: Mapped[User] = relationship()
    members: Mapped[list["CampaignMember"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )
    sessions: Mapped[list["GameSession"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )


class CampaignMember(Base):
    __tablename__ = "campaign_members"
    __table_args__ = (UniqueConstraint("campaign_id", "user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    role: Mapped[str] = mapped_column(String(16), default="player")  # gm | player

    campaign: Mapped[Campaign] = relationship(back_populates="members")
    user: Mapped[User] = relationship(back_populates="memberships")


class GameSession(Base):
    __tablename__ = "game_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"))
    title: Mapped[str] = mapped_column(String(200))
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="scheduled")  # scheduled | live | ended
    recording_active: Mapped[bool] = mapped_column(Boolean, default=False)
    recording_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    recordings_ready: Mapped[bool] = mapped_column(Boolean, default=False)

    campaign: Mapped[Campaign] = relationship(back_populates="sessions")
    events: Mapped[list["SessionEvent"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class SessionEvent(Base):
    """Chat messages, dice rolls, scene markers, and system events."""

    __tablename__ = "session_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("game_sessions.id"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(16))  # chat | roll | marker | system
    payload: Mapped[str] = mapped_column(Text)  # JSON
    at_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)  # since recording start
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    session: Mapped[GameSession] = relationship(back_populates="events")
    user: Mapped[User | None] = relationship()


class AudioChunk(Base):
    """One independently decodable webm/opus blob uploaded by a client."""

    __tablename__ = "audio_chunks"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("game_sessions.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    seq: Mapped[int] = mapped_column(Integer)
    path: Mapped[str] = mapped_column(String(500))
    offset_s: Mapped[float] = mapped_column(Float)  # seconds since recording start
    # pending | processing | done | failed | skipped
    transcribe_status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[User] = relationship()


class TranscriptSegment(Base):
    __tablename__ = "transcript_segments"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("game_sessions.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    start_s: Mapped[float] = mapped_column(Float)  # since recording start
    end_s: Mapped[float] = mapped_column(Float)
    text: Mapped[str] = mapped_column(Text)

    user: Mapped[User] = relationship()


class CampaignEntity(Base):
    """A recurring proper noun (NPC, place, faction) mined from transcripts
    and chat — the campaign's memory. Phase 2 extraction is heuristic;
    Phase 5 upgrades it with an LLM."""

    __tablename__ = "campaign_entities"
    __table_args__ = (UniqueConstraint("campaign_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(16), default="name")  # name|npc|place|faction
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    mentions: Mapped[list["EntityMention"]] = relationship(
        back_populates="entity", cascade="all, delete-orphan"
    )


class EntityMention(Base):
    __tablename__ = "entity_mentions"
    __table_args__ = (UniqueConstraint("entity_id", "session_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("campaign_entities.id"), index=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("game_sessions.id"), index=True)
    count: Mapped[int] = mapped_column(Integer, default=1)

    entity: Mapped[CampaignEntity] = relationship(back_populates="mentions")
    session: Mapped[GameSession] = relationship()


class SessionSummary(Base):
    """AI-generated recap for a finished session (one per session,
    regenerating replaces it)."""

    __tablename__ = "session_summaries"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("game_sessions.id"), unique=True, index=True
    )
    model: Mapped[str] = mapped_column(String(120))
    payload: Mapped[str] = mapped_column(Text)  # JSON: recap/bullets/npcs/locations/open_threads
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Recording(Base):
    """Finalized audio artifacts produced at session end."""

    __tablename__ = "recordings"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("game_sessions.id"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(16))  # speaker | mixed
    path: Mapped[str] = mapped_column(String(500))
    filename: Mapped[str] = mapped_column(String(200))

    user: Mapped[User | None] = relationship()
