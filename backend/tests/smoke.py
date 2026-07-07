"""End-to-end smoke test. Boots its own uvicorn on a fresh temp database,
exercises auth, campaigns, sessions, the room WebSocket (chat, dice,
markers, recording control, RTC relay, history replay), chunk upload, the
worker job API, archive rendering, and the Markdown export.

Run directly (used by CI):  python backend/tests/smoke.py
"""
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import websockets

PORT = int(os.environ.get("SMOKE_PORT", "8399"))
LLM_PORT = PORT + 1
BASE = f"http://127.0.0.1:{PORT}"
WORKER_TOKEN = "smoke-worker-token"
FAILED = False

RECAP_JSON = json.dumps({
    "recap": "The party investigated the customs office in Port Sainte Jeanne.",
    "bullets": ["Combat began at the docks"],
    "npcs": ["Judith Dumont"],
    "locations": ["Fort Robespierre"],
    "open_threads": ["Why does the cargo bear the Dumont seal?"],
})


def start_stub_llm():
    """Minimal OpenAI-compatible /chat/completions endpoint."""
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = json.dumps(
                {"choices": [{"message": {"content": RECAP_JSON}}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", LLM_PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def step(name, ok, detail=""):
    global FAILED
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")
    if not ok:
        FAILED = True
        raise AssertionError(name)


def start_server(data_dir: str) -> subprocess.Popen:
    env = dict(
        os.environ,
        TABLECAST_DATA_DIR=data_dir,
        TABLECAST_SHARED_DIR=data_dir,
        TABLECAST_SECRET_KEY="smoke-secret",
        TABLECAST_WORKER_TOKEN=WORKER_TOKEN,
        TABLECAST_LLM_BASE_URL=f"http://127.0.0.1:{LLM_PORT}",
        TABLECAST_LLM_MODEL="stub-model",
    )
    backend_dir = Path(__file__).resolve().parents[1]
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT)],
        cwd=backend_dir, env=env,
    )
    for _ in range(60):
        try:
            httpx.get(f"{BASE}/healthz", timeout=1).raise_for_status()
            return proc
        except Exception:
            time.sleep(0.5)
    proc.terminate()
    raise SystemExit("server did not become healthy")


