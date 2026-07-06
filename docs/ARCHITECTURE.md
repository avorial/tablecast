# Tablecast Architecture (Phase 1)

Two containers, one volume. Everything self-hosted.

```
┌───────────────┐   HTTPS/WSS    ┌──────────────────────────────┐
│ Player browser│◄──────────────►│  backend (FastAPI + Jinja)   │
│  - voice mesh │   WebRTC (P2P) │  - auth, campaigns, sessions │
│  - MediaRec.  │◄──────────────►│  - room WS (chat/dice/rtc)   │
│  - chunk POST │                │  - chunk store, FFmpeg mix   │
└───────────────┘                │  - markdown export           │
        ▲                        └───────────┬──────────────────┘
        │ other players                      │ SQLite + audio files
        ▼                                    │ on /data volume
┌───────────────┐   internal HTTP ┌──────────┴──────────────────┐
│ Player browser│                 │ worker (faster-whisper)     │
└───────────────┘                 │  polls jobs → posts segments│
                                  └─────────────────────────────┘
```

## Components

### Backend (`backend/`)

FastAPI + SQLAlchemy + Jinja templates + vanilla JS. No frontend build step.

| Module | Responsibility |
|---|---|
| `app/main.py` | App wiring, lifespan (DB init), static mount |
| `app/config.py` | All env-driven configuration |
| `app/db.py` | Engine, session factory, SQLite pragmas (WAL, FKs) |
| `app/models.py` | ORM entities (see data model below) |
| `app/security.py` | PBKDF2 password hashing, HMAC-signed session cookies |
| `app/deps.py` | DB/user dependencies, membership guards |
| `app/ws.py` | Room hub: presence, chat, dice, markers, recording control, RTC signaling relay |
| `app/routers/auth.py` | Register / login / logout |
| `app/routers/campaigns.py` | Dashboard, campaign CRUD, invite-code join |
| `app/routers/sessions.py` | Session lifecycle, room/archive pages, chunk upload, downloads, Markdown export, WS endpoint |
| `app/routers/internal.py` | Worker job API (claim / audio / result / requeue) |
| `app/services/dice.py` | Dice expression parser (`2d6+3`, `adv`, `dis`) |
| `app/services/audio.py` | Session-end FFmpeg finalization |
| `app/services/export.py` | Markdown page generation |
| `app/static/room.js` | Voice mesh, recorder, room UI |

### Worker (`worker/`)

A single-file poller (`transcribe.py`). Stateless — run one or many. Talks to
the backend only over HTTP with a shared token (`X-Worker-Token`), so it
needs no database access and can run on a different machine (e.g. one with a
GPU) by pointing `TABLECAST_BACKEND_URL` at the backend.

## Data model

```
User ──< CampaignMember >── Campaign ──< GameSession
                              │              ├──< SessionEvent      (chat/roll/marker/system, JSON payload)
                              │              ├──< AudioChunk        (uploaded webm blobs + transcribe status)
                              │              ├──< TranscriptSegment (speaker, start_s/end_s, text)
                              │              └──< Recording         (finalized speaker .ogg / mixed.mp3)
```

- `GameSession.status`: `scheduled → live → ended`. Recording state
  (`recording_active`, `recording_started_at`) lives on the session so late
  joiners sync to the same clock.
- `SessionEvent.at_seconds` is relative to recording start — this is what
  makes scene markers usable as podcast chapter markers later (Phase 3).
- All timestamps in transcript segments are `chunk.offset_s + segment.start`,
  i.e. seconds since the *global* recording start.

## Session room protocol (WebSocket)

One socket per participant at `/ws/sessions/{id}`, cookie-authenticated at
handshake. Client → server messages:

| type | payload | notes |
|---|---|---|
| `chat` | `{text}` | persisted as SessionEvent |
| `roll` | `{expression}` | server rolls (authoritative), broadcasts result |
| `marker` | `{label, note?}` | GM only |
| `record` | `{action: start\|stop}` | GM only; broadcasts the shared recording clock |
| `rtc` | `{to, data}` | opaque SDP/ICE relay to one peer |
| `state` | `{muted}` | presence updates |

Server → client additionally: `peers` (join snapshot + recording state),
`presence`, `transcript` (segments pushed as the worker finishes them),
`ended`, `error`.

