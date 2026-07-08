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


def have_ffmpeg() -> bool:
    import shutil
    return shutil.which("ffmpeg") is not None


def make_opus_chunk(seconds: float, freq: int, path: str) -> bytes:
    """A real webm/opus blob, like MediaRecorder produces."""
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
         "-c:a", "libopus", "-b:a", "48k", path],
        check=True,
    )
    with open(path, "rb") as f:
        return f.read()


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


GH_PORT = int(os.environ.get("SMOKE_PORT", "8399")) + 2
GH_PUT_FILES = {}  # path -> content (decoded)


def start_stub_github():
    """Minimal GitHub Contents API: GET returns 404 (new file), PUT records
    the decoded content and returns 201."""
    import base64
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"message":"Not Found"}')

        def do_PUT(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = json.loads(raw)
            path = self.path.split("/contents/", 1)[1].split("?")[0]
            GH_PUT_FILES[path] = base64.b64decode(body["content"]).decode()
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"content":{"sha":"deadbeef"}}')

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", GH_PORT), Handler)
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
        TABLECAST_FINALIZE_DELAY_S="0",
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

            # whisper: private to sender + target
            pl_id = pl_hello["you"]
            await gm_ws.send(json.dumps({"type": "whisper", "to": pl_id, "text": "secret plan"}))
            msg = json.loads(await pl_ws.recv())
            step("whisper reaches target",
                 msg["type"] == "whisper" and msg["text"] == "secret plan")
            msg = json.loads(await gm_ws.recv())
            step("whisper echoes to sender", msg["type"] == "whisper")

            # whisper to someone not in the room -> error
            await gm_ws.send(json.dumps({"type": "whisper", "to": 99999, "text": "x"}))
            msg = json.loads(await gm_ws.recv())
            step("whisper to absent target errors", msg["type"] == "error")

            # moderation: player forbidden, GM can mute/deafen the player
            await pl_ws.send(json.dumps({"type": "moderate", "target": hello["you"], "action": "mute"}))
            msg = json.loads(await pl_ws.recv())
            step("player can't moderate", msg["type"] == "error")
            await gm_ws.send(json.dumps({"type": "moderate", "target": pl_id, "action": "mute"}))
            msg = json.loads(await pl_ws.recv())
            step("gm mutes player",
                 msg["type"] == "moderate" and msg["action"] == "mute" and msg["target"] == pl_id)
            in_peers = [p for p in msg["peers"] if p["user_id"] == pl_id][0]
            step("presence shows gm-muted", in_peers["gm_muted"] is True)
            await gm_ws.recv()  # gm's copy of the broadcast
            await gm_ws.send(json.dumps({"type": "moderate", "target": pl_id, "action": "deafen"}))
            msg = json.loads(await pl_ws.recv())
            step("gm deafens player", msg["action"] == "deafen")
            await gm_ws.recv()

            # image upload broadcasts to the room
            png = bytes.fromhex(
                "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
                "0000000d4944415478da63fcffff3f030005fe02fea7566a400000000049454e44ae426082"
            )
            r = player.post(f"/sessions/{sid}/images",
                            files={"file": ("map.png", png, "image/png")})
            step("image upload accepted", r.status_code == 200 and r.json()["ok"])
            image_url = r.json()["url"]
            msg = json.loads(await gm_ws.recv())
            step("image broadcast", msg["type"] == "image" and msg["url"] == image_url)
            await pl_ws.recv()
            r = player.post(f"/sessions/{sid}/images",
                            files={"file": ("x.txt", b"nope", "text/plain")})
            step("non-image rejected", r.status_code == 415)
            r = gm.get(image_url)
            step("member can fetch image", r.status_code == 200 and r.content == png)
            r = httpx.get(BASE + image_url, follow_redirects=False)
            step("anonymous image fetch blocked", r.status_code == 303)

    # reconnecting mid-session replays history (chat sent earlier is there,
    # and the GM's own whisper too)
    async with websockets.connect(ws_url, additional_headers={"Cookie": gm_cookie}) as re_ws:
        json.loads(await re_ws.recv())  # peers
        hist2 = json.loads(await re_ws.recv())
        step("reconnect replays chat history", any(
            e["kind"] == "chat" and e["payload"]["text"] == "Hello table!"
            for e in hist2["events"]
        ))
        step("whisper replayed to participant", any(
            e["kind"] == "whisper" and e["payload"]["text"] == "secret plan"
            for e in hist2["events"]
        ))

    # a third member must NOT see the whisper in history
    third = httpx.Client(base_url=BASE, follow_redirects=False)
    third.post("/register", data={"name": "Annette", "email": "annette@example.com",
                                  "password": "password123"})
    third.post("/campaigns/join", data={"join_code": code})
    third_cookie = f"tablecast_session={third.cookies['tablecast_session']}"
    async with websockets.connect(ws_url, additional_headers={"Cookie": third_cookie}) as t_ws:
        json.loads(await t_ws.recv())  # peers
        hist3 = json.loads(await t_ws.recv())
        step("whisper hidden from third party", not any(
            e["kind"] == "whisper" for e in hist3["events"]
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
    step("archive shows whisper to participant", "secret plan" in r.text)
    step("archive shows shared image", "map.png" in r.text or "/images/" in r.text)
    r = third.get(session_url)
    step("archive hides whisper from third party",
         r.status_code == 200 and "secret plan" not in r.text)

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

    # --- Phase 4: Foundry export + RSS feed ---
    r = gm.get(f"/campaigns/{cid}/foundry.json")
    step("foundry export downloads", r.status_code == 200
         and r.headers["content-type"].startswith("application/json"))
    foundry = r.json()
    step("foundry has session journal + cast",
         isinstance(foundry, list)
         and any(e["name"].startswith("Session 1") for e in foundry)
         and any("Cast & Places" in e["name"] for e in foundry))
    session_entry = [e for e in foundry if e["name"].startswith("Session 1")][0]
    step("foundry session has recap + transcript pages",
         any(p["name"] == "Recap" for p in session_entry["pages"])
         and any(p["name"] == "Transcript" for p in session_entry["pages"]))
    cast_entry = [e for e in foundry if "Cast & Places" in e["name"]][0]
    step("foundry cast lists entities",
         any(p["name"] == "Judith Dumont" for p in cast_entry["pages"]))

    # feed token appears on the campaign page for the GM; grab it
    r = gm.get(campaign_url)
    m = re.search(r"/feeds/([\w-]+)/podcast\.xml", r.text)
    step("feed url shown to gm", m is not None)
    token = m.group(1)
    step("feed hidden from players", "/feeds/" not in third.get(campaign_url).text)

    # public feed: no auth, valid RSS. No episode yet (podcast not built here).
    r = httpx.get(BASE + f"/feeds/{token}/podcast.xml", follow_redirects=False)
    step("public feed reachable without auth",
         r.status_code == 200 and r.headers["content-type"].startswith("application/rss+xml"))
    step("feed is valid rss for the campaign",
         "<rss" in r.text and "Port Sainte Jeanne" in r.text)
    r = httpx.get(BASE + "/feeds/bogustoken/podcast.xml")
    step("bogus feed token 404s", r.status_code == 404)

    # rotating the token invalidates the old URL
    r = gm.post(f"/campaigns/{cid}/feed/rotate")
    step("gm rotates feed token", r.status_code == 303)
    r = httpx.get(BASE + f"/feeds/{token}/podcast.xml")
    step("old feed url dead after rotate", r.status_code == 404)
    r = player.post(f"/campaigns/{cid}/feed/rotate")
    step("player can't rotate feed", r.status_code == 403)

    # --- Phase 4: GitHub commit export (against a stub Contents API) ---
    r = player.post(f"/campaigns/{cid}/github/config",
                    data={"repo": "x/y", "token": "t"})
    step("player can't configure github", r.status_code == 403)
    r = gm.post(f"/campaigns/{cid}/github/config",
                data={"repo": "badrepo", "token": "t"})
    step("github config rejects bad repo", r.status_code == 400)
    r = gm.post(f"/campaigns/{cid}/github/config",
                data={"repo": "me/campaign", "token": "ghp_stubtoken",
                      "path_prefix": "sessions",
                      "api_base": f"http://127.0.0.1:{GH_PORT}"})
    step("gm configures github export", r.status_code == 303)
    GH_PUT_FILES.clear()
    r = gm.post(f"/campaigns/{cid}/github/push")
    step("gm triggers github push", r.status_code == 303)
    for _ in range(40):
        if any(p.startswith("sessions/") for p in GH_PUT_FILES):
            break
        time.sleep(0.25)
    step("github export committed README + session page",
         "sessions/README.md" in GH_PUT_FILES
         and any(p.endswith(".md") and "README" not in p for p in GH_PUT_FILES),
         str(list(GH_PUT_FILES)))
    step("committed session page has transcript",
         any("customs office" in c for c in GH_PUT_FILES.values()))
    r = gm.get(campaign_url)
    step("github status shown after push", "Pushed" in r.text)
    r = player.post(f"/campaigns/{cid}/github/push")
    step("player can't push to github", r.status_code == 403)

    # --- Phase 3: aligned audio + podcast bundle (needs real ffmpeg) ---
    if have_ffmpeg():
        await podcast_flow(gm, player, cid, gm_cookie)
        # a built episode now appears in the RSS feed as an enclosure
        r = gm.get(campaign_url)
        token2 = re.search(r"/feeds/([\w-]+)/podcast\.xml", r.text).group(1)
        r = httpx.get(BASE + f"/feeds/{token2}/podcast.xml")
        step("feed lists built episode as enclosure",
             "<enclosure" in r.text and "episodes/" in r.text and "audio/mp4" in r.text)
        m = re.search(r'url="[^"]*/feeds/[\w-]+/episodes/(\d+)\.m4a"', r.text)
        step("episode enclosure url present", m is not None)
        r = httpx.get(BASE + f"/feeds/{token2}/episodes/{m.group(1)}.m4a")
        step("episode media served without auth",
             r.status_code == 200 and len(r.content) > 20_000)

        # --- Phase 4: Craig import ---
        await craig_import_flow(gm, player, cid)
    else:
        print("SKIP  podcast + craig import pipeline (no ffmpeg on this machine)")

    print("\nALL SMOKE TESTS PASSED")


async def craig_import_flow(gm, player, cid):
    import io
    import zipfile as zf_mod

    # A Craig-style multi-track zip: one FLAC per speaker. "GM Greta" and
    # "Player Josh" are members; "Randal" is not (should be skipped).
    with tempfile.TemporaryDirectory() as tmp:
        buf = io.BytesIO()
        with zf_mod.ZipFile(buf, "w") as zf:
            for i, (fname, freq) in enumerate([
                ("1-GM Greta_1234.flac", 300),
                ("2-Player Josh.flac", 500),
                ("3-Randal#9.flac", 700),
            ]):
                p = f"{tmp}/t{i}.flac"
                subprocess.run(
                    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                     "-f", "lavfi", "-i", f"sine=frequency={freq}:duration=2",
                     p], check=True)
                zf.write(p, fname)
            zf.writestr("info.txt", "Craig recording")  # non-audio, ignored
        payload = buf.getvalue()

    # player can't import
    r = player.post(f"/campaigns/{cid}/import/craig",
                    files={"file": ("rec.zip", payload, "application/zip")},
                    data={"title": "Imported Session"})
    step("player can't import craig", r.status_code == 403)

    r = gm.post(f"/campaigns/{cid}/import/craig",
                files={"file": ("rec.zip", payload, "application/zip")},
                data={"title": "Imported Session", "date": "2026-06-01"})
    step("gm imports craig zip", r.status_code == 303, r.headers.get("location", ""))
    loc = r.headers["location"]
    step("import reports 2 matched + 1 skipped",
         "imported=2" in loc and "skipped=1" in loc, loc)
    isid = int(loc.split("/sessions/")[1].split("?")[0])

    # session exists, ended, with two speakers' chunks queued for transcription
    r = gm.get(f"/sessions/{isid}")
    step("imported session archive renders", r.status_code == 200 and "Imported Session" in r.text)

    # worker claims the imported tracks (real audio → whisper runs on them)
    for _ in range(60):
        page = gm.get(f"/sessions/{isid}").text
        if "mixed.mp3" in page:
            break
        time.sleep(1)
    step("imported audio finalized to aligned tracks + mix",
         "mixed.mp3" in page and page.count("speaker") >= 1)

    # a zip with no matching members is rejected cleanly
    with tempfile.TemporaryDirectory() as tmp:
        buf = io.BytesIO()
        with zf_mod.ZipFile(buf, "w") as zf:
            p = f"{tmp}/x.flac"
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 "-f", "lavfi", "-i", "sine=frequency=200:duration=1", p], check=True)
            zf.write(p, "1-Nobody.flac")
        r = gm.post(f"/campaigns/{cid}/import/craig",
                    files={"file": ("rec.zip", buf.getvalue(), "application/zip")},
                    data={"title": "No Match"})
    step("craig import with no matches rejected", r.status_code == 400)


