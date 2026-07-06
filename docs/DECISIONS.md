# Architecture Decision Records

Short log of the load-bearing choices, so future phases don't relitigate them
blind. Newest last.

## ADR-1: WebRTC full mesh instead of LiveKit (v1)

**Context.** The spec recommends LiveKit. Tablecast targets 3–6 person
tables, self-hosted by non-experts, deployed with one `docker compose up`.

**Decision.** v1 uses a browser full-mesh (peer-to-peer audio), with the
backend WebSocket as the signaling channel and public STUN.

**Consequences.** Zero media-server containers, no TURN/egress config, and
client-side recording falls out for free (ADR-2). Costs: upstream bandwidth
scales O(n) per client (fine for ≤6 audio streams), and users behind
symmetric NAT can fail to connect. LiveKit lands in Phase 2 as an *option*
for bigger rooms — the signaling is already isolated in `ws.py`/`room.js`,
so the swap is contained.

## ADR-2: Per-speaker recording happens in the browser, not the server

**Context.** Podcast editing needs clean per-speaker tracks (`GM.wav`,
`Annette.wav`…). Server-side capture of mesh audio would require an SFU.

**Decision.** Each client records its *own* microphone with MediaRecorder
and uploads chunks; the server never records network audio.

**Consequences.** Speaker tracks are pristine (no network jitter/loss
artifacts), even when a peer's connection is bad. Costs: recording depends
on the client staying open (mitigated by 20 s chunk uploads — a crash loses
at most ~20 s), and mixing happens after the fact from per-user chunk sets.

## ADR-3: Chunk-per-recorder, ~20 s rotation

**Context.** A single MediaRecorder emits header-less continuation blobs —
individually undecodable, useless for live transcription, and one crash
loses the whole take.

**Decision.** Clients restart MediaRecorder every ~20 s; every uploaded
chunk is a complete webm/opus file with its own `offset` from the shared
recording clock (`recording_started_at`, broadcast by the server).

**Consequences.** Chunks double as transcription jobs (this is what makes
the transcript *live*), uploads are crash-tolerant, and session-end
finalization is a straightforward FFmpeg concat. Cost: a few ms gap at each
rotation boundary — inaudible in speech, acceptable for v1.

## ADR-4: Worker talks HTTP to the backend, never to the database

**Context.** The transcription worker could share the SQLite volume, but
cross-process SQLite writes from separate containers invite locking bugs,
and a GPU worker may not even be on the same machine.

**Decision.** The worker uses a token-authenticated internal HTTP API
(claim → download audio → post segments). It is stateless.

**Consequences.** Workers scale horizontally, can live on other hosts
(point `TABLECAST_BACKEND_URL` at the backend), and the backend can push
new segments into the live room the moment results arrive. Cost: one small
internal API surface to maintain.

## ADR-5: SQLite first, PostgreSQL later

**Decision.** SQLite in WAL mode on the data volume; all access through
SQLAlchemy so `DATABASE_URL` can point at Postgres without code changes.
A game group's write volume is trivially within SQLite's envelope, and one
fewer container matters for self-hosters. Compose gains a Postgres option
in Phase 2 alongside full-text search (SQLite FTS5 vs. Postgres tsvector is
the real fork in that road).

## ADR-6: Stdlib crypto only for auth (PBKDF2 + HMAC cookies)

**Decision.** Password hashing is PBKDF2-HMAC-SHA256 (600k iterations) and
session cookies are HMAC-signed `user_id:expiry` — both pure stdlib, no
bcrypt/JWT dependencies to build in the slim image. Adequate for a
self-hosted tool where the user table is one gaming group; revisit only if
Tablecast ever becomes multi-tenant.

## ADR-7: Server-rendered pages + vanilla JS, no SPA framework

**Decision.** Jinja templates for pages; one hand-written `room.js` for the
only truly dynamic screen (the session room). No node toolchain in the
build.

**Consequences.** `docker build` is pip-only and fast; contributors need
Python only. If the room UI outgrows vanilla JS (Phase 3 waveform/chapter
editing is the likely trigger), adopt a build step then — the JSON WS
protocol is already UI-agnostic.

## ADR-8: Secrets are optional — auto-generate and persist, don't hard-require

**Context.** `docker-compose.yml` originally used `${VAR:?set in .env}` for
`TABLECAST_SECRET_KEY` and `TABLECAST_WORKER_TOKEN`. That's correct for the
`docker compose` CLI, which reads a `.env` file next to the compose file —
but it breaks pasting the same file into **Portainer** as a stack, which
doesn't load that `.env` the same way: the compose file fails to *load* at
all with "required variable is missing a value," before any container
starts.

**Decision.** Neither secret is required. `config.py`'s `_persisted_secret`
returns the env var if set; otherwise it generates one with `secrets.token_hex`
and persists it to a file (mode `0600`) so restarts reuse it. `SECRET_KEY`
persists to `DATA_DIR/.secret_key`. `WORKER_TOKEN` persists to
`SHARED_DIR/.worker_token`, and the worker's `resolve_token()` polls for that
same file when its own env var is unset.

**Consequences.** The stack now deploys with zero configuration in Portainer,
plain `docker compose`, or bare `uvicorn` — while every deployment still gets
a unique, real secret (never a hardcoded default) generated at first boot.
The one cost is a narrow exception to ADR-4: the worker gets a *tiny*
read-only volume (`worker_secrets`) shared with the backend, carrying only
the token handoff file — the worker still fetches all audio and posts all
results over HTTP, never touching `/data`. Operators who want secrets
independent of the volumes (multi-host worker, surviving a volume wipe) can
still set both env vars explicitly, which skips generation entirely.
