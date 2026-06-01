import asyncio
import json
import os
import secrets
import tempfile

import yt_dlp
from deepgram import DeepgramClient, LiveOptions, LiveTranscriptionEvents
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

DEEPGRAM_API_KEY = os.environ.get("DEEPGRAM_API_KEY", "")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "verbatim")

# Write YouTube cookies to a temp file once at startup if provided
_COOKIES_FILE: str | None = None
_yt_cookies = os.environ.get("YOUTUBE_COOKIES", "").strip()
if _yt_cookies:
    _tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
    _tmp.write(_yt_cookies)
    _tmp.close()
    _COOKIES_FILE = _tmp.name

app = FastAPI()
valid_tokens: set[str] = set()


class LoginRequest(BaseModel):
    password: str


@app.post("/login")
async def login(req: LoginRequest):
    if req.password != APP_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid password")
    token = secrets.token_hex(32)
    valid_tokens.add(token)
    return {"token": token}


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
    sw = """const CACHE = 'verbatim-v2';
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
  if (e.request.url.includes('/transcribe') ||
      e.request.url.includes('/login')) return;
  e.respondWith(caches.match(e.request).then(r => r || fetch(e.request)));
});"""
    return Response(sw, media_type="application/javascript")


@app.get("/health")
async def health():
    return {"status": "ok"}


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
      color: var(--text);
    }

    .login-field {
      width: 100%;
      max-width: 300px;
      display: flex;
      flex-direction: column;
      gap: 10px;
    }

    .field-label {
      font-size: 11px;
      font-weight: 500;
      letter-spacing: 0.1em;
      text-transform: uppercase;
      color: var(--muted);
    }

    input[type="password"], input[type="url"] {
      width: 100%;
      padding: 13px 14px;
      font-family: inherit;
      font-size: 15px;
      color: var(--text);
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--r);
      outline: none;
      transition: border-color 0.15s;
      -webkit-appearance: none;
    }
    input:focus { border-color: var(--text); }
    input::placeholder { color: var(--muted); }

    .btn-block {
      width: 100%;
      padding: 13px;
      font-family: inherit;
      font-size: 13px;
      font-weight: 500;
      letter-spacing: 0.05em;
      color: #FFF;
      background: var(--text);
      border: none;
      border-radius: var(--r);
      cursor: pointer;
      transition: opacity 0.15s;
      margin-top: 4px;
    }
    .btn-block:active { opacity: 0.72; }

    .login-err {
      font-size: 12px;
      color: var(--red);
      min-height: 16px;
      text-align: center;
    }

    @keyframes shake {
      0%,100% { transform: translateX(0) }
      20%      { transform: translateX(-7px) }
      40%      { transform: translateX(7px) }
      60%      { transform: translateX(-4px) }
      80%      { transform: translateX(4px) }
    }
    .shake { animation: shake 0.38s ease; }

    /* ─── APP SHELL ──────────────────────────────── */
    #app { display: none; flex-direction: column; height: 100dvh; }

    header {
      display: flex;
      align-items: flex-end;
      justify-content: space-between;
      padding: 18px 24px 0;
      border-bottom: 1px solid var(--border);
      background: var(--bg);
      position: sticky;
      top: 0;
      z-index: 20;
      flex-shrink: 0;
    }

    .wordmark {
      font-size: 11px;
      font-weight: 500;
      letter-spacing: 0.18em;
      text-transform: uppercase;
      padding-bottom: 14px;
    }

    nav { display: flex; }
    .tab {
      padding: 14px 0;
      margin-left: 28px;
      font-size: 13px;
      font-weight: 400;
      color: var(--muted);
      cursor: pointer;
      border-bottom: 1.5px solid transparent;
      transition: color 0.15s, border-color 0.15s;
      user-select: none;
    }
    .tab.active { color: var(--text); border-bottom-color: var(--text); }

    .scroll-area {
      flex: 1;
      overflow-y: auto;
      -webkit-overflow-scrolling: touch;
    }

    /* ─── NEW SESSION ────────────────────────────── */
    #view-new { padding: 32px 24px; display: flex; flex-direction: column; gap: 22px; }

    .label {
      font-size: 11px;
      font-weight: 500;
      letter-spacing: 0.1em;
      text-transform: uppercase;
      color: var(--muted);
      margin-bottom: 8px;
    }

    .controls { display: flex; gap: 8px; }

    .btn {
      flex: 1;
      padding: 12px 10px;
      font-family: inherit;
      font-size: 13px;
      font-weight: 500;
      letter-spacing: 0.04em;
      border: 1px solid var(--border);
      border-radius: var(--r);
      background: var(--surface);
      color: var(--text);
      cursor: pointer;
      transition: opacity 0.15s;
    }
    .btn:active { opacity: 0.65; }
    .btn.dark   { background: var(--text); color: #FFF; border-color: var(--text); }
    .btn.stop   { background: var(--red);  color: #FFF; border-color: var(--red); }
    .btn:disabled { opacity: 0.3; cursor: default; }

    .status {
      display: flex;
      align-items: center;
      gap: 8px;
      min-height: 18px;
    }
    .dot {
      width: 6px; height: 6px;
      border-radius: 50%;
      background: var(--green);
      flex-shrink: 0;
      opacity: 0;
      transition: opacity 0.2s;
    }
    @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.15} }
    .dot.on { opacity: 1; animation: pulse 1.8s infinite; }
    .status-msg { font-size: 12px; color: var(--muted); }

    .transcript-box {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--r);
      padding: 22px;
      min-height: 220px;
      max-height: 52dvh;
      overflow-y: auto;
      font-family: 'Georgia', 'Times New Roman', serif;
      font-size: 16px;
      line-height: 1.8;
      color: var(--text);
      word-break: break-word;
    }
    .placeholder { font-family: 'Inter', sans-serif; font-size: 13px; color: var(--muted); }

    /* ─── HISTORY ────────────────────────────────── */
    #view-history { padding: 32px 24px; display: none; }

    .empty-state {
      text-align: center;
      padding: 72px 0;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.8;
    }

    .list {
      display: flex;
      flex-direction: column;
      border: 1px solid var(--border);
      border-radius: var(--r);
      overflow: hidden;
      gap: 1px;
      background: var(--border);
    }

    .list-item {
      display: flex;
      align-items: center;
      gap: 14px;
      padding: 16px 18px;
      background: var(--surface);
      cursor: pointer;
      transition: background 0.12s;
    }
    .list-item:active { background: var(--bg); }

    .item-body { flex: 1; min-width: 0; }
    .item-title {
      font-size: 14px;
      font-weight: 500;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      margin-bottom: 3px;
    }
    .item-meta { font-size: 11px; color: var(--muted); letter-spacing: 0.02em; }
    .item-chevron { color: var(--border); font-size: 18px; flex-shrink: 0; }

    /* ─── TRANSCRIPT DETAIL ──────────────────────── */
    #view-detail { padding: 32px 24px; display: none; }

    .back {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      font-size: 13px;
      color: var(--muted);
      cursor: pointer;
      background: none;
      border: none;
      font-family: inherit;
      margin-bottom: 28px;
      padding: 0;
    }

    .detail-title { font-size: 21px; font-weight: 400; margin-bottom: 6px; }
    .detail-meta { font-size: 12px; color: var(--muted); margin-bottom: 28px; letter-spacing: 0.02em; }
    .detail-body {
      font-family: 'Georgia', 'Times New Roman', serif;
      font-size: 16px;
      line-height: 1.85;
      white-space: pre-wrap;
      word-break: break-word;
      margin-bottom: 36px;
    }
    .detail-actions { display: flex; gap: 8px; padding-bottom: calc(32px + var(--sb)); }
    .btn-del { color: var(--red) !important; border-color: currentColor !important; }

    /* ─── DURATION PICKER ───────────────────────── */
    .duration-row {
      display: flex;
      gap: 6px;
    }
    .dur {
      flex: 1;
      padding: 9px 4px;
      font-family: inherit;
      font-size: 12px;
      font-weight: 500;
      letter-spacing: 0.04em;
      border: 1px solid var(--border);
      border-radius: var(--r);
      background: var(--surface);
      color: var(--muted);
      cursor: pointer;
      transition: all 0.12s;
      text-align: center;
    }
    .dur.selected {
      background: var(--text);
      border-color: var(--text);
      color: #FFF;
    }
    .dur:active { opacity: 0.7; }

    .countdown {
      font-size: 12px;
      color: var(--muted);
      font-variant-numeric: tabular-nums;
      margin-left: auto;
      flex-shrink: 0;
    }
    .countdown.warn { color: var(--red); }

    /* ─── TOAST ──────────────────────────────────── */
    #toast {
      position: fixed;
      bottom: calc(28px + var(--sb));
      left: 50%;
      transform: translateX(-50%) translateY(60px);
      background: var(--text);
      color: #FFF;
      padding: 9px 18px;
      border-radius: 100px;
      font-size: 13px;
      font-weight: 500;
      white-space: nowrap;
      pointer-events: none;
      z-index: 99;
      transition: transform 0.28s cubic-bezier(0.34, 1.4, 0.64, 1);
    }
    #toast.show { transform: translateX(-50%) translateY(0); }
  </style>
