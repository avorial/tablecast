# Backlog

Working list, ordered within each bucket. The phase map lives in
[ROADMAP.md](../ROADMAP.md); this file is the next-actions detail.

## Phase 1 hardening (before calling the MVP done)

- [ ] **Transcription quality pass** *(top priority)*
  - [ ] Expose `WHISPER_LANGUAGE` env (skip auto-detect for non-English tables)
  - [ ] `initial_prompt` support seeded with campaign proper nouns (NPC/place
        names massively improve whisper accuracy on fantasy vocabulary)
  - [ ] Chunk-boundary stitching: merge segments that whisper splits across
        the 20 s rotation seam
  - [ ] Show interim "transcribing…" state in the room when jobs are queued
- [ ] Reconnect/resync in the room: on WS reconnect, replay recent events
      and transcript instead of starting blank
- [ ] Session page auto-refresh when the GM starts a scheduled session
      (currently players refresh manually)
- [ ] Archive: transcript search-in-page and per-speaker filter
- [ ] GM notes + pinned facts panel (in the spec's room layout, not yet built)
- [ ] Attendance derived from room presence, not just event authorship
- [ ] Configurable STUN/TURN servers via env
- [ ] Backup story: document copying `/data`; add `sqlite3 .backup` helper
- [ ] CI: GitHub Actions — lint (ruff), the smoke test suite, docker build

## Phase 2 — Campaign archive

- [ ] Full-text search across transcripts/chat/notes (SQLite FTS5)
- [ ] Entity extraction (NPCs, locations, factions) from transcripts into a
      campaign glossary; feed it back into whisper `initial_prompt` (loop!)
- [ ] Campaign timeline view (sessions × markers × reveals)
- [ ] "Campaign memory" cross-references ("Fort Robespierre also came up in
      Sessions 3 and 5")
- [ ] AI recap/summary/open-threads generation — pluggable backend (local
      model or API), filling the placeholder sections in the Markdown export
- [ ] Obsidian-vault-shaped export (folder of linked pages, not one file)
- [ ] LiveKit option (SFU + TURN) behind a compose profile
- [ ] Postgres option in compose

## Phase 3 — Podcast tools

- [ ] Sample-accurate speaker alignment in the mixdown (use chunk `offset_s`
      with `adelay`, verify drift over 4-hour sessions)
- [ ] Loudness normalization (EBU R128 via `loudnorm`) + silence trimming
- [ ] Intro/outro slots, chapter markers from scene markers (ID3/MP4 chapters)
- [ ] Per-speaker WAV export for DAW editing

## Phase 4 — Integrations

- [ ] RSS podcast feed
- [ ] GitHub commit export of session pages
- [ ] Foundry VTT journal export
- [ ] Discord import (historical recordings/logs)
- [ ] S3-compatible storage backend

## Phase 5 — AI assistant

- [ ] RAG over transcripts + entities + markers
- [ ] "What happened last session?" / "Who was the customs officer?" /
      "Generate a player recap" / "Make show notes"

## Known limitations (accepted for v1, tracked)

- `mixed.mp3` concatenates chunks without gap compensation — fine for
  transcript-first use, fixed properly in Phase 3.
- Mesh voice + public STUN only: symmetric-NAT users may not connect until
  TURN/LiveKit (Phase 2).
- No email verification or password reset (self-hosted, invite-code trust
  model).
- Transcript speaker labels come from account identity (who uploaded the
  chunk), not diarization — which is exactly what makes them reliable.
