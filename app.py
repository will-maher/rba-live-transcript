import asyncio
import json
import os
import subprocess

import yt_dlp
from deepgram import DeepgramClient, LiveOptions, LiveTranscriptionEvents
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from sse_starlette.sse import EventSourceResponse

DEEPGRAM_API_KEY = os.environ.get("DEEPGRAM_API_KEY", "")

app = FastAPI()

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
    <title>RBA Live Transcript</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, sans-serif;
            background: #f2f2f7;
            padding: 16px;
            max-width: 680px;
            margin: 0 auto;
        }
        h1 { font-size: 22px; font-weight: 700; margin: 16px 0 20px; color: #1c1c1e; }
        input[type="url"] {
            width: 100%;
            padding: 14px 12px;
            font-size: 16px;
            border: 1px solid #c7c7cc;
            border-radius: 10px;
            background: white;
            margin-bottom: 10px;
            outline: none;
            -webkit-appearance: none;
        }
        input[type="url"]:focus { border-color: #007aff; }
        .row { display: flex; gap: 10px; margin-bottom: 14px; }
        button {
            flex: 1; padding: 14px;
            font-size: 16px; font-weight: 600;
            border: none; border-radius: 10px;
            cursor: pointer; -webkit-tap-highlight-color: transparent;
        }
        #startBtn { background: #007aff; color: white; }
        #startBtn:active { background: #0062cc; }
        #stopBtn { background: #ff3b30; color: white; display: none; }
        #stopBtn:active { background: #c0392b; }
        #copyBtn { background: #34c759; color: white; display: none; }
        #status {
            font-size: 13px; color: #6e6e73;
            margin-bottom: 10px;
            display: flex; align-items: center; gap: 6px;
        }
        .dot {
            width: 8px; height: 8px; border-radius: 50%;
            background: #34c759; display: none;
            animation: pulse 1.5s infinite;
        }
        @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }
        #transcript {
            background: white;
            border-radius: 12px;
            padding: 16px;
            min-height: 200px;
            max-height: 65vh;
            overflow-y: auto;
            font-size: 16px;
            line-height: 1.65;
            color: #1c1c1e;
            border: 1px solid #e5e5ea;
            word-wrap: break-word;
        }
        .empty { color: #aeaeb2; font-style: italic; }
    </style>
</head>
<body>
    <h1>RBA Live Transcript</h1>
    <input type="url" id="streamUrl" placeholder="Paste YouTube or stream URL..." autocomplete="off" />
    <div class="row">
        <button id="startBtn" onclick="start()">Start</button>
        <button id="stopBtn" onclick="stop()">Stop</button>
        <button id="copyBtn" onclick="copyText()">Copy</button>
    </div>
    <div id="status"><span class="dot" id="dot"></span><span id="statusText">Ready</span></div>
    <div id="transcript"><span class="empty">Transcript will appear here...</span></div>

    <script>
        let es = null;
        let fullText = '';

        function start() {
            const url = document.getElementById('streamUrl').value.trim();
            if (!url) { alert('Please paste a stream URL'); return; }

            fullText = '';
            document.getElementById('transcript').innerHTML = '';
            document.getElementById('startBtn').style.display = 'none';
            document.getElementById('stopBtn').style.display = 'block';
            document.getElementById('copyBtn').style.display = 'none';
            setStatus('Connecting...', false);

            es = new EventSource('/transcribe?url=' + encodeURIComponent(url));

            es.onmessage = function(e) {
                const data = JSON.parse(e.data);
                if (data.type === 'status') {
                    setStatus(data.text, data.live || false);
                } else if (data.type === 'transcript') {
                    appendText(data.text);
                } else if (data.type === 'error') {
                    setStatus('Error: ' + data.text, false);
                    stop();
                } else if (data.type === 'done') {
                    setStatus('Finished', false);
                    stop();
                }
            };

            es.onerror = function() {
                setStatus('Connection lost', false);
                stop();
            };
        }

        function stop() {
            if (es) { es.close(); es = null; }
            document.getElementById('startBtn').style.display = 'block';
            document.getElementById('stopBtn').style.display = 'none';
            if (fullText) document.getElementById('copyBtn').style.display = 'block';
            document.getElementById('dot').style.display = 'none';
        }

        function appendText(text) {
            fullText += text + ' ';
            const box = document.getElementById('transcript');
            box.textContent = fullText;
            box.scrollTop = box.scrollHeight;
        }

        function setStatus(text, live) {
            document.getElementById('statusText').textContent = text;
            document.getElementById('dot').style.display = live ? 'block' : 'none';
        }

        function copyText() {
            navigator.clipboard.writeText(fullText).then(() => {
                const btn = document.getElementById('copyBtn');
                btn.textContent = 'Copied!';
                setTimeout(() => btn.textContent = 'Copy', 1500);
            });
        }
    </script>
</body>
</html>"""


@app.get("/")
async def index():
    return HTMLResponse(HTML)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/transcribe")
async def transcribe(url: str, request: Request):
    async def generate():
        queue: asyncio.Queue = asyncio.Queue()
        ffmpeg_proc = None
        dg_connection = None

        try:
            yield {"data": json.dumps({"type": "status", "text": "Extracting stream audio..."})}

            loop = asyncio.get_event_loop()

            def extract_audio_url():
                opts = {"format": "bestaudio/best", "quiet": True, "no_warnings": True}
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    if "entries" in info:
                        info = info["entries"][0]
                    return info.get("url") or info.get("manifest_url")

            audio_url = await loop.run_in_executor(None, extract_audio_url)

            if not audio_url:
                yield {"data": json.dumps({"type": "error", "text": "Could not extract audio from URL"})}
                return

            yield {"data": json.dumps({"type": "status", "text": "Connecting to Deepgram..."})}

            deepgram = DeepgramClient(DEEPGRAM_API_KEY)
            dg_connection = deepgram.listen.asyncwebsocket.v("1")

            async def on_transcript(self, result, **kwargs):
                try:
                    alt = result.channel.alternatives[0]
                    text = alt.transcript
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

            started = await dg_connection.start(options)
            if not started:
                yield {"data": json.dumps({"type": "error", "text": "Failed to start Deepgram connection"})}
                return

            yield {"data": json.dumps({"type": "status", "text": "Live — transcribing...", "live": True})}

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

            async def feed_audio():
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

            asyncio.create_task(feed_audio())

            while True:
                if await request.is_disconnected():
                    break
                try:
                    kind, text = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                if kind == "done":
                    yield {"data": json.dumps({"type": "done"})}
                    break
                elif kind == "transcript":
                    yield {"data": json.dumps({"type": "transcript", "text": text})}
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
