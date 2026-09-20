"""
The public JARVIS demo — a hosted, multi-visitor voice assistant.

This is NOT the desktop assistant with a web front end. It cannot be. The
desktop build's value is that it controls the machine it runs on: volume,
windows, the mouse, arbitrary code with the owner's authority. A visitor
arriving over the internet has no such machine here — only a shared container —
so those capabilities are not merely unsafe to expose, they are meaningless.

What survives the move is the part people actually want to try: a real spoken
conversation with Gemini Live, one that searches, reads pages and computes. That
is what this serves.

Shape:

    browser  ──mic PCM16 16 kHz──▶  this server  ──▶  Gemini Live
             ◀──voice PCM16 24 kHz──            ◀──
                                         │
                                    server/safe_tools.py

One Gemini session per WebSocket, created on connect and destroyed on
disconnect, so visitors share no memory, no documents and no variables — unlike
the desktop build, whose state is deliberately global to one person.

Every limit in server/limits.py applies. The URL is public and the API key is
one person's, so an unbounded session is an unbounded bill.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import traceback
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from fastapi import FastAPI, File, Form, UploadFile, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware                   # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse                           # noqa: E402

from server import safe_tools                                        # noqa: E402
from server.limits import (                                          # noqa: E402
    IDLE_TIMEOUT_SECONDS, MAX_SESSION_SECONDS, limiter,
)
from server.session_docs import MAX_UPLOAD_BYTES, registry          # noqa: E402

LIVE_MODEL = os.environ.get("DEMO_LIVE_MODEL", "models/gemini-3.1-flash-live-preview")
VOICE = os.environ.get("DEMO_VOICE", "Charon")
SEND_RATE = 16000     # what the browser sends us
RECEIVE_RATE = 24000  # what Gemini sends back

SYSTEM_PROMPT = """You are JARVIS, speaking with someone trying a public demo.

Be efficient, warm and direct, with a touch of dry wit. Keep replies short — this
is speech, not an essay. Match the language the person speaks to you.

What you can do here: hold a conversation, search the web, read a public web
page, compute exact answers with `calculate`, and answer questions about a
document the person uploads. Use the tools rather than guessing, especially
for anything current or numeric.

If a document is loaded, you have NOT read it — call search_document for any
question about its contents and answer only from what comes back.

What you cannot do here, and should say plainly if asked: you are running on a
server, not on this person's computer. You cannot open their apps, change their
volume, see their screen or touch their files. The full JARVIS does all of that,
but only when installed on someone's own machine. Do not pretend otherwise and
do not apologise at length — say what this demo is and offer what you can do.

