# Tablecast 🎲

A self-hosted session room for tabletop RPG groups: voice chat, dice,
per-speaker recording, live transcription, searchable session archives, and
podcast-ready exports.

**Discord is good at live voice. It is bad at preserving a game.** Tablecast
makes the session itself the source of truth: run your game in a browser voice
room, and walk away with speaker-labeled audio tracks, a transcript, scene
markers, the dice log, and a wiki-ready Markdown page.

Planning docs:

- [ROADMAP.md](ROADMAP.md) — the five-phase plan and what's in Phase 1
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — components, data model, protocols, pipelines
- [docs/DECISIONS.md](docs/DECISIONS.md) — why it's built this way (ADRs)
- [docs/BACKLOG.md](docs/BACKLOG.md) — prioritized next actions per phase

## Quick start

No `.env` file or secrets required — clone and run:

```bash
# App + live transcription (downloads a Whisper model on first run):
docker compose up -d --build

# App only, no transcription:
docker compose up -d --build backend
```

This also works as a **Portainer stack**: paste `docker-compose.yml` in
directly, no environment variables need to be set. Auth cookie signing and
the worker's internal API token are auto-generated on first boot and
persisted to their volumes, so they survive restarts.

Open http://localhost:8200, register an account, create a campaign, and share
the invite code with your table.

Want config beyond the defaults (custom port, closed registration, a bigger
Whisper model, pinned secrets)? Copy `.env.example` to `.env` and edit it —
see [Configuration](#configuration) below.

> **Microphone note:** browsers only allow microphone access on `localhost` or
> over HTTPS. For a real remote game, put Tablecast behind a TLS reverse proxy
> (Caddy, Traefik, nginx + Let's Encrypt) — make sure it forwards WebSockets.

## How a session works

1. The GM creates a campaign; players join with the invite code.
2. The GM schedules a session, then hits **Start session**.
3. Everyone joins the room: browser-to-browser voice (WebRTC mesh), text chat,
   dice roller (`/roll 2d6+3`, `adv`, `dis`), and a live transcript pane.
4. The GM hits **Start recording** — every participant's browser records its
   *own* microphone and streams ~20-second chunks to the server. That's what
   gives you clean per-speaker tracks instead of one muddy mix.
5. The GM drops **scene markers** during play: ⚔️ Combat starts, ☕ Break,
   ❗ Important reveal, 🧙 NPC introduced, 📜 Lore drop, 🎬 End scene.
6. If the transcription worker is running, transcript lines appear live in the
   room as people speak.
7. The GM hits **End session**. FFmpeg builds *aligned* speaker tracks
   (`<Name>.ogg`, all starting at recording t=0 — gaps become silence) and
   mixes everyone into `mixed.mp3`.
8. The session archive page now has: attendees, full event log, transcript,
   audio downloads, and a **Markdown export** ready for Obsidian / GitHub /
   your campaign wiki.
9. Want an episode? Hit **Build podcast bundle**: a loudness-normalized
   `episode.m4a` (-16 LUFS) with chapter markers generated from your scene
   markers, `chapters.txt`, `show-notes.md` (from the AI recap), and
   per-speaker WAVs for DAW editing. Drop `intro.mp3`/`outro.mp3` into
   `/data/podcast/` and they're stitched on, chapters shifted automatically.

## Architecture

```
backend  — FastAPI + Jinja + vanilla JS. Auth, campaigns, sessions, room
           WebSocket (chat/dice/markers/WebRTC signaling), chunk storage,
           FFmpeg finalization, Markdown export. SQLite on a volume.
worker   — faster-whisper. Polls the backend for uploaded audio chunks,
           posts transcript segments back; the backend pushes them into the
           live room. Runs by default; the app degrades gracefully (no
           transcript) if it's down.
```

Voice is a full-mesh WebRTC topology — ideal for the 3–6 person table this is
built for, with zero media-server infrastructure. LiveKit support for larger
rooms is on the roadmap (Phase 2).

## Configuration

All optional, via environment variables (see `.env.example`):

| Variable | Default | Purpose |
|---|---|---|
| `TABLECAST_SECRET_KEY` | auto-generated, persisted to the data volume | Signs auth cookies |
| `TABLECAST_WORKER_TOKEN` | auto-generated, persisted to a shared volume | Shared secret for the worker API |
| `TABLECAST_PORT` | `8200` | Published port |
| `TABLECAST_REGISTRATION_OPEN` | `true` | Set `false` to close signups |
| `WHISPER_MODEL` | `base` | `tiny`/`base`/`small`/`medium`/`large-v3` |
| `WHISPER_DEVICE` | `cpu` | `cuda` if you have a GPU |
| `WHISPER_LANGUAGE` | auto-detect | Pin the spoken language, e.g. `en` |
| `TABLECAST_LLM_BASE_URL` | unset (recaps off) | OpenAI-compatible endpoint for AI recaps |
| `TABLECAST_LLM_MODEL` | — | Model name at that endpoint |
| `TABLECAST_LLM_API_KEY` | — | If the endpoint needs one |
| `TABLECAST_ICE_SERVERS` | Google STUN | JSON array; add TURN for strict NATs |
| `DATABASE_URL` | SQLite on `/data` | Any SQLAlchemy URL (Postgres later) |

Pin `TABLECAST_SECRET_KEY` / `TABLECAST_WORKER_TOKEN` yourself if you want
them independent of the volumes (e.g. running the worker on a separate host,
or wanting cookies to survive a volume recreation).

## Backups

Everything lives on the `tablecast_data` volume: the SQLite database, audio
chunks, finalized recordings, and the auto-generated secrets. To back it up:

```bash
# Consistent SQLite snapshot + all audio, written to ./tablecast-backup/
docker compose exec backend sh -c \
  "python -c \"import sqlite3; sqlite3.connect('/data/tablecast.db').execute('VACUUM INTO \\'/data/backup.db\\'')\""
docker run --rm -v tablecast_data:/data -v "$PWD/tablecast-backup:/out" alpine \
  sh -c "cp /data/backup.db /out/ && cp -r /data/audio /out/ && rm /data/backup.db"
```

Restore by copying the files back into the volume before starting the stack.
(`VACUUM INTO` produces a consistent copy even while the app is running;
don't copy `tablecast.db` directly while a session is live.)

## Development

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
TABLECAST_DATA_DIR=./data TABLECAST_WORKER_TOKEN=dev \
  .venv/bin/uvicorn app.main:app --reload
```