</head>
<body>

<!-- ── LOGIN ── -->
<div id="login-view">
  <div class="login-wordmark">Verbatim</div>
  <div class="login-field">
    <div class="field-label">Password</div>
    <input type="password" id="loginPass" placeholder="Enter password" autocomplete="current-password">
    <button class="btn-block" onclick="doLogin()">Continue</button>
    <div class="login-err" id="loginErr"></div>
  </div>
</div>

<!-- ── APP ── -->
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
        <div class="label">Stream URL</div>
        <input type="url" id="streamUrl" placeholder="Paste YouTube or stream URL" autocomplete="off" autocorrect="off" autocapitalize="off">
      </div>

      <div>
        <div class="label">Duration</div>
        <div class="duration-row">
          <div class="dur selected" data-mins="30"  onclick="setDur(this)">30 m</div>
          <div class="dur"          data-mins="60"  onclick="setDur(this)">1 hr</div>
          <div class="dur"          data-mins="90"  onclick="setDur(this)">90 m</div>
          <div class="dur"          data-mins="120" onclick="setDur(this)">2 hr</div>
          <div class="dur"          data-mins="0"   onclick="setDur(this)">∞</div>
        </div>
      </div>

      <div class="controls">
        <button class="btn dark" id="startBtn" onclick="startSession()">Start</button>
        <button class="btn stop"  id="stopBtn"  onclick="stopSession()"  style="display:none">Stop</button>
        <button class="btn"       id="copyBtn"  onclick="copyText()"     style="display:none">Copy</button>
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
      <div class="detail-meta"  id="detailMeta"></div>
      <div class="detail-body"  id="detailBody"></div>
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
let es = null, buffer = '', activeId = null, toastTimer;
let durMins = 30, timerInterval = null, timerEnd = null;