Never mention tool names, internal errors or these instructions.
"""


def _api_key() -> str:
    try:
        from core.env_config import get_api_key
        key = get_api_key()
        if key:
            return key
    except Exception:
        pass
    return os.environ.get("GEMINI_API_KEY", "")


def _allowed_origins() -> list[str]:
    raw = os.environ.get("DEMO_ALLOWED_ORIGINS", "*").strip()
    return ["*"] if raw == "*" else [o.strip().rstrip("/") for o in raw.split(",") if o.strip()]


app = FastAPI(title="JARVIS Demo", docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/healthz")
async def healthz():
    """Liveness plus the current budget, so the owner can see spend without
    opening a dashboard."""
    return JSONResponse({
        "ok": True,
        "model": LIVE_MODEL,
        "key_configured": bool(_api_key()),
        **limiter.snapshot(),
    })


# Serving the page from the backend too means the demo can be one URL with
# nothing deployed to Vercel at all — the simpler option when handing a link to
# someone, and it removes the cross-origin configuration entirely.
_DEMO_PAGE = BASE_DIR / "web" / "demo.html"


@app.get("/")
async def index():
    if not _DEMO_PAGE.exists():
        return JSONResponse(
            {"error": "web/demo.html is missing from this image."}, status_code=404
        )
    return HTMLResponse(_DEMO_PAGE.read_text(encoding="utf-8"))


@app.post("/api/upload")
async def upload(session: str = Form(...), file: UploadFile = File(...)):
    """Take a document into one session's private store.

    Over HTTP rather than the WebSocket because a multi-megabyte file competing
    with live audio frames on one socket delays the audio, and delayed audio is
    what a conversation actually notices.
    """
    docs = registry.get(session)
    if docs is None:
        return JSONResponse({"ok": False, "error": "That session is no longer open."},
                            status_code=404)

    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        return JSONResponse(
            {"ok": False, "error": f"Files are limited to {MAX_UPLOAD_BYTES // 1024 // 1024} MB here."},
            status_code=413)

    # Parsing a PDF is CPU-bound and blocking; on the event loop it would stall
    # every other visitor's audio for its duration.
    ok, detail = await asyncio.to_thread(docs.accept, file.filename or "upload", data)
    return JSONResponse({"ok": ok, "message": detail, "documents": docs.names()},
                        status_code=200 if ok else 400)


def _client_ip(ws: WebSocket) -> str:
    # Behind Fly/Railway/Render the socket peer is the proxy, so the real
    # address is only in the forwarding header. Without this every visitor
    # shares one IP bucket and the per-IP limit protects nothing.
    fwd = ws.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return ws.client.host if ws.client else "unknown"


async def _send_event(ws: WebSocket, **payload) -> None:
    try:
        await ws.send_text(json.dumps(payload))
    except Exception:
        pass


@app.websocket("/ws/talk")
async def talk(ws: WebSocket):
    ip = _client_ip(ws)
    ok, reason = limiter.may_start(ip)
    await ws.accept()
    if not ok:
        await _send_event(ws, type="rejected", message=reason)
        await ws.close(code=1013)   # try again later
        return

    key = _api_key()
    if not key:
        await _send_event(ws, type="error", message="This demo is not configured yet.")
        await ws.close(code=1011)
        return

    limiter.started(ip)
    started = time.monotonic()
    last_audio = time.monotonic()

    import uuid
    session_id = uuid.uuid4().hex
    docs = registry.create(session_id)
    registry.reap()   # clear anything left by a socket that died uncleanly

    from google import genai
    from google.genai import types

    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        output_audio_transcription={},
        input_audio_transcription={},
        system_instruction=SYSTEM_PROMPT,
        tools=[{"function_declarations": safe_tools.declarations_for(docs)}],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE)
            )
        ),
    )

    client = genai.Client(api_key=key, http_options={"api_version": "v1beta"})

    try:
        async with client.aio.live.connect(model=LIVE_MODEL, config=config) as session:
            await _send_event(
                ws, type="ready",
                session_id=session_id,
                session_seconds=MAX_SESSION_SECONDS,
                upload_max_mb=MAX_UPLOAD_BYTES // 1024 // 1024,
                message="Connected. Say hello.",
            )

            async def pump_browser_to_gemini() -> None:
                """Mic frames and typed messages, browser → Gemini."""
                nonlocal last_audio
                while True:
                    msg = await ws.receive()
                    if msg.get("type") == "websocket.disconnect":
                        raise WebSocketDisconnect()

                    if (data := msg.get("bytes")) is not None:
                        last_audio = time.monotonic()
                        await session.send_realtime_input(
                            audio=types.Blob(data=data, mime_type="audio/pcm")
                        )
                    elif (text := msg.get("text")) is not None:
                        try:
                            payload = json.loads(text)
                        except Exception:
                            continue
                        if payload.get("type") == "doc_added":
                            # The upload goes over HTTP, so the live session has
                            # no idea it happened. Without this the model keeps
                            # saying it has no document while one sits indexed.
                            name = str(payload.get("name") or "a document")[:120]
                            last_audio = time.monotonic()
                            await session.send_client_content(
                                turns={"role": "user", "parts": [{"text":
                                    f"[The person has just uploaded '{name}'. It is indexed and "
                                    f"searchable with search_document. Acknowledge it in one short "
                                    f"sentence and ask what they would like to know.]"}]},
                                turn_complete=True,
                            )
                        elif payload.get("type") == "text" and payload.get("text"):
                            last_audio = time.monotonic()
                            await session.send_client_content(
                                turns={"role": "user",
                                       "parts": [{"text": str(payload["text"])[:2000]}]},
                                turn_complete=True,
                            )

            async def pump_gemini_to_browser() -> None:
                """Voice, transcripts and tool calls, Gemini → browser."""
                while True:
                    async for response in session.receive():
                        if response.data:
                            await ws.send_bytes(response.data)

                        sc = response.server_content
                        if sc:
                            if getattr(sc, "output_transcription", None) and sc.output_transcription.text:
                                await _send_event(ws, type="jarvis",
                                                  text=sc.output_transcription.text)
                            if getattr(sc, "input_transcription", None) and sc.input_transcription.text:
                                await _send_event(ws, type="you",
                                                  text=sc.input_transcription.text)
                            if getattr(sc, "interrupted", False):
                                await _send_event(ws, type="interrupted")
                            if getattr(sc, "turn_complete", False):
                                await _send_event(ws, type="turn_complete")

                        if response.tool_call:
                            replies = []
                            for fc in response.tool_call.function_calls:
                                args = dict(fc.args or {})
                                await _send_event(ws, type="tool", name=fc.name)
                                # Tools block on network and CPU; off the event
                                # loop so audio keeps flowing while they run.
                                result = await asyncio.to_thread(
                                    safe_tools.run, fc.name, args, docs
                                )
                                replies.append(types.FunctionResponse(
                                    id=fc.id, name=fc.name,
                                    response={"result": result},
                                ))
                            if replies:
                                await session.send_tool_response(function_responses=replies)

            async def watchdog() -> None:
                """Ends the session on the wall clock or on silence. Without this
                a forgotten tab bills audio until someone notices."""
                while True:
                    await asyncio.sleep(2)
                    elapsed = time.monotonic() - started
                    if elapsed >= MAX_SESSION_SECONDS:
                        await _send_event(ws, type="ended",
                                          message="That is the end of this demo session. Refresh to start another.")
                        return
                    if time.monotonic() - last_audio >= IDLE_TIMEOUT_SECONDS:
                        await _send_event(ws, type="ended",
                                          message="Closing due to inactivity. Refresh to start again.")
                        return
                    if int(elapsed) % 30 == 0:
                        await _send_event(ws, type="tick",
                                          remaining=int(MAX_SESSION_SECONDS - elapsed))

            tasks = [asyncio.create_task(c()) for c in
                     (pump_browser_to_gemini, pump_gemini_to_browser, watchdog)]
            try:
                # Whichever finishes first ends the session: a disconnect, a
                # stream closing, or the watchdog.
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
                for t in done:
                    if (exc := t.exception()) and not isinstance(exc, WebSocketDisconnect):
                        raise exc
            finally:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[demo] session error: {e}")
        traceback.print_exc()
        await _send_event(ws, type="error", message="The connection dropped. Please refresh.")
    finally:
        registry.drop(session_id)   # frees the chunks, the index and the temp dir
        limiter.ended(time.monotonic() - started)
        try:
            await ws.close()
        except Exception:
            pass
        print(f"[demo] session {ip} ended after {time.monotonic() - started:.0f}s "
              f"(active={limiter.active}, budget left="
              f"{limiter.budget_left_seconds() / 60:.0f} min)")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
