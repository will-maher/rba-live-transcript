import hashlib
import json
import os

from deepgram import DeepgramClient, LiveOptions, LiveTranscriptionEvents
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

DEEPGRAM_API_KEY = os.environ.get("DEEPGRAM_API_KEY", "")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "verbatim")

# Deterministic token derived from password — survives redeploys
VALID_TOKEN = hashlib.sha256(f"verbatim:{APP_PASSWORD}".encode()).hexdigest()

app = FastAPI()


class LoginRequest(BaseModel):
    password: str


@app.post("/login")
async def login(req: LoginRequest):
    if req.password != APP_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid password")
    return {"token": VALID_TOKEN}


@app.get("/manifest.json")
async def manifest():
    return JSONResponse({
        "name": "Verbatim",
        "short_name": "Verbatim",
        "description": "Live transcription",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#F4F3EE",
        "theme_color": "#1A1917",
        "icons": [
            {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable"}
        ]
    })


@app.get("/icon.svg")
async def icon():
    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <rect width="512" height="512" fill="#1A1917"/>
  <text x="256" y="340" font-family="-apple-system,sans-serif" font-size="260"
        font-weight="300" fill="#F4F3EE" text-anchor="middle">V</text>
</svg>"""
    return Response(svg, media_type="image/svg+xml")


@app.get("/sw.js")
async def service_worker():
    sw = """const CACHE = 'verbatim-v3';
self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.add('/')));
  self.skipWaiting();
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks =>
    Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))
  ));
  self.clients.claim();
});
self.addEventListener('fetch', e => {
  if (e.request.url.includes('/stream') || e.request.url.includes('/login')) return;
  e.respondWith(caches.match(e.request).then(r => r || fetch(e.request)));
});"""
    return Response(sw, media_type="application/javascript")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.websocket("/stream")
async def stream_ws(websocket: WebSocket, token: str = ""):
    if token != VALID_TOKEN:
        await websocket.close(code=1008, reason="Unauthorized")
        return

    await websocket.accept()

    deepgram = DeepgramClient(DEEPGRAM_API_KEY)
    dg = deepgram.listen.asyncwebsocket.v("1")

    async def on_transcript(self, result, **kwargs):
        try:
            text = result.channel.alternatives[0].transcript
            print(f"DG transcript (final={result.is_final}): {text!r}", flush=True)
            if text and result.is_final:
                await websocket.send_json({"type": "transcript", "text": text})
        except Exception as e:
            print(f"on_transcript error: {e}", flush=True)

    async def on_error(self, error, **kwargs):
        print(f"DG error: {error}", flush=True)
        try:
            await websocket.send_json({"type": "error", "text": str(error)})
        except Exception:
            pass

    dg.on(LiveTranscriptionEvents.Transcript, on_transcript)
    dg.on(LiveTranscriptionEvents.Error, on_error)

    options = LiveOptions(
        model="nova-3",
        language="en-AU",
        smart_format=True,
        punctuate=True,
        encoding="linear16",
        channels=1,
        sample_rate=16000,
        interim_results=False,
        endpointing=500,
    )

    try:
        started = await dg.start(options)
    except BaseException as e:
        print(f"dg.start() raised: {type(e).__name__}: {e}", flush=True)
        await websocket.send_json({"type": "error", "text": f"Deepgram start failed: {e}"})
        await websocket.close()
        return

    if not started:
        print("dg.start() returned False", flush=True)
        await websocket.send_json({"type": "error", "text": "Failed to connect to Deepgram — check API key"})
        await websocket.close()
        return

    print("Deepgram started OK", flush=True)
    await websocket.send_json({"type": "status", "text": "Live — transcribing", "live": True})

    chunk_count = 0
    try:
        while True:
            data = await websocket.receive_bytes()
            chunk_count += 1
            if chunk_count <= 3 or chunk_count % 50 == 0:
                print(f"Audio chunk #{chunk_count}: {len(data)} bytes", flush=True)
            await dg.send(data)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_json({"type": "error", "text": str(e)})
        except Exception:
            pass
    finally:
        try:
            await dg.finish()
        except BaseException:
            pass


HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <meta name="apple-mobile-web-app-title" content="Verbatim">
  <link rel="manifest" href="/manifest.json">
  <link rel="apple-touch-icon" href="/icon.svg">
  <title>Verbatim</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg:      #F4F3EE;
      --surface: #FDFCF9;
      --border:  #E2E0D8;
      --text:    #1A1917;
      --muted:   #96948D;
      --red:     #C0392B;
      --green:   #27AE60;
      --r:       6px;
      --sb:      env(safe-area-inset-bottom, 0px);
    }

    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }
    html, body { height: 100%; overscroll-behavior: none; }
    body {
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
      background: var(--bg);
      color: var(--text);
      font-size: 15px;
      line-height: 1.5;
      -webkit-font-smoothing: antialiased;
    }

    /* ─── LOGIN ─────────────────────────────────── */
    #login-view {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      min-height: 100dvh;
      padding: 40px 32px;
    }
    .login-wordmark {
      font-size: 12px;
      font-weight: 500;
      letter-spacing: 0.18em;
      text-transform: uppercase;
      margin-bottom: 56px;
    }
    .login-field { width: 100%; max-width: 300px; display: flex; flex-direction: column; gap: 10px; }
    .field-label { font-size: 11px; font-weight: 500; letter-spacing: 0.1em; text-transform: uppercase; color: var(--muted); }
    input[type="password"] {
      width: 100%; padding: 13px 14px; font-family: inherit; font-size: 15px;
      color: var(--text); background: var(--surface); border: 1px solid var(--border);
      border-radius: var(--r); outline: none; transition: border-color 0.15s; -webkit-appearance: none;
    }
    input[type="password"]:focus { border-color: var(--text); }
    .btn-block {
      width: 100%; padding: 13px; font-family: inherit; font-size: 13px; font-weight: 500;
      letter-spacing: 0.05em; color: #FFF; background: var(--text); border: none;
      border-radius: var(--r); cursor: pointer; transition: opacity 0.15s; margin-top: 4px;
    }
    .btn-block:active { opacity: 0.72; }
    .login-err { font-size: 12px; color: var(--red); min-height: 16px; text-align: center; }
    @keyframes shake {
      0%,100%{transform:translateX(0)} 20%{transform:translateX(-7px)}
      40%{transform:translateX(7px)} 60%{transform:translateX(-4px)} 80%{transform:translateX(4px)}
    }
    .shake { animation: shake 0.38s ease; }

    /* ─── APP SHELL ──────────────────────────────── */
    #app { display: none; flex-direction: column; height: 100dvh; }
    header {
      display: flex; align-items: flex-end; justify-content: space-between;
      padding: 18px 24px 0; border-bottom: 1px solid var(--border);
      background: var(--bg); position: sticky; top: 0; z-index: 20; flex-shrink: 0;
    }
    .wordmark { font-size: 11px; font-weight: 500; letter-spacing: 0.18em; text-transform: uppercase; padding-bottom: 14px; }
    nav { display: flex; }
    .tab {
      padding: 14px 0; margin-left: 28px; font-size: 13px; font-weight: 400;
      color: var(--muted); cursor: pointer; border-bottom: 1.5px solid transparent;
      transition: color 0.15s, border-color 0.15s; user-select: none;
    }
    .tab.active { color: var(--text); border-bottom-color: var(--text); }
    .scroll-area { flex: 1; overflow-y: auto; -webkit-overflow-scrolling: touch; }

    /* ─── NEW SESSION ────────────────────────────── */
    #view-new { padding: 32px 24px; display: flex; flex-direction: column; gap: 22px; }
    .label { font-size: 11px; font-weight: 500; letter-spacing: 0.1em; text-transform: uppercase; color: var(--muted); margin-bottom: 8px; }

    /* Source toggle */
    .source-toggle { display: flex; background: var(--surface); border: 1px solid var(--border); border-radius: var(--r); overflow: hidden; }
    .source-opt {
      flex: 1; padding: 11px 8px; font-family: inherit; font-size: 13px; font-weight: 500;
      color: var(--muted); background: none; border: none; cursor: pointer;
      transition: all 0.15s; text-align: center;
    }
    .source-opt.active { background: var(--text); color: #FFF; }
    .source-opt:active { opacity: 0.75; }

    /* Duration */
    .dur-row { display: flex; align-items: center; gap: 10px; }
    input[type="number"] {
      width: 76px; padding: 11px 12px; font-family: inherit; font-size: 15px;
      color: var(--text); background: var(--surface); border: 1px solid var(--border);
      border-radius: var(--r); outline: none; -webkit-appearance: none; transition: border-color 0.15s;
    }
    input[type="number"]:focus { border-color: var(--text); }
    .dur-label { font-size: 13px; color: var(--muted); }

    /* Controls */
    .controls { display: flex; gap: 8px; }
    .btn {
      flex: 1; padding: 12px 10px; font-family: inherit; font-size: 13px; font-weight: 500;
      letter-spacing: 0.04em; border: 1px solid var(--border); border-radius: var(--r);
      background: var(--surface); color: var(--text); cursor: pointer; transition: opacity 0.15s;
    }
    .btn:active { opacity: 0.65; }
    .btn.dark { background: var(--text); color: #FFF; border-color: var(--text); }
    .btn.stop { background: var(--red); color: #FFF; border-color: var(--red); }

    /* Status */
    .status { display: flex; align-items: center; gap: 8px; min-height: 18px; }
    .dot { width: 6px; height: 6px; border-radius: 50%; background: var(--green); flex-shrink: 0; opacity: 0; transition: opacity 0.2s; }
    @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.15} }
    .dot.on { opacity: 1; animation: pulse 1.8s infinite; }
    .status-msg { font-size: 12px; color: var(--muted); }
    .countdown { font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; margin-left: auto; }
    .countdown.warn { color: var(--red); }

    /* Transcript box */
    .transcript-box {
      background: var(--surface); border: 1px solid var(--border); border-radius: var(--r);
      padding: 22px; min-height: 220px; max-height: 52dvh; overflow-y: auto;
      font-family: 'Georgia', 'Times New Roman', serif; font-size: 16px; line-height: 1.8;
      color: var(--text); word-break: break-word;
    }
    .placeholder { font-family: 'Inter', sans-serif; font-size: 13px; color: var(--muted); }

    /* Hint box */
    .hint {
      background: var(--surface); border: 1px solid var(--border); border-radius: var(--r);
      padding: 14px 16px; font-size: 13px; color: var(--muted); line-height: 1.6;
    }
    .hint strong { color: var(--text); font-weight: 500; }

    /* ─── HISTORY ────────────────────────────────── */
    #view-history { padding: 32px 24px; display: none; }
    .empty-state { text-align: center; padding: 72px 0; color: var(--muted); font-size: 13px; line-height: 1.8; }
    .list { display: flex; flex-direction: column; border: 1px solid var(--border); border-radius: var(--r); overflow: hidden; gap: 1px; background: var(--border); }
    .list-item { display: flex; align-items: center; gap: 14px; padding: 16px 18px; background: var(--surface); cursor: pointer; transition: background 0.12s; }
    .list-item:active { background: var(--bg); }
    .item-body { flex: 1; min-width: 0; }
    .item-title { font-size: 14px; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-bottom: 3px; }
    .item-meta { font-size: 11px; color: var(--muted); letter-spacing: 0.02em; }
    .item-chevron { color: var(--border); font-size: 18px; flex-shrink: 0; }

    /* ─── DETAIL ─────────────────────────────────── */
    #view-detail { padding: 32px 24px; display: none; }
    .back { display: inline-flex; align-items: center; gap: 4px; font-size: 13px; color: var(--muted); cursor: pointer; background: none; border: none; font-family: inherit; margin-bottom: 28px; padding: 0; }
    .detail-title { font-size: 21px; font-weight: 400; margin-bottom: 6px; }
    .detail-meta { font-size: 12px; color: var(--muted); margin-bottom: 28px; letter-spacing: 0.02em; }
    .detail-body { font-family: 'Georgia', 'Times New Roman', serif; font-size: 16px; line-height: 1.85; white-space: pre-wrap; word-break: break-word; margin-bottom: 36px; }
    .detail-actions { display: flex; gap: 8px; padding-bottom: calc(32px + var(--sb)); }
    .btn-del { color: var(--red) !important; border-color: currentColor !important; }

    /* ─── TOAST ──────────────────────────────────── */
    #toast {
      position: fixed; bottom: calc(28px + var(--sb)); left: 50%;
      transform: translateX(-50%) translateY(60px); background: var(--text); color: #FFF;
      padding: 9px 18px; border-radius: 100px; font-size: 13px; font-weight: 500;
      white-space: nowrap; pointer-events: none; z-index: 99;
      transition: transform 0.28s cubic-bezier(0.34, 1.4, 0.64, 1);
    }
    #toast.show { transform: translateX(-50%) translateY(0); }
  </style>
</head>
<body>

<!-- LOGIN -->
<div id="login-view">
  <div class="login-wordmark">Verbatim</div>
  <div class="login-field">
    <div class="field-label">Password</div>
    <input type="password" id="loginPass" placeholder="Enter password" autocomplete="current-password">
    <button class="btn-block" onclick="doLogin()">Continue</button>
    <div class="login-err" id="loginErr"></div>
  </div>
</div>

<!-- APP -->
<div id="app">
  <header>
    <div class="wordmark">Verbatim</div>
    <nav>
      <div class="tab active" id="tab-new" onclick="nav('new')">New</div>
      <div class="tab" id="tab-history" onclick="nav('history')">History</div>
    </nav>
  </header>

  <div class="scroll-area">

    <!-- New -->
    <div id="view-new">

      <div>
        <div class="label">Audio source</div>
        <div class="source-toggle">
          <button class="source-opt active" id="src-tab" onclick="setSrc('tab')">Tab / Screen</button>
          <button class="source-opt" id="src-mic" onclick="setSrc('mic')">Microphone</button>
        </div>
      </div>

      <div id="hint-tab" class="hint">
        <strong>How it works:</strong> click Start, then pick the browser tab playing the audio. <strong>Important:</strong> in Chrome's share dialog, make sure "Share tab audio" (or "Share audio") is ticked — it's off by default.
      </div>
      <div id="hint-mic" class="hint" style="display:none">
        <strong>Microphone mode:</strong> your device mic will be used. On a phone, hold it near the speaker playing the audio.
      </div>

      <div>
        <div class="label">Duration</div>
        <div class="dur-row">
          <input type="number" id="durInput" value="60" min="1" max="480">
          <span class="dur-label">minutes &nbsp;·&nbsp; 0 for no limit</span>
        </div>
      </div>

      <div class="controls">
        <button class="btn dark" id="startBtn" onclick="startSession()">Start</button>
        <button class="btn stop" id="stopBtn" onclick="stopSession()" style="display:none">Stop</button>
        <button class="btn" id="copyBtn" onclick="copyText()" style="display:none">Copy</button>
      </div>

      <div class="status">
        <div class="dot" id="dot"></div>
        <div class="status-msg" id="statusMsg">Ready</div>
        <div class="countdown" id="countdown"></div>
      </div>

      <div class="transcript-box" id="transcriptBox">
        <span class="placeholder">Transcript will appear here as audio is detected.</span>
      </div>

    </div>

    <!-- History -->
    <div id="view-history">
      <div class="label" style="margin-bottom:16px">Saved sessions</div>
      <div id="historyList"></div>
    </div>

    <!-- Detail -->
    <div id="view-detail">
      <button class="back" onclick="nav('history')">&#8592; History</button>
      <div class="detail-title" id="detailTitle"></div>
      <div class="detail-meta" id="detailMeta"></div>
      <div class="detail-body" id="detailBody"></div>
      <div class="detail-actions">
        <button class="btn dark" onclick="copyDetail()">Copy text</button>
        <button class="btn btn-del" onclick="deleteDetail()">Delete</button>
      </div>
    </div>

  </div>
</div>

<div id="toast"></div>

<script>
const TOKEN_KEY   = 'vb_token';
const HISTORY_KEY = 'vb_history';
let ws = null, audioCtx = null, mediaStream = null;
let buffer = '', activeId = null, toastTimer;
let timerInterval = null, currentSrc = 'tab';

/* ── Auth ──────────────────────────────────────── */
async function doLogin() {
  const pass = document.getElementById('loginPass').value;
  const errEl = document.getElementById('loginErr');
  errEl.textContent = '';
  try {
    const r = await fetch('/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({password: pass})
    });
    if (!r.ok) throw new Error();
    const {token} = await r.json();
    localStorage.setItem(TOKEN_KEY, token);
    boot();
  } catch {
    errEl.textContent = 'Incorrect password';
    const inp = document.getElementById('loginPass');
    inp.classList.remove('shake');
    void inp.offsetWidth;
    inp.classList.add('shake');
  }
}

document.getElementById('loginPass').addEventListener('keydown', e => {
  if (e.key === 'Enter') doLogin();
});

function boot() {
  document.getElementById('login-view').style.display = 'none';
  document.getElementById('app').style.display = 'flex';
  nav('new');
}

(function init() { if (localStorage.getItem(TOKEN_KEY)) boot(); })();

/* ── Navigation ────────────────────────────────── */
const VIEWS = ['new', 'history', 'detail'];
function nav(name) {
  VIEWS.forEach(v => {
    document.getElementById('view-' + v).style.display = 'none';
    const t = document.getElementById('tab-' + v);
    if (t) t.classList.remove('active');
  });
  document.getElementById('view-' + name).style.display = 'block';
  const t = document.getElementById('tab-' + name);
  if (t) t.classList.add('active');
  if (name === 'history') renderHistory();
}

/* ── Source toggle ─────────────────────────────── */
function setSrc(src) {
  currentSrc = src;
  document.getElementById('src-tab').classList.toggle('active', src === 'tab');
  document.getElementById('src-mic').classList.toggle('active', src === 'mic');
  document.getElementById('hint-tab').style.display = src === 'tab' ? 'block' : 'none';
  document.getElementById('hint-mic').style.display = src === 'mic' ? 'block' : 'none';
}

/* ── Timer ─────────────────────────────────────── */
function getDurMins() {
  const v = parseInt(document.getElementById('durInput').value, 10);
  return isNaN(v) ? 0 : v;
}

function startTimer() {
  clearInterval(timerInterval);
  const cdEl = document.getElementById('countdown');
  const mins = getDurMins();
  if (!mins) { cdEl.textContent = ''; return; }
  const end = Date.now() + mins * 60000;
  function tick() {
    const rem = end - Date.now();
    if (rem <= 0) { cdEl.textContent = '0:00'; stopSession(); return; }
    const m = Math.floor(rem / 60000);
    const s = Math.floor((rem % 60000) / 1000).toString().padStart(2, '0');
    cdEl.textContent = m + ':' + s;
    cdEl.classList.toggle('warn', rem < 60000);
  }
  tick();
  timerInterval = setInterval(tick, 1000);
}

function stopTimer() {
  clearInterval(timerInterval);
  timerInterval = null;
  const cdEl = document.getElementById('countdown');
  cdEl.textContent = '';
  cdEl.classList.remove('warn');
}

/* ── Transcription ─────────────────────────────── */
async function startSession() {
  setStatus('Requesting audio…', false);
  document.getElementById('startBtn').style.display = 'none';
  document.getElementById('copyBtn').style.display = 'none';

  let stream;
  try {
    if (currentSrc === 'tab') {
      stream = await navigator.mediaDevices.getDisplayMedia({audio: true, video: true});
      // Stop any video tracks — we only want audio
      stream.getVideoTracks().forEach(t => t.stop());
    } else {
      stream = await navigator.mediaDevices.getUserMedia({audio: true, video: false});
    }
  } catch (e) {
    setStatus('Audio access denied', false);
    document.getElementById('startBtn').style.display = 'block';
    return;
  }

  if (!stream.getAudioTracks().length) {
    setStatus('No audio track found — make sure to share a tab with audio', false);
    document.getElementById('startBtn').style.display = 'block';
    stream.getTracks().forEach(t => t.stop());
    return;
  }

  mediaStream = stream;
  buffer = '';
  renderBuffer();
  document.getElementById('stopBtn').style.display = 'block';
  setStatus('Connecting…', false);

  const token = localStorage.getItem(TOKEN_KEY);
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(proto + '//' + location.host + '/stream?token=' + encodeURIComponent(token));
  ws.binaryType = 'arraybuffer';

  ws.onopen = () => {
    // Set up AudioContext to capture PCM at 16 kHz
    audioCtx = new AudioContext({sampleRate: 16000});
    const src = audioCtx.createMediaStreamSource(stream);
    const proc = audioCtx.createScriptProcessor(4096, 1, 1);

    proc.onaudioprocess = e => {
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      const f32 = e.inputBuffer.getChannelData(0);
      const i16 = new Int16Array(f32.length);
      for (let i = 0; i < f32.length; i++) {
        i16[i] = Math.max(-32768, Math.min(32767, f32[i] * 32768));
      }
      ws.send(i16.buffer);
    };

    src.connect(proc);
    proc.connect(audioCtx.destination);
  };

  ws.onmessage = e => {
    const d = JSON.parse(e.data);
    if (d.type === 'status')     { setStatus(d.text, d.live || false); if (d.live) startTimer(); }
    else if (d.type === 'transcript') { buffer += d.text + ' '; renderBuffer(); }
    else if (d.type === 'error') { setStatus('Error: ' + d.text, false); endSession(); }
  };

  ws.onclose = () => endSession();

  // If the user stops sharing via the browser's built-in button
  stream.getAudioTracks()[0].addEventListener('ended', () => endSession());
}

function stopSession() { endSession(); }

function endSession() {
  if (ws) { ws.close(); ws = null; }
  if (audioCtx) { audioCtx.close(); audioCtx = null; }
  if (mediaStream) { mediaStream.getTracks().forEach(t => t.stop()); mediaStream = null; }
  stopTimer();
  document.getElementById('startBtn').style.display = 'block';
  document.getElementById('stopBtn').style.display = 'none';
  document.getElementById('dot').classList.remove('on');
  const text = buffer.trim();
  if (text) {
    document.getElementById('copyBtn').style.display = 'block';
    persist(text);
    toast('Saved to history');
  }
}

function renderBuffer() {
  const box = document.getElementById('transcriptBox');
  if (!buffer) {
    box.innerHTML = '<span class="placeholder">Transcript will appear here as audio is detected.</span>';
  } else {
    box.textContent = buffer;
    box.scrollTop = box.scrollHeight;
  }
}

function setStatus(text, live) {
  document.getElementById('statusMsg').textContent = text;
  document.getElementById('dot').classList.toggle('on', live);
}

function copyText() {
  navigator.clipboard.writeText(buffer.trim()).then(() => toast('Copied'));
}

/* ── Storage ───────────────────────────────────── */
function getHistory() {
  try { return JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]'); } catch { return []; }
}

function persist(text) {
  const h = getHistory();
  const words = text.split(/[\s]+/);
  const preview = words.slice(0, 7).join(' ') + (words.length > 7 ? '…' : '');
  h.unshift({ id: Date.now().toString(), title: preview, content: text, createdAt: new Date().toISOString(), words: words.length });
  localStorage.setItem(HISTORY_KEY, JSON.stringify(h.slice(0, 100)));
}

function renderHistory() {
  const h = getHistory();
  const el = document.getElementById('historyList');
  if (!h.length) {
    el.innerHTML = '<div class="empty-state">No sessions yet.<br>Start a new transcription to begin.</div>';
    return;
  }
  el.innerHTML = '<div class="list">' + h.map(t => `
    <div class="list-item" onclick="openDetail('${t.id}')">
      <div class="item-body">
        <div class="item-title">${esc(t.title)}</div>
        <div class="item-meta">${fmtDate(t.createdAt)}${t.words != null ? '&nbsp;&middot;&nbsp;' + t.words.toLocaleString() + ' words' : ''}</div>
      </div>
      <div class="item-chevron">&#8250;</div>
    </div>`).join('') + '</div>';
}

function openDetail(id) {
  const t = getHistory().find(x => x.id === id);
  if (!t) return;
  activeId = id;
  document.getElementById('detailTitle').textContent = t.title;
  document.getElementById('detailMeta').textContent = fmtDate(t.createdAt) + (t.words != null ? ' · ' + t.words.toLocaleString() + ' words' : '');
  document.getElementById('detailBody').textContent = t.content;
  nav('detail');
}

function copyDetail() {
  const t = getHistory().find(x => x.id === activeId);
  if (t) navigator.clipboard.writeText(t.content).then(() => toast('Copied'));
}

function deleteDetail() {
  const h = getHistory().filter(x => x.id !== activeId);
  localStorage.setItem(HISTORY_KEY, JSON.stringify(h));
  nav('history');
  toast('Deleted');
}

/* ── Helpers ───────────────────────────────────── */
function esc(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function fmtDate(iso) {
  return new Date(iso).toLocaleDateString('en-AU', {
    weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'
  });
}
function toast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 2400);
}

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}
</script>
</body>
</html>"""


@app.get("/")
async def index():
    return HTMLResponse(HTML)
