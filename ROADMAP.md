# Tablecast Roadmap

> A self-hosted session room for tabletop RPG groups: voice chat, dice,
> recording, live transcription, searchable session archives, and
> podcast-ready exports. **The session itself is the source of truth.**

Deployment target for every phase: `docker compose up` on a single host.

---

## Phase 1 — Session Recorder (MVP) ✅ in progress

Goal: the smallest thing that is useful, fast. A group can run a real session
tonight and walk away with audio, a transcript, and a Markdown page.

### Features

- [x] Email/password auth (register, login, session cookie)
- [x] Campaigns (create, join via invite code, member list)
- [x] Sessions (schedule, start, live room, end, archive)
- [x] Browser voice room (WebRTC mesh, mute/deafen, presence)
- [x] Per-user audio recording (each browser records its own mic → clean speaker tracks)
- [x] Dice roller (`2d6+3`, advantage/disadvantage shorthand, roll log)
- [x] Text chat (WebSocket, persisted)
- [x] Scene markers (Combat starts / Break / Important reveal / NPC introduced / Lore drop / End scene)
- [x] Live transcript (chunked upload → whisper worker → pushed to room)
- [x] Session archive (attendees, events, transcript, downloads)
- [x] Download audio (per-speaker tracks + `mixed.mp3` via FFmpeg)
- [x] Download Markdown summary (wiki-ready page per session)
- [x] Docker Compose deployment (backend + transcription worker, SQLite, local volume)

### Deliberate v1 constraints

- **WebRTC mesh instead of LiveKit.** A full mesh is fine for 3–6 person
  tables, needs zero extra infrastructure, and client-side recording gives us
  per-speaker tracks for free. LiveKit (SFU, TURN, server-side egress) is the
  Phase 2 upgrade path for bigger rooms and flaky NATs.
- **SQLite instead of PostgreSQL.** One volume, zero setup. The data layer is
  SQLAlchemy, so Postgres is a `DATABASE_URL` change away.
- **No video, no maps, no character sheets, no marketplace, no mobile app.**
- **No track alignment in `mixed.mp3` yet** — speaker tracks are mixed from
  each user's recording start; sample-accurate alignment lands with the
  podcast tools in Phase 3.
- The transcription worker runs by default in compose; the app degrades
  gracefully to "no transcript" if it's stopped.

## Phase 2 — Campaign Archive

Goal: the campaign becomes searchable memory.

- Full-text search across transcripts, chat, notes (SQLite FTS5)
- NPC / location / faction extraction from transcripts
- Campaign timeline view (sessions, markers, reveals)
- Campaign memory: "Judith Dumont mentioned Fort Robespierre again — connects to Session 3 and 5"
- Wiki export: Obsidian vault layout, GitHub repo, CampaignRepo
- AI session summary, bullet recap, unresolved plot hooks (pluggable LLM: local or API)
- LiveKit option for larger rooms + TURN
- PostgreSQL option in compose

## Phase 3 — Podcast Tools

Goal: session → publishable episode with minimal editing.

- Sample-accurate speaker track alignment
- Silence trimming and loudness normalization (EBU R128)
- Intro/outro slots
- Chapter markers generated from scene markers
- Per-speaker track export (WAV) + episode master (MP3/AAC)
- Show-notes generation from the session summary

## Phase 4 — Integrations

- RSS podcast feed (self-hosted)
- Discord import (bring old recordings/logs into the archive)
- Foundry VTT links (journal entry export)
- GitHub commit export (push session pages to a repo)
- Obsidian vault sync
- S3-compatible object storage backend

## Phase 5 — AI Assistant

Ask the campaign anything:

- "What happened last session?"
- "Who was the customs officer?"
- "What plot hooks are unresolved?"
- "Generate a recap for players."
- "Turn this session into podcast show notes."

RAG over the session archive (transcripts + extracted entities + markers),
with a pluggable model backend so it stays self-hostable.

---

## Architecture (Phase 1)

```
┌───────────────┐   HTTPS/WSS    ┌──────────────────────────────┐
│ Player browser│◄──────────────►│  backend (FastAPI + Jinja)   │
│  - voice mesh │   WebRTC (P2P) │  - auth, campaigns, sessions │
│  - MediaRec.  │◄──────────────►│  - room WS (chat/dice/rtc)   │
│  - chunk POST │                │  - chunk store, FFmpeg mix   │
└───────────────┘                │  - markdown export           │
        ▲                        └───────────┬──────────────────┘
        │ other players                      │ SQLite + files on
        ▼                                    │ /data volume
┌───────────────┐   internal HTTP ┌──────────┴──────────────────┐
│ Player browser│                 │ worker (faster-whisper)     │
└───────────────┘                 │  polls jobs → posts segments│
                                  └─────────────────────────────┘
```
