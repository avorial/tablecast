"""Podcast bundle: a publish-ready episode from a finished session.

- episode.m4a — aligned mixdown, loudness-normalized to the podcast
  standard (-16 LUFS / -1.5 dBTP), with embedded chapters generated from
  the GM's scene markers. Optional intro/outro audio is stitched on and
  chapter times shift automatically.
- chapters.txt — the same chapters, human-readable.
- show-notes.md — episode description from the AI recap (when one exists)
  plus the chapter list.
- <Speaker>.wav — per-speaker aligned tracks for DAW editing.

Mid-session silence is deliberately NOT trimmed: cutting time out of the
episode would desync chapters and the transcript. (A future
timestamp-remapping pass is on the backlog.)
"""

import json
import logging
import shutil
import tempfile
from pathlib import Path

from sqlalchemy.orm import Session

from .. import config, models
from ..db import SessionLocal
from . import recap as recap_service
from .audio import (
    _ffmpeg,
    _probe_duration,
    _safe_name,
    build_aligned_track,
    session_audio_dir,
)

log = logging.getLogger("tablecast.podcast")

LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"


def _hms(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _chapters(db: Session, game: models.GameSession, shift_s: float) -> list[dict]:
    markers = (
        db.query(models.SessionEvent)
        .filter_by(session_id=game.id, kind="marker")
        .order_by(models.SessionEvent.id)
        .all()
    )
    chapters = [{"start": 0.0, "title": "Session start"}]
    for m in markers:
        if m.at_seconds is None:
            continue
        payload = json.loads(m.payload)
        title = payload.get("label", "Marker")
        if payload.get("note"):
            title += f" — {payload['note']}"
        chapters.append({"start": m.at_seconds + shift_s, "title": title})
    chapters.sort(key=lambda c: c["start"])
    return chapters


def _ffmetadata(chapters: list[dict], total_s: float, title: str) -> str:
    lines = [";FFMETADATA1", f"title={title}", "artist=Tablecast", ""]
    for i, ch in enumerate(chapters):
        end = chapters[i + 1]["start"] if i + 1 < len(chapters) else total_s
        lines += [
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={int(ch['start'] * 1000)}",
            f"END={int(max(ch['start'] + 0.001, end) * 1000)}",
            f"title={ch['title']}",
            "",
        ]
    return "\n".join(lines)


def build_podcast_bundle(session_id: int) -> None:
    """Background thread entry point."""
    db = SessionLocal()
    try:
        _build(db, session_id)
    except Exception:
        log.exception("podcast build failed for session %s", session_id)
        game = db.get(models.GameSession, session_id)
        if game is not None:
            game.podcast_status = "failed"
            db.commit()
    finally:
        db.close()


def _build(db: Session, session_id: int) -> None:
    game = db.get(models.GameSession, session_id)
    if game is None or shutil.which("ffmpeg") is None:
        raise RuntimeError("session missing or ffmpeg unavailable")

    out_dir = session_audio_dir(session_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    chunks = (
        db.query(models.AudioChunk)
        .filter_by(session_id=session_id)
        .order_by(models.AudioChunk.user_id, models.AudioChunk.seq)
        .all()
    )
    if not chunks:
        raise RuntimeError("no audio chunks recorded")

    by_user: dict[int, list[models.AudioChunk]] = {}
    for chunk in chunks:
        by_user.setdefault(chunk.user_id, []).append(chunk)

    # wipe previous bundle rows so a rebuild replaces cleanly
    (
        db.query(models.Recording)
        .filter(models.Recording.session_id == session_id,
                models.Recording.kind.in_(("podcast", "chapters", "shownotes", "speaker_wav")))
        .delete(synchronize_session=False)
    )
    db.commit()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        # 1. aligned per-speaker WAVs (kept in the bundle for DAW editing)
        wav_paths: list[Path] = []
        for user_id, user_chunks in by_user.items():
            user = db.get(models.User, user_id)
            filename = f"{_safe_name(user.name if user else str(user_id))}.wav"
            wav_path = out_dir / filename
            build_aligned_track(user_chunks, wav_path, ["-ar", "48000"])
            wav_paths.append(wav_path)
            db.add(models.Recording(
                session_id=session_id, user_id=user_id,
                kind="speaker_wav", path=str(wav_path), filename=filename,
            ))

        # 2. aligned mix, loudness-normalized
        body = tmp / "body.m4a"
        inputs: list[str] = []
        for path in wav_paths:
            inputs += ["-i", str(path)]
        _ffmpeg(
            inputs
            + ["-filter_complex",
               f"amix=inputs={len(wav_paths)}:duration=longest:normalize=0,{LOUDNORM}",
               "-c:a", "aac", "-b:a", "160k", str(body)]
        )

        # 3. intro/outro slots
        intro = config.INTRO_PATH if config.INTRO_PATH.exists() else None
        outro = config.OUTRO_PATH if config.OUTRO_PATH.exists() else None
        shift = _probe_duration(str(intro)) if intro else 0.0
        episode_src = body
        if intro or outro:
            pieces = [p for p in (intro, body, outro) if p]
            seg_inputs: list[str] = []
            for p in pieces:
                seg_inputs += ["-i", str(p)]
            graph = (
                "".join(f"[{i}]aresample=48000,aformat=channel_layouts=stereo[s{i}];"
                        for i in range(len(pieces)))
                + "".join(f"[s{i}]" for i in range(len(pieces)))
                + f"concat=n={len(pieces)}:v=0:a=1[out]"
            )
            stitched = tmp / "stitched.m4a"
            _ffmpeg([*seg_inputs, "-filter_complex", graph, "-map", "[out]",
                     "-c:a", "aac", "-b:a", "160k", str(stitched)])
            episode_src = stitched

        # 4. embed chapters + title metadata
        total = _probe_duration(str(episode_src))
        chapters = _chapters(db, game, shift)
        meta = tmp / "meta.txt"
        meta.write_text(_ffmetadata(chapters, total, game.title))
        episode = out_dir / "episode.m4a"
        _ffmpeg(["-i", str(episode_src), "-i", str(meta),
                 "-map_metadata", "1", "-map_chapters", "1",
                 "-c", "copy", str(episode)])
        db.add(models.Recording(
            session_id=session_id, user_id=None,
            kind="podcast", path=str(episode), filename="episode.m4a",
        ))

    # 5. chapters.txt + show notes
    chapters_txt = out_dir / "chapters.txt"
    chapters_txt.write_text(
        "".join(f"{_hms(c['start'])} {c['title']}\n" for c in chapters)
    )
    db.add(models.Recording(
        session_id=session_id, user_id=None,
        kind="chapters", path=str(chapters_txt), filename="chapters.txt",
    ))

    summary = recap_service.get_summary(db, session_id)
    notes = [f"# {game.title}", ""]
    if summary and summary.get("recap"):
        notes += [summary["recap"], ""]
        if summary.get("bullets"):
            notes += ["## Highlights", ""] + [f"- {b}" for b in summary["bullets"]] + [""]
    notes += ["## Chapters", ""]
    notes += [f"- {_hms(c['start'])} — {c['title']}" for c in chapters]
    notes.append("")
    notes_path = out_dir / "show-notes.md"
    notes_path.write_text("\n".join(notes))
    db.add(models.Recording(
        session_id=session_id, user_id=None,
        kind="shownotes", path=str(notes_path), filename="show-notes.md",
    ))

    game.podcast_status = "ready"
    db.commit()
    log.info("podcast bundle built for session %s (%d chapters, intro=%s)",
             session_id, len(chapters), bool(shift))
