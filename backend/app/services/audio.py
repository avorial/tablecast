"""Session-end audio finalization.

Each browser uploads a series of independently decodable webm/opus chunks
stamped with an offset on the shared recording clock. At session end we
build **aligned** speaker tracks: chunks are grouped into contiguous runs,
each run is placed at its true position on the timeline (gaps — mutes,
late joins, disconnects — become silence), so every speaker track starts
at recording t=0 and lines up sample-accurately in a DAW. The mixdown is
then a straight sum of aligned tracks.
"""

import logging
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from sqlalchemy.orm import Session

from .. import config, models
from ..db import SessionLocal

log = logging.getLogger("tablecast.audio")

# Chunks whose start is within this of the previous chunk's expected end are
# treated as contiguous (rotation jitter); anything larger is a real gap.
RUN_GAP_TOLERANCE_S = 1.5


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


def _probe_duration(path: str) -> float:
    if shutil.which("ffprobe"):
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        try:
            return float(out)
        except ValueError:
            return 0.0
    # ffprobe-less fallback (e.g. static ffmpeg-only installs): decode to
    # null and read the reported time.
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", path, "-f", "null", "-"],
        capture_output=True, text=True,
    )
    match = re.findall(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
    if not match:
        return 0.0
    h, m, s = match[-1]
    return int(h) * 3600 + int(m) * 60 + float(s)


def _runs(chunks: list[models.AudioChunk], tmp: Path) -> list[tuple[float, Path]]:
    """Group a user's chunks into contiguous runs; concat each run to a WAV.
    Returns [(start_offset_s, wav_path), ...]."""
    runs: list[list[models.AudioChunk]] = []
    expected_end: float | None = None
    for chunk in chunks:
        duration = _probe_duration(chunk.path)
        if (
            runs
            and expected_end is not None
            and abs(chunk.offset_s - expected_end) <= RUN_GAP_TOLERANCE_S
        ):
            runs[-1].append(chunk)
        else:
            runs.append([chunk])
        expected_end = chunk.offset_s + duration

    out: list[tuple[float, Path]] = []
    for i, run in enumerate(runs):
        listfile = tmp / f"run{i}.txt"
        escaped = [c.path.replace("'", "'\\''") for c in run]
        listfile.write_text("".join(f"file '{p}'\n" for p in escaped))
        wav = tmp / f"run{i}.wav"
        _ffmpeg(["-f", "concat", "-safe", "0", "-i", str(listfile),
                 "-ar", "48000", "-ac", "1", str(wav)])
        out.append((run[0].offset_s, wav))
    return out


def build_aligned_track(
    chunks: list[models.AudioChunk], out_path: Path, codec_args: list[str]
) -> None:
    """One speaker's aligned track: every run delayed to its true position
    on the recording clock, mixed onto a single timeline."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        runs = _runs(chunks, tmp)
        if len(runs) == 1 and runs[0][0] < 0.05:
            _ffmpeg(["-i", str(runs[0][1]), *codec_args, str(out_path)])
            return
        inputs: list[str] = []
        filters: list[str] = []
        labels: list[str] = []
        for i, (offset, wav) in enumerate(runs):
            inputs += ["-i", str(wav)]
            ms = max(0, int(offset * 1000))
            filters.append(f"[{i}]adelay={ms}|{ms}[a{i}]")
            labels.append(f"[a{i}]")
        graph = (
            ";".join(filters)
            + f";{''.join(labels)}amix=inputs={len(runs)}:duration=longest:normalize=0[out]"
        )
        _ffmpeg([*inputs, "-filter_complex", graph, "-map", "[out]", *codec_args,
                 str(out_path)])


def finalize_session_audio(session_id: int, delay_s: float | None = None) -> None:
    """Runs in a background thread after the GM ends the session.

    Waits briefly first so clients can flush their final recording chunk
    (accepted during the grace window in the chunk-upload route)."""
    if delay_s is None:
        delay_s = config.FINALIZE_DELAY_S
    if delay_s > 0:
        time.sleep(delay_s)
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
        try:
            build_aligned_track(
                user_chunks, out_path, ["-c:a", "libopus", "-b:a", "64k"]
            )
        except subprocess.CalledProcessError as exc:
            log.error("track build failed for user %s: %s", user_id, exc.stderr)
            continue
        speaker_files.append(out_path)
        db.add(models.Recording(
            session_id=session_id, user_id=user_id,
            kind="speaker", path=str(out_path), filename=filename,
        ))

    if speaker_files:
        mixed_path = out_dir / "mixed.mp3"
        inputs: list[str] = []
        for path in speaker_files:
            inputs += ["-i", str(path)]
        try:
            # Tracks share the recording clock (adelay above), so a plain
            # sum is sample-aligned.
            _ffmpeg(
                inputs
                + ["-filter_complex",
                   f"amix=inputs={len(speaker_files)}:duration=longest:normalize=0",
                   "-c:a", "libmp3lame", "-q:a", "4", str(mixed_path)]
            )
            db.add(models.Recording(
                session_id=session_id, user_id=None,
                kind="mixed", path=str(mixed_path), filename="mixed.mp3",
            ))
        except subprocess.CalledProcessError as exc:
            log.error("mixdown failed: %s", exc.stderr)

    game.recordings_ready = True
    db.commit()
    log.info("finalized audio for session %s (%d speakers, aligned)",
             session_id, len(speaker_files))
