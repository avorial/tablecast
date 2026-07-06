"""Tablecast transcription worker.

Polls the backend's internal API for pending audio chunks, transcribes them
with faster-whisper, and posts the segments back. Stateless: run one or many.

Env:
  TABLECAST_BACKEND_URL   e.g. http://backend:8000
  TABLECAST_WORKER_TOKEN  shared secret (must match the backend). If unset,
                          the worker waits for the backend to auto-generate
                          one and hand it over via TABLECAST_SHARED_DIR —
                          see config.py's _persisted_secret on the backend.
  TABLECAST_SHARED_DIR    volume shared read-only with the backend, used only
                          to pick up the auto-generated token (default: /shared)
  WHISPER_MODEL           tiny | base | small | medium | large-v3  (default: base)
  WHISPER_DEVICE          cpu | cuda        (default: cpu)
  WHISPER_COMPUTE         int8 | float16 …  (default: int8)
"""

import logging
import os
import tempfile
import time
from pathlib import Path

import httpx
from faster_whisper import WhisperModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tablecast.worker")

BACKEND = os.environ.get("TABLECAST_BACKEND_URL", "http://backend:8000").rstrip("/")
SHARED_TOKEN_FILE = Path(os.environ.get("TABLECAST_SHARED_DIR", "/shared")) / ".worker_token"
MODEL_NAME = os.environ.get("WHISPER_MODEL", "base")
DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
COMPUTE = os.environ.get("WHISPER_COMPUTE", "int8")

POLL_INTERVAL = 2.0
HEADERS: dict[str, str] = {}


def resolve_token() -> str:
    token = os.environ.get("TABLECAST_WORKER_TOKEN", "")
    if token:
        return token
    log.info("TABLECAST_WORKER_TOKEN not set; waiting for the backend to "
              "generate one at %s …", SHARED_TOKEN_FILE)
    while not SHARED_TOKEN_FILE.exists():
        time.sleep(2)
    return SHARED_TOKEN_FILE.read_text().strip()


def wait_for_backend(client: httpx.Client) -> None:
    while True:
        try:
            client.get(f"{BACKEND}/healthz").raise_for_status()
            return
        except Exception:
            log.info("waiting for backend at %s …", BACKEND)
            time.sleep(3)


def process(client: httpx.Client, model: WhisperModel, job: dict) -> None:
    chunk_id = job["id"]
    audio = client.get(f"{BACKEND}/internal/jobs/{chunk_id}/audio", headers=HEADERS)
    audio.raise_for_status()

    with tempfile.NamedTemporaryFile(suffix=".webm") as tmp:
        tmp.write(audio.content)
        tmp.flush()
        # Chunks are ~20s and independent, so cross-segment conditioning only
        # invites repetition hallucinations; greedy-only decoding keeps the
        # temperature-fallback sampler from producing garbage on hard audio.
        segments, info = model.transcribe(
            tmp.name,
            vad_filter=True,
            condition_on_previous_text=False,
            temperature=0.0,
        )
        out = [
            {"start": seg.start, "end": seg.end, "text": seg.text}
            for seg in segments
        ]

    client.post(
        f"{BACKEND}/internal/jobs/{chunk_id}/result",
        headers=HEADERS,
        json={"status": "done", "segments": out},
    ).raise_for_status()
    log.info("chunk %s: %d segment(s), lang=%s", chunk_id, len(out), info.language)


def main() -> None:
    HEADERS["X-Worker-Token"] = resolve_token()

    log.info("loading whisper model %r (%s/%s)…", MODEL_NAME, DEVICE, COMPUTE)
    model = WhisperModel(MODEL_NAME, device=DEVICE, compute_type=COMPUTE)
    log.info("model loaded")

    with httpx.Client(timeout=120) as client:
        wait_for_backend(client)
        client.post(f"{BACKEND}/internal/jobs/requeue-stale", headers=HEADERS)

        while True:
            try:
                response = client.post(f"{BACKEND}/internal/jobs/claim", headers=HEADERS)
                response.raise_for_status()
                job = response.json().get("job")
                if job is None:
                    time.sleep(POLL_INTERVAL)
                    continue
                try:
                    process(client, model, job)
                except Exception:
                    log.exception("transcription failed for chunk %s", job["id"])
                    client.post(
                        f"{BACKEND}/internal/jobs/{job['id']}/result",
                        headers=HEADERS,
                        json={"status": "failed", "segments": []},
                    )
            except Exception:
                log.exception("worker loop error; backing off")
                time.sleep(5)


if __name__ == "__main__":
    main()
