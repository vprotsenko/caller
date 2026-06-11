"""FastAPI entry point — stage 1 (PoC, Plan.md §11).

Routes (all behind HTTP Basic Auth, same scheme as v1):
  GET  /              -> static/index.html (PoC page: preview + test call)
  POST /preview       -> synthesize the message, return an audio URL
  POST /call          -> originate ONE number, play the message, report outcome
  GET  /status        -> ESL/FreeSWITCH health (grows into the campaign
                         snapshot at stage 2)
  GET  /audio/{name}  -> serve a synthesized WAV

WEB_PASSWORD comes from the environment; if unset, a random one is generated
and logged so the app is never left open. NOTE: without TLS in front, Basic
Auth credentials travel in cleartext (POC trade-off, Plan.md §12).

The ESL password is read from the environment, used on the socket and never
logged or returned to the client.
"""

import asyncio
import logging
import os
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Form, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import esl, jobs, tts

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("caller")

AUDIO_DIR = jobs.AUDIO_DIR
os.makedirs(AUDIO_DIR, exist_ok=True)
INDEX_PATH = os.path.join(os.path.dirname(__file__), "static", "index.html")

WEB_USER = os.environ.get("WEB_USER", "admin")
WEB_PASSWORD = os.environ.get("WEB_PASSWORD") or secrets.token_urlsafe(12)
if not os.environ.get("WEB_PASSWORD"):
    logger.warning("WEB_PASSWORD not set — generated one for this run: %s", WEB_PASSWORD)

ESL_HOST = os.environ.get("ESL_HOST", "127.0.0.1")
ESL_PORT = int(os.environ.get("ESL_PORT", "8021"))
ESL_PASSWORD = os.environ.get("ESL_PASSWORD", "")

_security = HTTPBasic()


def require_auth(credentials: HTTPBasicCredentials = Depends(_security)):
    ok_user = secrets.compare_digest(credentials.username, WEB_USER)
    ok_pass = secrets.compare_digest(credentials.password, WEB_PASSWORD)
    if not (ok_user and ok_pass):
        raise HTTPException(status_code=401, detail="Unauthorized",
                            headers={"WWW-Authenticate": "Basic"})


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    client = getattr(app.state, "esl_client", None)
    if client:
        await client.close()


app = FastAPI(dependencies=[Depends(require_auth)], lifespan=lifespan)

_esl_lock = asyncio.Lock()
_call_lock = asyncio.Lock()  # one PoC call at a time (mirrors v1's one campaign)


async def get_esl():
    """Shared inbound ESL connection, (re)established on demand.

    FreeSWITCH may boot slower than this container, so the connection is lazy
    instead of a hard startup dependency.
    """
    async with _esl_lock:
        client = getattr(app.state, "esl_client", None)
        if client and client.connected:
            return client
        client = esl.InboundClient(ESL_HOST, ESL_PORT, ESL_PASSWORD)
        await client.connect()
        app.state.esl_client = client
        return client


@app.get("/")
def index():
    return FileResponse(INDEX_PATH, media_type="text/html")


@app.post("/preview")
def preview(
    text: str = Form(...),
    voice: str = Form(tts.DEFAULT_VOICE),
    speed: float = Form(1.05),
    steps: int = Form(8),
):
    text = text.strip()
    if not text:
        return JSONResponse({"error": "Порожній текст"}, status_code=400)
    if voice not in tts.VOICES:
        return JSONResponse({"error": f"Невідомий голос {voice}"}, status_code=400)
    speed, steps, silence = tts.clamp(speed, steps, 0.3)

    out = os.path.join(AUDIO_DIR, f"preview_{voice}.wav")
    tts.synthesize_native(text, voice, out, speed=speed, steps=steps, silence=silence)
    return {"voice": voice, "url": f"/audio/preview_{voice}.wav", "secs": tts.wav_seconds(out)}


@app.post("/call")
async def call(
    number: str = Form(...),
    text: str = Form(...),
    voice: str = Form(tts.DEFAULT_VOICE),
    speed: float = Form(1.05),
    steps: int = Form(8),
):
    text = text.strip()
    if not text:
        return JSONResponse({"error": "Порожній текст"}, status_code=400)
    if voice not in tts.VOICES:
        return JSONResponse({"error": f"Невідомий голос {voice}"}, status_code=400)
    num = jobs.normalize_number(number)
    if num is None:
        return JSONResponse({"error": f"Некоректний номер {number}"}, status_code=400)
    if _call_lock.locked():
        return JSONResponse({"error": "Дзвінок уже виконується"}, status_code=409)

    async with _call_lock:
        speed, steps, silence = tts.clamp(speed, steps, 0.3)
        native = os.path.join(AUDIO_DIR, "call_native.wav")
        tel = os.path.join(AUDIO_DIR, "call.wav")
        try:
            await asyncio.to_thread(
                tts.synthesize_telephony, text, voice, native, tel,
                speed=speed, steps=steps, silence=silence)
        except Exception:
            logger.exception("synthesis failed")
            return JSONResponse({"error": "Помилка синтезу"}, status_code=500)

        try:
            client = await get_esl()
        except Exception:
            logger.exception("ESL connection failed")
            return JSONResponse(
                {"error": "FreeSWITCH недоступний (ESL)"}, status_code=502)

        result = await jobs.call_once(client, num, tel)
    return {"ok": True, "number": num, **result}


@app.get("/status")
async def status():
    """PoC health snapshot; becomes the campaign snapshot at stage 2."""
    out = {"phase": "idle", "esl_connected": False}
    try:
        client = await get_esl()
        st = await client.api("status")
        out["esl_connected"] = True
        out["freeswitch"] = st.strip().splitlines()[0] if st.strip() else ""
    except Exception as exc:  # noqa: BLE001 — health endpoint must not 500
        out["error"] = f"ESL: {exc.__class__.__name__}"
    return out


@app.get("/audio/{name}")
def audio(name: str):
    path = os.path.join(AUDIO_DIR, os.path.basename(name))
    if not os.path.isfile(path):
        return JSONResponse({"error": "not found"}, status_code=404)
    # no-store so an overwritten file is never served stale
    return FileResponse(path, media_type="audio/wav", headers={"Cache-Control": "no-store"})