## Voice

Full-mesh WebRTC, audio only. The newcomer initiates offers to every
existing peer (`peers` snapshot on join); signaling is relayed through the
room socket; STUN only (Google public STUN) in v1 — symmetric-NAT users need
the TURN story that arrives with LiveKit in Phase 2.

## Recording pipeline

1. GM sends `record/start`; server stamps `recording_started_at` and
   broadcasts it. Every client starts a local `MediaRecorder` on its own mic.
2. Clients rotate the recorder every ~20 s so each uploaded chunk is an
   independently decodable webm/opus file (a *single* long MediaRecorder
   stream would make chunks header-less and useless individually).
3. Chunks POST to `/sessions/{id}/chunks` with `seq` and `offset` (seconds
   since the shared recording start). Uploads retry 3× with backoff.
4. Each stored chunk becomes a transcription job (`transcribe_status=pending`).
5. On session end, a background thread concatenates each speaker's chunks
   (FFmpeg concat demuxer → `Name.ogg`) and mixes all speakers into
   `mixed.mp3` (`amix`). `recordings_ready` flips when done.

Late joiners compute offsets from the same `recording_started_at`, so all
speaker timelines share one clock (alignment inside `mixed.mp3` is
best-effort in v1; sample-accurate alignment is Phase 3).

## Transcription pipeline

Worker loop: `POST /internal/jobs/claim` → `GET /internal/jobs/{id}/audio` →
faster-whisper (VAD-filtered) → `POST /internal/jobs/{id}/result`. The
backend stores segments and pushes them over the room WebSocket, so the live
transcript appears mid-session. On startup the worker requeues chunks stuck
in `processing` (crash recovery). A failed chunk is marked `failed` and never
blocks the queue.

## Security model

- Sessions: HMAC-SHA256-signed cookie (`user_id:expiry:sig`), 30-day TTL,
  `HttpOnly` + `SameSite=Lax`.
- Passwords: PBKDF2-HMAC-SHA256, 600k iterations, per-user salt (stdlib only).
- Authorization: every session/campaign route resolves membership
  (`require_member` / `require_session_member`); GM-only actions checked
  server-side both in HTTP routes and WS dispatch.
- Worker API: constant-time shared-token comparison against `WORKER_TOKEN`.
  It's reachable on the published port (same FastAPI app as everything
  else), so it's a real secret, not an internal-network-only one — see
  ADR-8 for how it gets a per-deployment value without requiring the
  operator to set anything.
- Browsers require HTTPS (or localhost) for `getUserMedia` — production
  deployments sit behind a TLS reverse proxy that forwards WebSockets.

## Secrets and zero-config deployment

`TABLECAST_SECRET_KEY` (cookie signing) and `TABLECAST_WORKER_TOKEN` (worker
auth) are both optional. If unset, `config.py`'s `_persisted_secret` generates
a random one on first boot and persists it — `SECRET_KEY` to
`DATA_DIR/.secret_key`, `WORKER_TOKEN` to `SHARED_DIR/.worker_token` — so the
stack deploys with no environment configuration at all (this is what makes
it pastable directly into Portainer, which doesn't load a `.env` file next
to `docker-compose.yml` the way the `docker compose` CLI does). Restarting a
container reuses the persisted value; only recreating the volume rotates it.

The worker needs the *same* token as the backend but must not share the
backend's `/data` volume (ADR-4). Compose adds one narrow exception: a small
`worker_secrets` volume mounted read-write on the backend and read-only on
the worker, carrying only the `.worker_token` handoff file — never
session/audio data. `worker/transcribe.py`'s `resolve_token()` polls for that
file if `TABLECAST_WORKER_TOKEN` isn't set in its own environment.

Set either variable explicitly if you want it independent of the volumes
(e.g. running the worker on a separate host, or wanting cookies to survive a
volume recreation).

## Storage layout (`/data` volume)

```
/data/tablecast.db                          SQLite (WAL)
/data/.secret_key                            auto-generated cookie-signing key (if not set via env)
/data/audio/session_<id>/chunks/<user>/NNNNNN.webm
/data/audio/session_<id>/<Speaker>.ogg
/data/audio/session_<id>/mixed.mp3
```