async def podcast_flow(gm, player, cid, gm_cookie):
    r = gm.post(f"/campaigns/{cid}/sessions", data={"title": "Session 2 - Audio"})
    sid = int(r.headers["location"].rsplit("/", 1)[1])
    gm.post(f"/sessions/{sid}/start")

    with tempfile.TemporaryDirectory() as tmp:
        c0 = make_opus_chunk(2.0, 440, f"{tmp}/c0.webm")
        c1 = make_opus_chunk(2.0, 660, f"{tmp}/c1.webm")
        c2 = make_opus_chunk(2.0, 880, f"{tmp}/c2.webm")

    ws_url = f"ws://127.0.0.1:{PORT}/ws/sessions/{sid}"
    async with websockets.connect(ws_url, additional_headers={"Cookie": gm_cookie}) as ws:
        await ws.recv()  # peers
        await ws.recv()  # history
        await ws.send(json.dumps({"type": "record", "action": "start"}))
        await ws.recv()  # record broadcast
        await ws.send(json.dumps({"type": "marker", "label": "Combat starts"}))
        await ws.recv()  # marker broadcast

    # GM: two runs separated by a real gap (0-2s tone, silence, 6-8s tone);
    # player: one run starting at 3s. Exercises run grouping + adelay.
    gm.post(f"/sessions/{sid}/chunks", files={"file": ("c.webm", c0, "audio/webm")},
            data={"seq": "0", "offset": "0.0"})
    gm.post(f"/sessions/{sid}/chunks", files={"file": ("c.webm", c1, "audio/webm")},
            data={"seq": "1", "offset": "6.0"})
    player.post(f"/sessions/{sid}/chunks", files={"file": ("c.webm", c2, "audio/webm")},
                data={"seq": "0", "offset": "3.0"})

    gm.post(f"/sessions/{sid}/end")

    def wait_for(pred, seconds=90):
        deadline = time.time() + seconds
        while time.time() < deadline:
            page = gm.get(f"/sessions/{sid}").text
            if "build failed" in page:
                return page
            if pred(page):
                return page
            time.sleep(1)
        return gm.get(f"/sessions/{sid}").text

    page = wait_for(lambda p: "mixed.mp3" in p)
    step("aligned speaker tracks + mixdown built",
         "mixed.mp3" in page and ".ogg" in page)

    r = gm.post(f"/sessions/{sid}/podcast")
    step("gm starts podcast build", r.status_code == 303)
    page = wait_for(lambda p: "episode.m4a" in p)
    step("podcast bundle built",
         "episode.m4a" in page and "chapters.txt" in page
         and "show-notes.md" in page and ".wav" in page, "see archive page")

    # chapters.txt content: session start + the marker
    m = re.search(r'href="(/sessions/%d/recordings/\d+)">chapters\.txt' % sid, page)
    step("chapters link present", m is not None)
    r = gm.get(m.group(1))
    step("chapters content",
         "00:00:00 Session start" in r.text and "Combat starts" in r.text, r.text.strip())

    # the mixed episode is a real, non-trivial file
    m = re.search(r'href="(/sessions/%d/recordings/\d+)">episode\.m4a' % sid, page)
    r = gm.get(m.group(1))
    step("episode.m4a non-trivial", len(r.content) > 20_000, f"{len(r.content)} bytes")
    return sid


if __name__ == "__main__":
    stub_llm = start_stub_llm()
    stub_gh = start_stub_github()
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
            stub_gh.shutdown()
    sys.exit(1 if FAILED else 0)
