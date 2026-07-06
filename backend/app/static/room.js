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

  // ---- recording state ----
  let recording = false;
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
  async function initMic() {
    try {
      micStream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true },
      });
      $("voice-status").textContent = "mic ready";
    } catch (err) {
      $("voice-status").textContent =
        "mic unavailable (" + err.name + ") — voice and recording disabled";
    }
  }

  // ---------------- WebRTC mesh ----------------
  function newPeer(peerId, initiator) {
    if (peers.has(peerId)) return peers.get(peerId);
    const pc = new RTCPeerConnection({
      iceServers: [{ urls: "stun:stun.l.google.com:19302" }],
    });
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
      audio.muted = deafened;
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
    if (recording || !micStream) return;
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
    if (!recording) return;
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
  function renderParticipants(peersInfo) {
    participantsList.innerHTML = "";
    const me = el("li", "participant", root.dataset.userName + " (you)" + (muted ? " 🔇" : ""));
    participantsList.appendChild(me);
    for (const p of peersInfo) {
      if (p.user_id === MY_ID) continue;
      participantsList.appendChild(
        el("li", "participant", p.name + (p.muted ? " 🔇" : ""))
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

  // ---------------- WebSocket ----------------
  function send(obj) {
    if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(obj));
  }

  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    socket = new WebSocket(`${proto}://${location.host}/ws/sessions/${SESSION_ID}`);

    socket.onmessage = (event) => {
      const msg = JSON.parse(event.data);
      switch (msg.type) {
        case "peers":
          renderParticipants([{ user_id: MY_ID, name: root.dataset.userName, muted }, ...msg.peers]);
          for (const p of msg.peers) newPeer(p.user_id, true); // newcomer initiates
          if (msg.recording_active) startRecording(msg.recording_started_at);
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
        case "chat": {
          const p = el("p");
          p.appendChild(el("strong", null, msg.name + ": "));
          p.appendChild(document.createTextNode(msg.text));
          appendChat(p);
          break;
        }
        case "roll": {
          const p = el("p", "roll-line");
          p.appendChild(el("strong", null, `🎲 ${msg.name} `));
          p.appendChild(document.createTextNode(
            `${msg.expression} → ${msg.total} (${msg.rolls.join(", ")})`));
          appendChat(p);
          break;
        }
        case "marker":
          appendChat(el("p", "marker-line", `🎬 ${hms(msg.at_seconds)}${msg.label}`));
          break;
        case "transcript":
          msg.segments.forEach(addTranscript);
          break;
        case "record":
          if (msg.action === "start") startRecording(msg.recording_started_at);
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
      for (const id of [...peers.keys()]) closePeer(id);
      setTimeout(connect, 2000);
    };
  }

  // ---------------- controls ----------------
  $("chat-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const input = $("chat-input");
    const text = input.value.trim();
    if (!text) return;
    if (text.startsWith("/roll ")) send({ type: "roll", expression: text.slice(6) });
    else if (text === "adv" || text === "dis") send({ type: "roll", expression: text });
    else send({ type: "chat", text });
    input.value = "";
  });

  document.querySelectorAll(".die").forEach((btn) =>
    btn.addEventListener("click", () => send({ type: "roll", expression: btn.dataset.expr }))
  );

  document.querySelectorAll(".marker").forEach((btn) =>
    btn.addEventListener("click", () => send({ type: "marker", label: btn.dataset.label }))
  );

  $("btn-mute").addEventListener("click", () => {
    muted = !muted;
    if (micStream) micStream.getAudioTracks().forEach((t) => (t.enabled = !muted));
    $("btn-mute").textContent = muted ? "Unmute" : "Mute";
    send({ type: "state", muted });
  });

  $("btn-deafen").addEventListener("click", () => {
    deafened = !deafened;
    document.querySelectorAll("#audio-sinks audio").forEach((a) => (a.muted = deafened));
    $("btn-deafen").textContent = deafened ? "Undeafen" : "Deafen";
  });

  if (IS_GM) {
    $("btn-record").addEventListener("click", () => {
      send({ type: "record", action: recording ? "stop" : "start" });
    });
  }

  // ---------------- boot ----------------
  initMic().then(connect);
})();