async def main():
    gm = httpx.Client(base_url=BASE, follow_redirects=False)
    player = httpx.Client(base_url=BASE, follow_redirects=False)

    # health
    r = gm.get("/healthz")
    step("healthz", r.status_code == 200)

    # register GM + player
    r = gm.post("/register", data={"name": "GM Greta", "email": "gm@example.com", "password": "password123"})
    step("register gm", r.status_code == 303 and "tablecast_session" in r.cookies)
    r = player.post("/register", data={"name": "Player Josh", "email": "josh@example.com", "password": "password123"})
    step("register player", r.status_code == 303)

    # duplicate email rejected
    r = gm.post("/register", data={"name": "x", "email": "gm@example.com", "password": "password123"})
    step("duplicate email rejected", r.status_code == 400)

    # bad login rejected
    r = httpx.post(BASE + "/login", data={"email": "gm@example.com", "password": "wrong"})
    step("bad login rejected", r.status_code == 401)

    # dashboard requires auth
    r = httpx.get(BASE + "/", follow_redirects=False)
    step("dashboard requires auth", r.status_code == 303)

    # create campaign
    r = gm.post("/campaigns", data={"name": "Port Sainte Jeanne", "description": "Test campaign"})
    step("create campaign", r.status_code == 303, r.headers.get("location", ""))
    campaign_url = r.headers["location"]
    cid = int(campaign_url.rsplit("/", 1)[1])

    # GM sees campaign page + invite code
    r = gm.get(campaign_url)
    step("campaign page", r.status_code == 200)
    match = re.search(r"Invite code: <code>([^<]+)</code>", r.text)
    step("invite code shown", match is not None)
    code = match.group(1)

    # player can't view campaign yet
    r = player.get(campaign_url)
    step("non-member blocked", r.status_code == 403)

    # player joins
    r = player.post("/campaigns/join", data={"join_code": code})
    step("player joins", r.status_code == 303)
    r = player.get(campaign_url)
    step("member can view", r.status_code == 200)

    # player can't schedule sessions
    r = player.post(f"/campaigns/{cid}/sessions", data={"title": "nope"})
    step("player can't schedule", r.status_code == 403)

    # GM schedules a session
    r = gm.post(f"/campaigns/{cid}/sessions", data={"title": "Session 1 - The Incident", "scheduled_at": "2026-07-05T19:00"})
    step("schedule session", r.status_code == 303)
    session_url = r.headers["location"]
    sid = int(session_url.rsplit("/", 1)[1])

    # scheduled page renders for both
    r = gm.get(session_url)
    step("scheduled page (gm)", r.status_code == 200 and "Start session" in r.text)
    r = player.get(session_url)
    step("scheduled page (player)", r.status_code == 200 and "Waiting for the GM" in r.text)

    # player can't start
    r = player.post(f"/sessions/{sid}/start")
    step("player can't start", r.status_code == 403)

    # GM starts
    r = gm.post(f"/sessions/{sid}/start")
    step("gm starts session", r.status_code == 303)
    r = gm.get(session_url)
    step("room renders", r.status_code == 200 and 'id="room"' in r.text)

    # --- WebSocket: GM + player in room ---
    gm_cookie = f"tablecast_session={gm.cookies['tablecast_session']}"
    pl_cookie = f"tablecast_session={player.cookies['tablecast_session']}"
    ws_url = f"ws://127.0.0.1:{PORT}/ws/sessions/{sid}"

    # unauthenticated socket rejected
    try:
        async with websockets.connect(ws_url) as bad:
            await bad.recv()
        step("unauth ws rejected", False)
    except websockets.exceptions.ConnectionClosed as e:
        step("unauth ws rejected", e.code == 4401, f"code={e.code}")
    except Exception as e:
        step("unauth ws rejected", "4401" in str(e) or "403" in str(e), str(e))

    async with websockets.connect(ws_url, additional_headers={"Cookie": gm_cookie}) as gm_ws:
        hello = json.loads(await gm_ws.recv())
        step("gm ws peers msg", hello["type"] == "peers" and hello["peers"] == [])
        hist = json.loads(await gm_ws.recv())
        step("gm gets history replay", hist["type"] == "history")

        async with websockets.connect(ws_url, additional_headers={"Cookie": pl_cookie}) as pl_ws:
            pl_hello = json.loads(await pl_ws.recv())
            step("player sees gm as peer", len(pl_hello["peers"]) == 1)
            pl_hist = json.loads(await pl_ws.recv())
            step("player gets history replay", pl_hist["type"] == "history")
            join = json.loads(await gm_ws.recv())
            step("gm gets join presence", join["type"] == "presence" and join["action"] == "join")

            # chat
            await pl_ws.send(json.dumps({"type": "chat", "text": "Hello table!"}))
            msg = json.loads(await gm_ws.recv())
            step("chat broadcast", msg["type"] == "chat" and msg["text"] == "Hello table!")
            await pl_ws.recv()  # player's own echo

            # dice
            await pl_ws.send(json.dumps({"type": "roll", "expression": "2d6+3"}))
            msg = json.loads(await gm_ws.recv())
            step("dice roll", msg["type"] == "roll" and len(msg["rolls"]) == 2 and 5 <= msg["total"] <= 15, f"total={msg['total']}")
            await pl_ws.recv()

            # bad dice expression -> error only to sender
            await pl_ws.send(json.dumps({"type": "roll", "expression": "banana"}))
            msg = json.loads(await pl_ws.recv())
            step("bad dice error", msg["type"] == "error")

            # marker: player forbidden, gm allowed
            await pl_ws.send(json.dumps({"type": "marker", "label": "Combat starts"}))
            msg = json.loads(await pl_ws.recv())
            step("player marker forbidden", msg["type"] == "error")
            await gm_ws.send(json.dumps({"type": "marker", "label": "Combat starts"}))
            msg = json.loads(await pl_ws.recv())
            step("gm marker broadcast", msg["type"] == "marker" and msg["label"] == "Combat starts")
            await gm_ws.recv()

            # recording control
            await gm_ws.send(json.dumps({"type": "record", "action": "start"}))
            msg = json.loads(await pl_ws.recv())
            step("recording start broadcast", msg["type"] == "record" and msg["action"] == "start" and msg["recording_started_at"])
            await gm_ws.recv()

            # rtc relay
            await pl_ws.send(json.dumps({"type": "rtc", "to": hello["you"], "data": {"sdp": {"type": "offer", "fake": True}}}))
            msg = json.loads(await gm_ws.recv())
            step("rtc relay", msg["type"] == "rtc" and msg["data"]["sdp"]["fake"] is True)

    # reconnecting mid-session replays history (chat sent earlier is there)
    async with websockets.connect(ws_url, additional_headers={"Cookie": gm_cookie}) as re_ws:
        json.loads(await re_ws.recv())  # peers
        hist2 = json.loads(await re_ws.recv())
        step("reconnect replays chat history", any(
            e["kind"] == "chat" and e["payload"]["text"] == "Hello table!"
            for e in hist2["events"]
        ))

    # upload a fake audio chunk (real webm not needed for storage test)
    fake = b"\x1a\x45\xdf\xa3" + b"\x00" * 100
    r = player.post(f"/sessions/{sid}/chunks", files={"file": ("c.webm", fake, "audio/webm")}, data={"seq": "0", "offset": "0.0"})
    step("chunk upload", r.status_code == 200 and r.json()["ok"])

    # worker API: bad token rejected
    r = httpx.post(BASE + "/internal/jobs/claim", headers={"X-Worker-Token": "wrong"})
    step("worker bad token", r.status_code == 401)

    # worker claims the chunk, posts result
    r = httpx.post(BASE + "/internal/jobs/claim", headers={"X-Worker-Token": WORKER_TOKEN})
    job = r.json()["job"]
    step("worker claims job", job is not None and job["session_id"] == sid)
    step("claim includes vocabulary prompt",
         "Port Sainte Jeanne" in job["initial_prompt"] and "GM Greta" in job["initial_prompt"])
    r = httpx.get(BASE + f"/internal/jobs/{job['id']}/audio", headers={"X-Worker-Token": WORKER_TOKEN})
    step("worker downloads audio", r.status_code == 200 and r.content == fake)
    r = httpx.post(
        BASE + f"/internal/jobs/{job['id']}/result",
        headers={"X-Worker-Token": WORKER_TOKEN},
        json={"status": "done", "segments": [{
            "start": 0.5, "end": 3.2,
            "text": "We should check the customs office. Judith Dumont mentioned Fort Robespierre again.",
        }]},
    )
    step("worker posts segments", r.status_code == 200)
    r = httpx.post(BASE + "/internal/jobs/claim", headers={"X-Worker-Token": WORKER_TOKEN})
    step("queue empty after done", r.json()["job"] is None)

    # GM ends session
    r = gm.post(f"/sessions/{sid}/end")
    step("end session", r.status_code == 303)
    await asyncio.sleep(1.0)  # let finalize thread run (ffmpeg absent here — should degrade)

    # archive renders with transcript + events
    r = gm.get(session_url)
    step("archive renders", r.status_code == 200 and "customs office" in r.text and "Combat starts" in r.text)

    # markdown export
    r = gm.get(f"/sessions/{sid}/export.md")
    ok = (r.status_code == 200
          and "# Session 1 - The Incident" in r.text
          and "Combat starts" in r.text
          and "customs office" in r.text
          and "2d6+3" in r.text
          and "GM Greta" in r.text and "Player Josh" in r.text)
    step("markdown export", ok)
    print("---- export preview ----")
    print("\n".join(r.text.splitlines()[:14]))

    # ws to ended session rejected
    try:
        async with websockets.connect(ws_url, additional_headers={"Cookie": gm_cookie}) as ws:
            await ws.recv()
        step("ws to ended session rejected", False)
    except Exception as e:
        step("ws to ended session rejected", "403" in str(e) or "4403" in str(e), str(e))

    # --- Phase 2: search, campaign memory, vault export ---
    r = gm.get(f"/campaigns/{cid}/search", params={"q": "Robespierre"})
    step("fts search finds transcript", r.status_code == 200 and "<mark>Robespierre</mark>" in r.text)
    r = gm.get(f"/campaigns/{cid}/search", params={"q": "hello table"})
    step("fts search finds chat", r.status_code == 200 and "Hello" in r.text)
    r = gm.get(f"/campaigns/{cid}/search", params={"q": "nonexistentword12345"})
    step("fts search empty result", r.status_code == 200 and "Nothing found" in r.text)

    # archive lazily extracted entities -> campaign memory on both pages
    r = gm.get(session_url)
    step("archive shows campaign memory", "Judith Dumont" in r.text and "Fort Robespierre" in r.text)
    r = gm.get(campaign_url)
    step("campaign glossary", "Campaign memory" in r.text and "Judith Dumont" in r.text)

    # entity names now feed the whisper prompt for future chunks
    import io
    import zipfile as zf_mod
    r = gm.get(f"/campaigns/{cid}/export.zip")
    step("vault export downloads", r.status_code == 200 and r.headers["content-type"] == "application/zip")
    names = zf_mod.ZipFile(io.BytesIO(r.content)).namelist()
    step("vault has session + entity pages",
         any(n.startswith("Sessions/") for n in names)
         and "Entities/Judith Dumont.md" in names
         and "Port Sainte Jeanne.md" in names,
         str(names))

    # --- AI recap (against the stub LLM) ---
    r = player.post(f"/sessions/{sid}/recap")
    step("player can't generate recap", r.status_code == 403)
    r = gm.post(f"/sessions/{sid}/recap")
    step("gm generates recap", r.status_code == 303)
    r = gm.get(session_url)
    step("archive shows recap",
         "investigated the customs office" in r.text
         and "Why does the cargo bear the Dumont seal?" in r.text)
    r = gm.get(f"/sessions/{sid}/export.md")
    step("export includes recap sections",
         "investigated the customs office" in r.text
         and "- Judith Dumont" in r.text
         and "- Fort Robespierre" in r.text
         and "- Why does the cargo bear the Dumont seal?" in r.text)

    print("\nALL SMOKE TESTS PASSED")


if __name__ == "__main__":
    stub_llm = start_stub_llm()
    with tempfile.TemporaryDirectory() as tmp:
        server = start_server(tmp)
        try:
            asyncio.run(main())
        except AssertionError:
            pass
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
            stub_llm.shutdown()
    sys.exit(1 if FAILED else 0)