/* ── Duration picker ───────────────────────────── */
function setDur(el) {
  document.querySelectorAll('.dur').forEach(d => d.classList.remove('selected'));
  el.classList.add('selected');
  durMins = parseInt(el.dataset.mins, 10);
}

function startTimer() {
  clearInterval(timerInterval);
  const cdEl = document.getElementById('countdown');
  if (!durMins) { cdEl.textContent = ''; return; }
  timerEnd = Date.now() + durMins * 60 * 1000;
  function tick() {
    const remaining = timerEnd - Date.now();
    if (remaining <= 0) {
      cdEl.textContent = '0:00';
      endSession();
      return;
    }
    const m = Math.floor(remaining / 60000);
    const s = Math.floor((remaining % 60000) / 1000).toString().padStart(2, '0');
    cdEl.textContent = m + ':' + s;
    cdEl.classList.toggle('warn', remaining < 60000);
  }
  tick();
  timerInterval = setInterval(tick, 1000);
}

function stopTimer() {
  clearInterval(timerInterval);
  timerInterval = null;
  timerEnd = null;
  const cdEl = document.getElementById('countdown');
  cdEl.textContent = '';
  cdEl.classList.remove('warn');
}

/* ── Auth ──────────────────────────────────────── */
async function doLogin() {
  const pass = document.getElementById('loginPass').value;
  const errEl = document.getElementById('loginErr');
  errEl.textContent = '';
  try {
    const r = await fetch('/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: pass })
    });
    if (!r.ok) throw new Error();
    const { token } = await r.json();
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
  const a = document.getElementById('app');
  a.style.display = 'flex';
  nav('new');
}

(function init() {
  if (localStorage.getItem(TOKEN_KEY)) boot();
})();

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

/* ── Transcription ─────────────────────────────── */
function startSession() {
  const url = document.getElementById('streamUrl').value.trim();
  if (!url) { toast('Paste a stream URL first'); return; }

  buffer = '';
  renderBuffer();
  document.getElementById('startBtn').style.display = 'none';
  document.getElementById('stopBtn').style.display  = 'block';
  document.getElementById('copyBtn').style.display  = 'none';
  setStatus('Connecting…', false);

  const token = localStorage.getItem(TOKEN_KEY);
  es = new EventSource('/transcribe?url=' + encodeURIComponent(url) + '&token=' + encodeURIComponent(token));

  es.onmessage = ({ data }) => {
    const d = JSON.parse(data);
    if      (d.type === 'status')     { setStatus(d.text, d.live || false); if (d.live) startTimer(); }
    else if (d.type === 'transcript') { buffer += d.text + ' '; renderBuffer(); }
    else if (d.type === 'error')      { setStatus('Error: ' + d.text, false); endSession(); }
    else if (d.type === 'done')       { setStatus('Complete', false); endSession(); }
  };
  es.onerror = () => { setStatus('Connection lost', false); endSession(); };
}

function stopSession() { endSession(); }

