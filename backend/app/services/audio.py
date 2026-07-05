"""Session-end audio finalization.

Each browser uploads a series of independently decodable webm/opus chunks.
At session end we concatenate them into one track per speaker, then mix all
speaker tracks into mixed.mp3 with FFmpeg.

Note (Phase 3): speaker tracks are not yet sample-accurately aligned in the
mix; chunks are simply concatenated in sequence order.
"""

import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from sqlalchemy.orm import Session

from .. import config, models
from ..db import SessionLocal

log = logging.getLogger("tablecast.audio")


def session_audio_dir(session_id: int) -> Path:
    return config.AUDIO_DIR / f"session_{session_id}"


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "speaker"


def _ffmpeg(args: list[str]) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
        check=True,
        capture_output=True,
        text=True,
    )


def finalize_session_audio(session_id: int) -> None:
    """Runs in a background thread after the GM ends the session."""
    db: Session = SessionLocal()
    try:
        _finalize(db, session_id)
    except Exception:
        log.exception("audio finalization failed for session %s", session_id)
    finally:
        db.close()


def _finalize(db: Session, session_id: int) -> None:
    game = db.get(models.GameSession, session_id)
    if game is None:
        return

    chunks = (
        db.query(models.AudioChunk)
        .filter_by(session_id=session_id)
        .order_by(models.AudioChunk.user_id, models.AudioChunk.seq)
        .all()
    )
    if not chunks:
        game.recordings_ready = True
        db.commit()
        return

    if shutil.which("ffmpeg") is None:
        log.error("ffmpeg not found; skipping audio finalization")
        return

    out_dir = session_audio_dir(session_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    by_user: dict[int, list[models.AudioChunk]] = {}
    for chunk in chunks:
        by_user.setdefault(chunk.user_id, []).append(chunk)

    speaker_files: list[Path] = []
    for user_id, user_chunks in by_user.items():
        user = db.get(models.User, user_id)
        filename = f"{_safe_name(user.name if user else str(user_id))}.ogg"
        out_path = out_dir / filename
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as listfile:
            for chunk in user_chunks:
                escaped = chunk.path.replace("'", "'\\''")
                listfile.write(f"file '{escaped}'\n")
            list_path = listfile.name
        try:
            _ffmpeg(
                ["-f", "concat", "-safe", "0", "-i", list_path,
                 "-c:a", "libopus", "-b:a", "64k", str(out_path)]
            )
        except subprocess.CalledProcessError as exc:
            log.error("concat failed for user %s: %s", user_id, exc.stderr)
            continue
        finally:
            Path(list_path).unlink(missing_ok=True)

        speaker_files.append(out_path)
        db.add(
            models.Recording(
                session_id=session_id, user_id=user_id,
                kind="speaker", path=str(out_path), filename=filename,
            )
        )

    if speaker_files:
        mixed_path = out_dir / "mixed.mp3"
        inputs: list[str] = []
        for path in speaker_files:
            inputs += ["-i", str(path)]
        try:
            _ffmpeg(
                inputs
                + ["-filter_complex",
                   f"amix=inputs={len(speaker_files)}:duration=longest:normalize=0",
                   "-c:a", "libmp3lame", "-q:a", "4", str(mixed_path)]
            )
            db.add(
                models.Recording(
                    session_id=session_id, user_id=None,
                    kind="mixed", path=str(mixed_path), filename="mixed.mp3",
                )
            )
        except subprocess.CalledProcessError as exc:
            log.error("mixdown failed: %s", exc.stderr)

    game.recordings_ready = True
    db.commit()
    log.info("finalized audio for session %s (%d speakers)", session_id, len(speaker_files))
