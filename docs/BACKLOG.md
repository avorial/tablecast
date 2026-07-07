# Backlog

Working list, ordered within each bucket. The phase map lives in
[ROADMAP.md](../ROADMAP.md); this file is the next-actions detail.

## Phase 1 hardening (before calling the MVP done)

- [x] **Transcription quality pass** *(top priority)*
  - [x] Expose `WHISPER_LANGUAGE` env (skip auto-detect for non-English tables)
  - [x] `initial_prompt` support seeded with campaign proper nouns (NPC/place
        names massively improve whisper accuracy on fantasy vocabulary) —
        campaign name, session title, player names for now; extracted
        NPC/location names join in Phase 2
  - [x] Chunk-boundary stitching: merge segments that whisper splits across
        the 20 s rotation seam (same-speaker segments ≤2 s apart)
  - [x] Show interim "transcribing…" state in the room when jobs are queued
- [x] Reconnect/resync in the room: on WS reconnect, replay recent events
      and transcript instead of starting blank
- [x] Session page auto-refresh when the GM starts a scheduled session
- [x] Archive: transcript search-in-page and per-speaker filter
- [ ] GM notes + pinned facts panel (in the spec's room layout, not yet built)
- [x] Attendance derived from room presence, not just event authorship
- [x] Configurable STUN/TURN servers via env (`TABLECAST_ICE_SERVERS`)
- [x] Backup story: documented `VACUUM INTO` snapshot + volume copy in README
- [x] CI: GitHub Actions — lint (ruff), the smoke test suite, docker build

## Phase 2 — Campaign archive

- [x] Full-text search across transcripts/chat/markers (SQLite FTS5 with
      snippets and highlights; LIKE fallback on other databases)
- [x] Entity extraction into a campaign glossary — heuristic proper-noun
      mining (no ML dependency); feeds back into whisper `initial_prompt`
      so recurring names transcribe correctly (the loop!)
- [x] Campaign timeline view (sessions × markers)
- [x] "Campaign memory" cross-references on archive pages ("Fort Robespierre
      also came up in Sessions 3 and 5")
- [ ] AI recap/summary/open-threads generation — pluggable backend (local
      model or OpenAI-compatible API), filling the placeholder sections in
      the Markdown export; also upgrades entity extraction
- [x] Obsidian-vault-shaped export (zip of linked session + entity pages)
- [ ] LiveKit option (SFU + TURN) behind a compose profile
- [ ] Postgres option in compose

## Phase 3 — Podcast tools

- [ ] Sample-accurate speaker alignment in the mixdown (use chunk `offset_s`
      with `adelay`, verify drift over 4-hour sessions)
- [ ] Loudness normalization (EBU R128 via `loudnorm`) + silence trimming
- [ ] Intro/outro slots, chapter markers from scene markers (ID3/MP4 chapters)
- [ ] Per-speaker WAV export for DAW editing
- [ ] Prior art: [Craig](https://github.com/CraigChat/craig) (Discord
      multi-track recorder) — study its "cook" post-processing pipeline
      (per-track normalization, format conversion, smart mixdown)

## Phase 4 — Integrations

- [ ] RSS podcast feed
- [ ] GitHub commit export of session pages
- [ ] Foundry VTT journal export
- [ ] Discord import (historical recordings/logs). Target
      [Craig](https://github.com/CraigChat/craig) multi-track exports as the
      primary ingest format — per-speaker tracks map 1:1 onto Tablecast's
      recording model, so old Craig sessions can join the archive and get
      transcribed like native ones
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