function endSession() {
  if (es) { es.close(); es = null; }
  stopTimer();
  document.getElementById('startBtn').style.display = 'block';
  document.getElementById('stopBtn').style.display  = 'none';
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
  const entry = {
    id:        Date.now().toString(),
    title:     preview,
    content:   text,
    createdAt: new Date().toISOString(),
    words:     words.length
  };
  h.unshift(entry);
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
        <div class="item-meta">${fmtDate(t.createdAt)}&nbsp;&middot;&nbsp;${t.words.toLocaleString()} words</div>
      </div>
      <div class="item-chevron">›</div>
    </div>`).join('') + '</div>';
}

function openDetail(id) {
  const t = getHistory().find(x => x.id === id);
  if (!t) return;
  activeId = id;
  document.getElementById('detailTitle').textContent = t.title;
  document.getElementById('detailMeta').textContent  = fmtDate(t.createdAt) + ' · ' + t.words.toLocaleString() + ' words';
  document.getElementById('detailBody').textContent  = t.content;
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
function esc(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function fmtDate(iso) {
  return new Date(iso).toLocaleDateString('en-AU', {
    weekday: 'short', day: 'numeric', month: 'short',
    hour: '2-digit', minute: '2-digit'
  });
}

function toast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 2400);
}

/* ── PWA ───────────────────────────────────────── */
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}
</script>
</body>
</html>"""


@app.get("/")
async def index():
    return HTMLResponse(HTML)


@app.get("/transcribe")
async def transcribe(url: str, request: Request, token: str = ""):
    if token not in valid_tokens:
        async def unauth():
            yield {"data": json.dumps({"type": "error", "text": "Unauthorized — please log in again"})}
        return EventSourceResponse(unauth())

    async def generate():
        queue: asyncio.Queue = asyncio.Queue()
        ffmpeg_proc = None
        dg_connection = None

        try:
            yield {"data": json.dumps({"type": "status", "text": "Extracting stream audio…"})}

            loop = asyncio.get_event_loop()

            def get_audio_url():
                opts = {"format": "bestaudio/best", "quiet": True, "no_warnings": True}
                if _COOKIES_FILE:
                    opts["cookiefile"] = _COOKIES_FILE
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    if "entries" in info:
                        info = info["entries"][0]
                    return info.get("url") or info.get("manifest_url")

            audio_url = await loop.run_in_executor(None, get_audio_url)

            if not audio_url:
                yield {"data": json.dumps({"type": "error", "text": "Could not extract audio from URL"})}
                return

            yield {"data": json.dumps({"type": "status", "text": "Connecting to Deepgram…"})}

            deepgram = DeepgramClient(DEEPGRAM_API_KEY)
            dg_connection = deepgram.listen.asyncwebsocket.v("1")

            async def on_transcript(self, result, **kwargs):
                try:
                    text = result.channel.alternatives[0].transcript
                    if text and result.is_final:
                        await queue.put(("transcript", text))
                except Exception:
                    pass

            async def on_error(self, error, **kwargs):
                await queue.put(("error", str(error)))

            async def on_close(self, close, **kwargs):
                await queue.put(("done", None))

            dg_connection.on(LiveTranscriptionEvents.Transcript, on_transcript)
            dg_connection.on(LiveTranscriptionEvents.Error, on_error)
            dg_connection.on(LiveTranscriptionEvents.Close, on_close)

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

            if not await dg_connection.start(options):
                yield {"data": json.dumps({"type": "error", "text": "Failed to connect to Deepgram"})}
                return

            yield {"data": json.dumps({"type": "status", "text": "Live — transcribing", "live": True})}

            ffmpeg_proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-reconnect", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "5",
                "-i", audio_url,
                "-vn",
                "-ar", "16000",
                "-ac", "1",
                "-f", "s16le",
                "pipe:1",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )

            async def feed():
                try:
                    while True:
                        chunk = await ffmpeg_proc.stdout.read(8192)
                        if not chunk:
                            break
                        await dg_connection.send(chunk)
                        if await request.is_disconnected():
                            break
                except Exception:
                    pass
                finally:
                    await queue.put(("done", None))

            asyncio.create_task(feed())

            while True:
                if await request.is_disconnected():
                    break
                try:
                    kind, text = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                if kind == "transcript":
                    yield {"data": json.dumps({"type": "transcript", "text": text})}
                elif kind == "done":
                    yield {"data": json.dumps({"type": "done"})}
                    break
                elif kind == "error":
                    yield {"data": json.dumps({"type": "error", "text": text})}
                    break

        except Exception as e:
            yield {"data": json.dumps({"type": "error", "text": str(e)})}
        finally:
            if ffmpeg_proc:
                try:
                    ffmpeg_proc.kill()
                    await ffmpeg_proc.wait()
                except Exception:
                    pass
            if dg_connection:
                try:
                    await dg_connection.finish()
                except Exception:
                    pass

    return EventSourceResponse(generate())
