"""Import a Craig (CraigChat) multi-track Discord recording.

Craig's multi-track download is a zip with one audio file per speaker,
named like "1-Annette_1234.flac". Each track is already aligned to the
session start (t=0), which maps cleanly onto Tablecast's per-speaker
recording model: we create one ended session and one AudioChunk per
matched speaker at offset 0, then let the normal transcription +
finalization pipeline take over (aligned speaker tracks, mixdown,
transcript, entities, podcast).

Speakers are matched to campaign members by a forgiving normalized
substring compare. Unmatched tracks are reported and skipped rather than
mis-assigned, so speaker identity stays correct.
"""

import io
import logging
import re
import zipfile
from pathlib import Path

from sqlalchemy.orm import Session

from .. import models
from .audio import session_audio_dir

log = logging.getLogger("tablecast.craig")

AUDIO_EXTS = {".flac", ".aac", ".opus", ".ogg", ".oga", ".wav", ".mp3", ".m4a", ".webm"}
# Craig prefixes tracks with a track number; usernames may carry a Discord
# discriminator or id suffix.
_PREFIX = re.compile(r"^\d+[-_]")
_SUFFIX = re.compile(r"[_#-]\d+$")


class CraigImportError(RuntimeError):
    pass


def _speaker_label(filename: str) -> str:
    stem = Path(filename).stem
    stem = _PREFIX.sub("", stem)
    stem = _SUFFIX.sub("", stem)
    return stem.replace("_", " ").strip() or filename


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _match_member(label: str, members: list[models.CampaignMember]) -> models.User | None:
    target = _normalize(label)
    if not target:
        return None
    best = None
    for m in members:
        mn = _normalize(m.user.name)
        if not mn:
            continue
        if mn == target:
            return m.user
        if mn in target or target in mn:
            best = m.user
    return best


def import_craig_zip(
    db: Session, campaign: models.Campaign, title: str, data: bytes,
    started_at=None,
) -> dict:
    """Returns {session_id, matched: [(label, name)], skipped: [label]}."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise CraigImportError("Not a valid zip file") from exc

    audio_members = [
        info for info in zf.infolist()
        if not info.is_dir() and Path(info.filename).suffix.lower() in AUDIO_EXTS
    ]
    if not audio_members:
        raise CraigImportError("No audio tracks found in the zip")

    members = list(campaign.members)

    game = models.GameSession(
        campaign_id=campaign.id, title=title.strip()[:200],
        status="ended", started_at=started_at or models.utcnow(),
        ended_at=models.utcnow(),
        recording_started_at=started_at or models.utcnow(),
    )
    db.add(game)
    db.flush()

    chunk_dir = session_audio_dir(game.id) / "chunks"
    matched: list[tuple[str, str]] = []
    skipped: list[str] = []

    for info in audio_members:
        label = _speaker_label(Path(info.filename).name)
        user = _match_member(label, members)
        if user is None:
            skipped.append(label)
            continue
        ext = Path(info.filename).suffix.lower()
        user_dir = chunk_dir / str(user.id)
        user_dir.mkdir(parents=True, exist_ok=True)
        dest = user_dir / f"000000{ext}"
        with zf.open(info) as src, open(dest, "wb") as out:
            # stream copy so a large FLAC doesn't balloon memory
            while True:
                buf = src.read(1024 * 256)
                if not buf:
                    break
                out.write(buf)
        db.add(models.AudioChunk(
            session_id=game.id, user_id=user.id, seq=0,
            path=str(dest), offset_s=0.0,
        ))
        matched.append((label, user.name))

    if not matched:
        db.rollback()
        raise CraigImportError(
            "None of the tracks matched a campaign member. Track names: "
            + ", ".join(_speaker_label(Path(i.filename).name) for i in audio_members)
        )

    db.commit()
    log.info("craig import: session %s, matched %d, skipped %d",
             game.id, len(matched), len(skipped))
    return {"session_id": game.id, "matched": matched, "skipped": skipped}
