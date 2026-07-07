/* Tablecast session room client.
 *
 * - WebSocket to the backend for chat / dice / markers / presence /
 *   transcript / WebRTC signaling.
 * - Full-mesh WebRTC audio between participants (newcomer sends offers).
 * - When the GM starts recording, every client records its own mic with
 *   MediaRecorder and uploads independently decodable ~20s chunks.
 */
(() => {
  const root = document.getElementById("room");
  const SESSION_ID = Number(root.dataset.sessionId);
  const MY_ID = Number(root.dataset.userId);
  const IS_GM = root.dataset.isGm === "1";
  const CHUNK_SECONDS = 20;

  const $ = (id) => document.getElementById(id);
  const chatBox = $("chat");
  const transcriptBox = $("transcript");
  const participantsList = $("participants");

  let socket = null;
  let micStream = null;
  const peers = new Map(); // user_id -> RTCPeerConnection
  let muted = false;
  let deafened = false;
  let gmMuted = false;    // GM force-muted me
  let gmDeafened = false; // GM force-deafened me

  function applyMicState() {
    if (micStream) {
      micStream.getAudioTracks().forEach((t) => (t.enabled = !muted && !gmMuted));
    }
    const btn = $("btn-mute");
    btn.disabled = gmMuted;
    btn.textContent = gmMuted ? "Muted by GM" : muted ? "Unmute" : "Mute";
  }

  function applyDeafenState() {
    const off = deafened || gmDeafened;
    document.querySelectorAll("#audio-sinks audio").forEach((a) => (a.muted = off));
    const btn = $("btn-deafen");
    btn.disabled = gmDeafened;
    btn.textContent = gmDeafened ? "Deafened by GM" : deafened ? "Undeafen" : "Deafen";
  }

  // ---- recording state ----
  let recording = false;      // this client is capturing its own mic
  let roomRecording = false;  // the session-wide recording clock is running
  let recorder = null;
  let chunkTimer = null;
  let seq = 0;
  let recordingStartEpoch = null; // ms epoch of global recording start
  let currentChunkOffset = 0;     // seconds since recording start, for the chunk in flight

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function appendChat(node) {
    chatBox.appendChild(node);
    chatBox.scrollTop = chatBox.scrollHeight;
  }

  function hms(seconds) {
    if (seconds === null || seconds === undefined) return "";
    const s = Math.floor(seconds);
    const m = Math.floor(s / 60) % 60, h = Math.floor(s / 3600);
    return `[${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}] `;
  }

  // ---------------- microphone ----------------
  let micError = "";

  async function initMic() {
    // getUserMedia only exists in secure contexts (HTTPS or localhost) —
    // over plain http:// on a LAN IP the whole API is missing.
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      micError =
        "Microphone blocked: browsers only allow mic access over HTTPS or on " +
        "localhost. Open Tablecast via https:// (reverse proxy) or a localhost " +
        "tunnel — voice and recording are disabled until then.";
      $("voice-status").textContent = "mic blocked — needs HTTPS or localhost";
      appendChat(el("p", "error small", micError));
      return;
    }
    try {
      micStream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true },
      });
      $("voice-status").textContent = "mic ready";
      watchSelfLevels(micStream);
    } catch (err) {
      micError =
        err.name === "NotAllowedError"
          ? "Microphone permission denied — allow mic access for this site and reload."
          : "Microphone unavailable (" + err.name + ") — voice and recording disabled.";
      $("voice-status").textContent = "mic unavailable (" + err.name + ")";
      appendChat(el("p", "error small", micError));
    }
  }

  // ---------------- audio level meters ----------------
  // Per-participant VU meters make feedback and audio bleed visible: if two
  // meters move in lockstep while one person talks, someone's speakers are
  // leaking into their mic.
  //
  // Own mic: WebAudio analyser on the local stream (shows exactly what gets
  // sent/recorded — drops to zero when muted).
  // Remote peers: RTCPeerConnection.getStats() inbound audioLevel, which
  // rides the RTP audio-level header and works even where the browser can't
  // tap remote audio into WebAudio.
  let audioCtx = null;
  let selfMeter = null; // {analyser, data}

  function ensureAudioCtx() {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return null;
    if (!audioCtx) audioCtx = new AC();
    if (audioCtx.state === "suspended") audioCtx.resume();
    return audioCtx;
  }

  // Autoplay policy can leave the context suspended until a user gesture.
  document.addEventListener("click", () => {
    if (audioCtx && audioCtx.state === "suspended") audioCtx.resume();
  });

  function watchSelfLevels(stream) {
    const ctx = ensureAudioCtx();
    if (!ctx || !stream.getAudioTracks().length) return;
    try {
      const source = ctx.createMediaStreamSource(stream);
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 512;
      source.connect(analyser);
      selfMeter = { analyser, data: new Uint8Array(analyser.fftSize) };
    } catch (err) {
      console.error("self level meter failed", err);
    }
  }

  function selfLevel() {
    selfMeter.analyser.getByteTimeDomainData(selfMeter.data);
    let peak = 0;
    for (let i = 0; i < selfMeter.data.length; i++) {
      const v = Math.abs(selfMeter.data[i] - 128);
      if (v > peak) peak = v;
    }
    return Math.min(1, peak / 128);
  }

  function setMeter(userId, level) {
    const fill = document.getElementById("meter-" + userId);
    if (!fill) return;
    fill.style.width = Math.round(Math.min(1, level) * 100) + "%";
    fill.classList.toggle("hot", level > 0.85);
    const item = fill.closest(".participant");
    if (item) item.classList.toggle("speaking", level > 0.12);
  }

  function updateMeters() {
    if (selfMeter) setMeter(MY_ID, selfLevel());
    for (const [peerId, pc] of peers) {
      pc.getStats().then((stats) => {
        let level = 0;
        stats.forEach((s) => {
          if (s.type === "inbound-rtp" && s.kind === "audio" && s.audioLevel !== undefined) {
            level = Math.max(level, s.audioLevel);
          }
        });
        // audioLevel is linear amplitude; sqrt ≈ perceived loudness so quiet
        // speech still registers visibly.
        setMeter(peerId, Math.sqrt(level));
      }).catch(() => {});
    }
  }

  setInterval(updateMeters, 150);

  // ---------------- WebRTC mesh ----------------
  let iceServers = [{ urls: "stun:stun.l.google.com:19302" }];
  try {
    iceServers = JSON.parse(root.dataset.iceServers) || iceServers;
  } catch (e) { /* fall back to default STUN */ }

  function newPeer(peerId, initiator) {
    if (peers.has(peerId)) return peers.get(peerId);
    const pc = new RTCPeerConnection({ iceServers });
    peers.set(peerId, pc);

    if (micStream) {
      for (const track of micStream.getAudioTracks()) pc.addTrack(track, micStream);
    }

    pc.onicecandidate = (e) => {
      if (e.candidate) send({ type: "rtc", to: peerId, data: { candidate: e.candidate } });
    };
    pc.ontrack = (e) => {
      let audio = document.getElementById("audio-" + peerId);
      if (!audio) {
        audio = document.createElement("audio");
        audio.id = "audio-" + peerId;
        audio.autoplay = true;
        $("audio-sinks").appendChild(audio);
      }
      audio.srcObject = e.streams[0];
      audio.muted = deafened || gmDeafened;
    };
    pc.onconnectionstatechange = () => {
      if (["failed", "closed", "disconnected"].includes(pc.connectionState)) {
        closePeer(peerId);
      }
    };

    if (initiator) {
      pc.onnegotiationneeded = async () => {
        try {
          await pc.setLocalDescription(await pc.createOffer());
          send({ type: "rtc", to: peerId, data: { sdp: pc.localDescription } });
        } catch (err) { console.error("offer failed", err); }
      };
      // No mic -> onnegotiationneeded won't fire; still create a recv-only offer.
      if (!micStream) {
        pc.addTransceiver("audio", { direction: "recvonly" });
      }
    }
    return pc;
  }

  function closePeer(peerId) {
    const pc = peers.get(peerId);
    if (pc) { pc.close(); peers.delete(peerId); }
    setMeter(peerId, 0);
    const audio = document.getElementById("audio-" + peerId);
    if (audio) audio.remove();
  }

  async function onRtc(fromId, data) {
    const pc = newPeer(fromId, false);
    try {
      if (data.sdp) {
        await pc.setRemoteDescription(data.sdp);
        if (data.sdp.type === "offer") {
          await pc.setLocalDescription(await pc.createAnswer());
          send({ type: "rtc", to: fromId, data: { sdp: pc.localDescription } });
        }
      } else if (data.candidate) {
        await pc.addIceCandidate(data.candidate);
      }
    } catch (err) { console.error("rtc signal failed", err); }
  }

  // ---------------- recording ----------------
  function pickMime() {
    for (const m of ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"]) {
      if (MediaRecorder.isTypeSupported(m)) return m;
    }
    return "";
  }

  function startRecording(startedAtIso) {
    if (recording) return;
    if (!micStream) {
      // The room is recording, but this participant can't contribute audio.
      $("rec-status").textContent = "● room recording — your mic is off";
      $("rec-status").classList.add("on");
      if (IS_GM) $("btn-record").textContent = "Stop recording";
      if (micError) appendChat(el("p", "error small", "You are NOT being recorded. " + micError));
      return;
    }
    recording = true;
    recordingStartEpoch = startedAtIso ? Date.parse(startedAtIso) : Date.now();
    seq = 0;
    startChunk();
    chunkTimer = setInterval(rotateChunk, CHUNK_SECONDS * 1000);
    $("rec-status").textContent = "● RECORDING";
    $("rec-status").classList.add("on");
    if (IS_GM) $("btn-record").textContent = "Stop recording";
  }

  function startChunk() {
    currentChunkOffset = Math.max(0, (Date.now() - recordingStartEpoch) / 1000);
    const rec = new MediaRecorder(micStream, { mimeType: pickMime() });
    recorder = rec;
    const parts = [];
    const offset = currentChunkOffset;
    const mySeq = seq++;
    rec.ondataavailable = (e) => { if (e.data.size) parts.push(e.data); };
    rec.onstop = () => {
      if (parts.length) uploadChunk(new Blob(parts, { type: rec.mimeType }), mySeq, offset);
    };
    rec.start();
  }

  function rotateChunk() {
    if (recorder && recorder.state === "recording") recorder.stop();
    if (recording) startChunk();
  }

  function stopRecording() {
    if (!recording) {
      // Mic-less clients still show the room-level indicator; clear it.
      $("rec-status").textContent = "● not recording";
      $("rec-status").classList.remove("on");
      if (IS_GM) $("btn-record").textContent = "Start recording";
      return;
    }
    recording = false;
    clearInterval(chunkTimer);
    if (recorder && recorder.state === "recording") recorder.stop();
    recorder = null;
    $("rec-status").textContent = "● not recording";
    $("rec-status").classList.remove("on");
    if (IS_GM) $("btn-record").textContent = "Start recording";
  }

  const pendingUploads = new Set();

  async function uploadChunk(blob, chunkSeq, offset, attempt = 0) {
    const form = new FormData();
    form.append("file", blob, `chunk-${chunkSeq}.webm`);
    form.append("seq", String(chunkSeq));
    form.append("offset", String(offset));
    const promise = (async () => {
      try {
        const res = await fetch(`/sessions/${SESSION_ID}/chunks`, { method: "POST", body: form });
        if (!res.ok) throw new Error("HTTP " + res.status);
      } catch (err) {
        if (attempt < 3) {
          setTimeout(() => uploadChunk(blob, chunkSeq, offset, attempt + 1), 2000 * (attempt + 1));
        } else {
          console.error("chunk upload failed permanently", chunkSeq, err);
        }
      }
    })();
    pendingUploads.add(promise);
    promise.finally(() => pendingUploads.delete(promise));
  }

  // ---------------- UI rendering ----------------
  let roomPeers = []; // last presence snapshot (for whisper name lookup)

  function participantRow(userId, label, info) {
    const li = el("li", "participant");
    li.appendChild(el("span", "p-name", label));
    const meter = el("div", "meter");
    const fill = el("div", "meter-fill");
    fill.id = "meter-" + userId;
    meter.appendChild(fill);
    li.appendChild(meter);
    // GM moderation buttons on every row except the GM's own
    if (IS_GM && userId !== MY_ID && info) {
      const muteBtn = el("button", "mod-btn", info.gm_muted ? "🔊" : "🔇");
      muteBtn.title = info.gm_muted ? "Unmute this player" : "Mute this player";
      muteBtn.addEventListener("click", () =>
        send({ type: "moderate", target: userId,
               action: info.gm_muted ? "unmute" : "mute" }));
      const deafBtn = el("button", "mod-btn", info.gm_deafened ? "🔔" : "🔕");
      deafBtn.title = info.gm_deafened ? "Undeafen this player" : "Deafen this player";
      deafBtn.addEventListener("click", () =>
        send({ type: "moderate", target: userId,
               action: info.gm_deafened ? "undeafen" : "deafen" }));
      li.appendChild(muteBtn);
      li.appendChild(deafBtn);
    }
    return li;
  }

  function stateBadge(info) {
    let badge = "";
    if (info.gm_muted) badge += " 🔇GM";
    else if (info.muted) badge += " 🔇";
    if (info.gm_deafened) badge += " 🔕GM";
    return badge;
  }

  function renderParticipants(peersInfo) {
    roomPeers = peersInfo;
    participantsList.innerHTML = "";
    const meInfo = peersInfo.find((p) => p.user_id === MY_ID)
      || { muted, gm_muted: gmMuted, gm_deafened: gmDeafened };
    participantsList.appendChild(
      participantRow(MY_ID, root.dataset.userName + " (you)" + stateBadge(meInfo), null)
    );
    for (const p of peersInfo) {
      if (p.user_id === MY_ID) continue;
      participantsList.appendChild(
        participantRow(p.user_id, p.name + stateBadge(p), p)
      );
    }
    $("voice-status").textContent = `${peersInfo.length} in room`;
  }

  function addTranscript(seg) {
    const p = el("p");
    p.appendChild(el("span", "muted small", hms(seg.start_s)));
    p.appendChild(el("strong", null, seg.name + ": "));
    p.appendChild(document.createTextNode(seg.text));
    transcriptBox.appendChild(p);
    transcriptBox.scrollTop = transcriptBox.scrollHeight;
  }

  function renderChatLine(msg) {
    const p = el("p");
    p.appendChild(el("strong", null, msg.name + ": "));
    p.appendChild(document.createTextNode(msg.text));
    appendChat(p);
  }

  function renderRollLine(msg) {
    const p = el("p", "roll-line");
    p.appendChild(el("strong", null, `🎲 ${msg.name} `));
    p.appendChild(document.createTextNode(
      `${msg.expression} → ${msg.total} (${msg.rolls.join(", ")})`));
    appendChat(p);
  }

  function renderMarkerLine(msg) {
    appendChat(el("p", "marker-line", `🎬 ${hms(msg.at_seconds)}${msg.label}`));
  }

  function renderWhisperLine(msg) {
    const p = el("p", "whisper-line");
    const label = msg.user_id === MY_ID
      ? `🤫 to ${msg.to_name}: `
      : `🤫 ${msg.name} whispers: `;
    p.appendChild(el("strong", null, label));
    p.appendChild(document.createTextNode(msg.text));
    appendChat(p);
  }

  function renderImageLine(msg) {
    const p = el("p", "image-line");
    p.appendChild(el("strong", null, msg.name + ": "));
    const link = document.createElement("a");
    link.href = msg.url;
    link.target = "_blank";
    link.rel = "noopener";
    const img = document.createElement("img");
    img.src = msg.url;
    img.alt = msg.filename || "shared image";
    img.className = "chat-image";
    img.loading = "lazy";
    img.addEventListener("load", () => { chatBox.scrollTop = chatBox.scrollHeight; });
    link.appendChild(img);
    p.appendChild(link);
    appendChat(p);
  }

  function renderHistory(msg) {
    // Replayed on every (re)connect — wipe the panes so nothing duplicates.
    chatBox.innerHTML = "";
    transcriptBox.innerHTML = "";
    for (const e of msg.events) {
      const entry = { name: e.name, user_id: e.user_id, at_seconds: e.at_seconds, ...e.payload };
      if (e.kind === "chat") renderChatLine(entry);
      else if (e.kind === "whisper") renderWhisperLine(entry);
      else if (e.kind === "image") renderImageLine(entry);
      else if (e.kind === "roll") renderRollLine(entry);
      else if (e.kind === "marker") renderMarkerLine(entry);
      else if (e.kind === "system") appendChat(el("p", "muted small", `— ${e.payload.text} —`));
    }
    msg.segments.forEach(addTranscript);
  }

  function renderTranscribeQueue(pending) {
    $("transcribe-status").textContent =
      pending > 0 ? `· transcribing ${pending} chunk${pending === 1 ? "" : "s"}…` : "";
  }

  // ---------------- WebSocket ----------------
  function send(obj) {
    if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(obj));
  }

  let wsEverConnected = false;
  let wsWarned = false;

  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    socket = new WebSocket(`${proto}://${location.host}/ws/sessions/${SESSION_ID}`);

    socket.onopen = () => {
      wsEverConnected = true;
    };

    socket.onmessage = (event) => {
      const msg = JSON.parse(event.data);
      switch (msg.type) {
        case "peers":
          gmMuted = !!msg.gm_muted;
          gmDeafened = !!msg.gm_deafened;
          applyMicState();
          applyDeafenState();
          renderParticipants([{ user_id: MY_ID, name: root.dataset.userName, muted,
                                gm_muted: gmMuted, gm_deafened: gmDeafened }, ...msg.peers]);
          for (const p of msg.peers) newPeer(p.user_id, true); // newcomer initiates
          if (msg.recording_active) {
            roomRecording = true;
            startRecording(msg.recording_started_at);
          }
          break;
        case "presence":
          renderParticipants(msg.peers);
          if (msg.action === "leave") closePeer(msg.user_id);
          if (msg.action) {
            if (msg.action === "join") appendChat(el("p", "muted small", `${msg.name} joined`));
            if (msg.action === "leave") appendChat(el("p", "muted small", `${msg.name} left`));
          }
          break;
        case "rtc":
          onRtc(msg.from, msg.data);
          break;
        case "history":
          renderHistory(msg);
          break;
        case "chat":
          renderChatLine(msg);
          break;
        case "whisper":
          renderWhisperLine(msg);
          break;
        case "image":
          renderImageLine(msg);
          break;
        case "moderate": {
          if (msg.target === MY_ID) {
            if (msg.action === "mute") gmMuted = true;
            if (msg.action === "unmute") gmMuted = false;
            if (msg.action === "deafen") gmDeafened = true;
            if (msg.action === "undeafen") gmDeafened = false;
            applyMicState();
            applyDeafenState();
            const verb = { mute: "muted", unmute: "unmuted",
                           deafen: "deafened", undeafen: "undeafened" }[msg.action];
            appendChat(el("p", "muted small", `You were ${verb} by ${msg.by}`));
          } else {
            const verb = { mute: "muted", unmute: "unmuted",
                           deafen: "deafened", undeafen: "undeafened" }[msg.action];
            appendChat(el("p", "muted small", `${msg.target_name} was ${verb} by ${msg.by}`));
          }
          renderParticipants(msg.peers);
          break;
        }
        case "roll":
          renderRollLine(msg);
          break;
        case "marker":
          renderMarkerLine(msg);
          break;
        case "transcript":
          msg.segments.forEach(addTranscript);
          break;
        case "transcribe_queue":
          renderTranscribeQueue(msg.pending);
          break;
        case "record":
          roomRecording = msg.action === "start";
          if (roomRecording) startRecording(msg.recording_started_at);
          else stopRecording();
          break;
        case "ended": {
          stopRecording();
          // The recorder's onstop fires async; give it a beat to enqueue the
          // final chunk, then hold the reload until uploads finish.
          const reload = () => location.reload();
          setTimeout(() => {
            Promise.race([
              Promise.allSettled([...pendingUploads]),
              new Promise((r) => setTimeout(r, 15000)),
            ]).then(reload, reload);
          }, 800);
          break;
        }
        case "error":
          appendChat(el("p", "error small", msg.message));
          break;
      }
    };

    socket.onclose = () => {
      $("voice-status").textContent = "disconnected — retrying…";
      if (!wsEverConnected && !wsWarned) {
        wsWarned = true;
        appendChat(el("p", "error small",
          "Can't reach the session server — the WebSocket connection is being " +
          "blocked. If Tablecast is behind a reverse proxy, enable WebSocket " +
          "support for this host (e.g. Nginx Proxy Manager: edit the proxy " +
          "host and toggle on \"Websockets Support\"). Chat, voice, dice, and " +
          "recording all need this connection."));
      }
      for (const id of [...peers.keys()]) closePeer(id);
      setTimeout(connect, 2000);
    };
  }

  // ---------------- controls ----------------
  function sendWhisper(rest) {
    // /w Name message — participant names can contain spaces, so match the
    // longest name that prefixes the rest of the line.
    const candidates = roomPeers
      .filter((p) => p.user_id !== MY_ID)
      .filter((p) => rest.toLowerCase().startsWith(p.name.toLowerCase() + " "))
      .sort((a, b) => b.name.length - a.name.length);
    if (!candidates.length) {
      appendChat(el("p", "error small",
        'Whisper: no matching player. Use "/w Full Name message".'));
      return;
    }
    const target = candidates[0];
    const text = rest.slice(target.name.length + 1).trim();
    if (text) send({ type: "whisper", to: target.user_id, text });
  }

  $("chat-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const input = $("chat-input");
    const text = input.value.trim();
    if (!text) return;
    if (text.startsWith("/roll ")) send({ type: "roll", expression: text.slice(6) });
    else if (text === "adv" || text === "dis") send({ type: "roll", expression: text });
    else if (text.startsWith("/w ")) sendWhisper(text.slice(3).trim());
    else send({ type: "chat", text });
    input.value = "";
  });

  // ---------------- image sharing ----------------
  async function uploadImage(file) {
    if (!file || !file.type.startsWith("image/")) return;
    const form = new FormData();
    form.append("file", file, file.name || "image");
    try {
      const res = await fetch(`/sessions/${SESSION_ID}/images`, { method: "POST", body: form });
      if (!res.ok) {
        const detail = (await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`;
        appendChat(el("p", "error small", `Image upload failed: ${detail}`));
      }
    } catch (err) {
      appendChat(el("p", "error small", "Image upload failed — connection error"));
    }
  }

  $("btn-attach").addEventListener("click", () => $("image-input").click());
  $("image-input").addEventListener("change", (e) => {
    for (const file of e.target.files) uploadImage(file);
    e.target.value = "";
  });

  // drag & drop onto the chat pane
  chatBox.addEventListener("dragover", (e) => {
    e.preventDefault();
    chatBox.classList.add("droppable");
  });
  chatBox.addEventListener("dragleave", () => chatBox.classList.remove("droppable"));
  chatBox.addEventListener("drop", (e) => {
    e.preventDefault();
    chatBox.classList.remove("droppable");
    for (const file of e.dataTransfer.files) uploadImage(file);
  });

  // paste an image into the chat input
  $("chat-input").addEventListener("paste", (e) => {
    for (const item of e.clipboardData.items) {
      if (item.type.startsWith("image/")) {
        e.preventDefault();
        uploadImage(item.getAsFile());
      }
    }
  });

  document.querySelectorAll(".die").forEach((btn) =>
    btn.addEventListener("click", () => send({ type: "roll", expression: btn.dataset.expr }))
  );

  document.querySelectorAll(".marker").forEach((btn) =>
    btn.addEventListener("click", () => send({ type: "marker", label: btn.dataset.label }))
  );

  $("btn-mute").addEventListener("click", () => {
    if (gmMuted) return;
    muted = !muted;
    applyMicState();
    send({ type: "state", muted });
  });

  $("btn-deafen").addEventListener("click", () => {
    if (gmDeafened) return;
    deafened = !deafened;
    applyDeafenState();
  });

  if (IS_GM) {
    $("btn-record").addEventListener("click", () => {
      if (!recording && !micStream && micError) {
        appendChat(el("p", "error small", micError));
      }
      send({ type: "record", action: roomRecording ? "stop" : "start" });
    });
  }

  // ---------------- boot ----------------
  initMic().then(connect);
})();
