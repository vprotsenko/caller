"""FastAPI entry point — stages 1–2 (Plan.md §8, §15).

Routes (all behind HTTP Basic Auth, same scheme as v1):
  GET  /              -> static/index.html
  POST /preview       -> synthesize the message, return an audio URL
  POST /call          -> stage-1 PoC: one ad-hoc call playing the message
  POST /start         -> start a campaign (JSON body, §15); 409 if one runs
  GET  /status        -> active-or-latest campaign snapshot (§15)
  GET  /audio/{name}  -> serve a synthesized WAV

SIP profiles:
  GET /config, POST /config/profiles, POST /config/profiles/{id},
  DELETE /config/profiles/{id}      (passwords: write-only, only password_set
                                     ever goes back — Plan.md §4)
History:
  GET /campaigns, GET /campaigns/{id},
  POST /campaigns/{id}/retry-failed (never touches optout),
  POST /campaigns/{id}/resume       (interrupted -> running)

WEB_PASSWORD comes from the environment; if unset, a random one is generated
and logged so the app is never left open. NOTE: without TLS in front, Basic
Auth credentials travel in cleartext (POC trade-off, Plan.md §12).
"""

import asyncio
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager

from fastapi import Body, Depends, FastAPI, Form, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import (db, esl, flow as flow_mod, gateways, ivr, jobs,
               operators as operators_mod, tts)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("caller")

AUDIO_DIR = jobs.AUDIO_DIR
os.makedirs(AUDIO_DIR, exist_ok=True)
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
INDEX_PATH = os.path.join(STATIC_DIR, "index.html")

WEB_USER = os.environ.get("WEB_USER", "admin")
WEB_PASSWORD = os.environ.get("WEB_PASSWORD") or secrets.token_urlsafe(12)
if not os.environ.get("WEB_PASSWORD"):
    logger.warning("WEB_PASSWORD not set — generated one for this run: %s", WEB_PASSWORD)

# On a fresh DB, seed one SIP profile from the optional .env defaults (SIP_*
# matter only here; afterwards profiles live in the DB, as in v1).
_ENV_SEED = {
    "name": "default",
    "server": os.environ.get("SIP_SERVER", ""),
    "port": os.environ.get("SIP_PORT", "5060"),
    "username": os.environ.get("SIP_USER", ""),
    "password": os.environ.get("SIP_PASSWORD", ""),
}

_security = HTTPBasic()


def require_auth(credentials: HTTPBasicCredentials = Depends(_security)):
    ok_user = secrets.compare_digest(credentials.username, WEB_USER)
    ok_pass = secrets.compare_digest(credentials.password, WEB_PASSWORD)
    if not (ok_user and ok_pass):
        raise HTTPException(status_code=401, detail="Unauthorized",
                            headers={"WWW-Authenticate": "Basic"})


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init(seed_profile=_ENV_SEED)
    db.mark_interrupted_on_startup()  # crash mid-campaign -> resumable, not running
    app.state.ivr_server = await ivr.start_server(jobs.IVR_HOST, jobs.IVR_PORT)
    yield
    app.state.ivr_server.close()
    await app.state.ivr_server.wait_closed()
    await esl.close_shared()


app = FastAPI(dependencies=[Depends(require_auth)], lifespan=lifespan)

_call_lock = asyncio.Lock()  # one ad-hoc PoC call at a time

# Static assets are served by an explicit route (NOT app.mount): a mounted
# sub-app would bypass the global require_auth dependency.
_STATIC_TYPES = {".js": "application/javascript", ".css": "text/css",
                 ".html": "text/html"}


@app.get("/")
def index():
    return FileResponse(INDEX_PATH, media_type="text/html")


@app.get("/static/{name}")
def static_file(name: str):
    path = os.path.join(STATIC_DIR, os.path.basename(name))
    if not os.path.isfile(path):
        return JSONResponse({"error": "not found"}, status_code=404)
    ext = os.path.splitext(path)[1]
    return FileResponse(path, media_type=_STATIC_TYPES.get(ext, "application/octet-stream"))


# --- TTS preview ----------------------------------------------------------------

@app.post("/preview")
def preview(
    text: str = Form(...),
    voice: str = Form(tts.DEFAULT_VOICE),
    speed: float = Form(1.05),
    steps: int = Form(8),
):
    text = jobs.normalize_text(text).strip()
    if not text:
        return JSONResponse({"error": "Порожній текст"}, status_code=400)
    if voice not in tts.VOICES:
        return JSONResponse({"error": f"Невідомий голос {voice}"}, status_code=400)
    speed, steps, silence = tts.clamp(speed, steps, 0.3)

    out = os.path.join(AUDIO_DIR, f"preview_{voice}.wav")
    tts.synthesize_native(text, voice, out, speed=speed, steps=steps, silence=silence)
    return {"voice": voice, "url": f"/audio/preview_{voice}.wav", "secs": tts.wav_seconds(out)}


# --- Campaign (§15) ---------------------------------------------------------------

@app.post("/start")
async def start(payload: dict = Body(...)):
    name = (payload.get("name") or "").strip() or "Кампанія"
    message = (payload.get("message") or "").strip()
    voice = payload.get("voice") or tts.DEFAULT_VOICE
    campaign_type = payload.get("campaign_type") or "info"
    if not message:
        return JSONResponse({"error": "Порожній текст повідомлення"}, status_code=400)
    if voice not in tts.VOICES:
        return JSONResponse({"error": f"Невідомий голос {voice}"}, status_code=400)
    if campaign_type not in ("info", "operator"):
        return JSONResponse({"error": f"Невідомий тип кампанії {campaign_type}"},
                            status_code=400)

    raw_numbers = payload.get("numbers") or []
    if isinstance(raw_numbers, str):
        raw_numbers = raw_numbers.splitlines()
    numbers, bad = [], []
    for raw in raw_numbers:
        num = jobs.normalize_number(str(raw))
        (numbers if num else bad).append(num or str(raw))
    if bad:
        return JSONResponse({"error": f"Некоректні номери: {', '.join(bad[:5])}"},
                            status_code=400)
    if not numbers:
        return JSONResponse({"error": "Вкажіть хоча б один номер"}, status_code=400)

    try:
        max_concurrent = int(payload.get("max_concurrent") or 1)
    except (TypeError, ValueError):
        return JSONResponse({"error": "max_concurrent має бути числом"}, status_code=400)
    if not 1 <= max_concurrent <= 5:
        return JSONResponse({"error": "max_concurrent поза межами 1..5"}, status_code=400)

    profile_id = payload.get("profile_id") or db.default_profile_id()
    profile = db.get_profile(profile_id)
    if profile is None:
        return JSONResponse({"error": "SIP-профіль не знайдено"}, status_code=400)

    try:
        compiled = flow_mod.compile_form(message, voice, payload.get("ivr"))
    except flow_mod.FlowError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except (TypeError, ValueError):
        return JSONResponse({"error": "Некоректна IVR-форма"}, status_code=400)

    if jobs.campaign_running():
        return JSONResponse({"detail": "campaign already running"}, status_code=409)
    campaign_id = db.create_campaign(
        name, campaign_type, message, voice, compiled,
        profile["id"], f"{profile['username']}@{profile['server']}",
        max_concurrent, numbers)
    err = jobs.start_campaign(campaign_id)
    if err:  # raced with another start
        db.set_campaign_status(campaign_id, "stopped", error=err, finished=True)
        return JSONResponse({"detail": err}, status_code=409)
    return {"campaign_id": campaign_id}


@app.get("/status")
async def status():
    snap = jobs.snapshot()
    # keep the stage-1 health info: is the engine reachable at all?
    try:
        client = await esl.shared_client()
        st = await client.api("status")
        snap["esl_connected"] = True
        snap["freeswitch"] = st.strip().splitlines()[0] if st.strip() else ""
        snap["operators"] = await _operator_states(client)
    except Exception as exc:  # noqa: BLE001 — health endpoint must not 500
        snap["esl_connected"] = False
        snap["esl_error"] = exc.__class__.__name__
    return snap


async def _operator_states(client):
    """Operators with live registration + busy flags (Plan.md §15)."""
    busy = jobs.busy_extensions()
    out = []
    for op in db.list_operators():
        registered = False
        try:
            registered = await operators_mod.is_registered(client, op["extension"])
        except Exception:  # noqa: BLE001 — a dead ESL must not break /status
            pass
        out.append({**op, "registered": registered,
                    "busy": op["extension"] in busy})
    return out


# --- stage-1 PoC: one ad-hoc call -------------------------------------------------

@app.post("/call")
async def call(
    number: str = Form(...),
    text: str = Form(...),
    voice: str = Form(tts.DEFAULT_VOICE),
    speed: float = Form(1.05),
    steps: int = Form(8),
):
    text = jobs.normalize_text(text).strip()
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
            client = await esl.shared_client()
        except Exception:
            logger.exception("ESL connection failed")
            return JSONResponse(
                {"error": "FreeSWITCH недоступний (ESL)"}, status_code=502)

        result = await jobs.call_once(client, num, tel)
    return {"ok": True, "number": num, **result}


# --- SIP profiles ------------------------------------------------------------------

@app.get("/config")
def config():
    return {"profiles": db.list_profiles(), "default_id": db.default_profile_id()}


@app.post("/config/profiles")
def create_profile(
    name: str = Form(...),
    server: str = Form(...),
    port: str = Form("5060"),
    username: str = Form(...),
    password: str = Form(""),
    is_default: bool = Form(False),
):
    err = _validate_profile(name, server, port, username)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    try:
        pid = db.create_profile(name.strip(), server.strip(), int(port),
                                username.strip(), password, is_default)
    except Exception:
        return JSONResponse({"error": f"Профіль «{name}» уже існує"}, status_code=409)
    return {"ok": True, "id": pid}


@app.post("/config/profiles/{profile_id}")
def update_profile(
    profile_id: int,
    name: str = Form(...),
    server: str = Form(...),
    port: str = Form("5060"),
    username: str = Form(...),
    password: str = Form(""),   # blank => keep the stored password
    is_default: bool = Form(False),
):
    if db.get_profile(profile_id) is None:
        return JSONResponse({"error": "Профіль не знайдено"}, status_code=404)
    err = _validate_profile(name, server, port, username)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    try:
        db.update_profile(profile_id, name.strip(), server.strip(), int(port),
                          username.strip(), password, is_default)
    except Exception:
        return JSONResponse({"error": f"Профіль «{name}» уже існує"}, status_code=409)
    return {"ok": True}


@app.delete("/config/profiles/{profile_id}")
def delete_profile(profile_id: int):
    db.delete_profile(profile_id)
    gateways.remove_gateway(profile_id)  # drop the generated trunk XML too
    return {"ok": True}


def _validate_profile(name, server, port, username):
    if not name.strip():
        return "Вкажіть назву профілю"
    if not server.strip() or not username.strip():
        return "Вкажіть сервер і логін"
    if not str(port).isdigit() or not 0 < int(port) < 65536:
        return f"Некоректний порт {port}"
    return None


# --- Operators (§15, stage 3) ------------------------------------------------------

@app.get("/config/operators")
async def list_operators():
    try:
        client = await esl.shared_client()
        return {"operators": await _operator_states(client)}
    except Exception:  # noqa: BLE001 — show the list even with the engine down
        return {"operators": [{**op, "registered": None, "busy": False}
                              for op in db.list_operators()]}


@app.post("/config/operators")
async def create_operator(payload: dict = Body(...)):
    name = (payload.get("name") or "").strip()
    extension = (payload.get("extension") or "").strip()
    password = payload.get("password") or ""
    if not name:
        return JSONResponse({"error": "Вкажіть ім'я оператора"}, status_code=400)
    if not operators_mod.EXTENSION_RE.match(extension):
        return JSONResponse({"error": f"Некоректний extension «{extension}» (3–6 цифр)"},
                            status_code=400)
    if len(password) < 6:
        return JSONResponse({"error": "Пароль закороткий (мінімум 6 символів)"},
                            status_code=400)
    try:
        op_id = db.create_operator(name, extension, password)
    except Exception:
        return JSONResponse({"error": f"Extension {extension} уже існує"}, status_code=409)
    operators_mod.write_extension(extension, password)
    reload_ok = True
    try:
        client = await esl.shared_client()
        reload_ok = "+OK" in await operators_mod.reloadxml(client)
    except Exception:  # noqa: BLE001 — the entry is durable; reload retried on demand
        reload_ok = False
        logger.warning("reloadxml failed; FreeSWITCH will pick the entry up later")
    return {"ok": True, "id": op_id, "reloadxml": reload_ok}


@app.delete("/config/operators/{operator_id}")
async def delete_operator(operator_id: int):
    op = db.get_operator(operator_id)
    if op is None:
        return JSONResponse({"error": "Оператора не знайдено"}, status_code=404)
    db.delete_operator(operator_id)
    operators_mod.remove_extension(op["extension"])
    try:
        client = await esl.shared_client()
        await operators_mod.reloadxml(client)
    except Exception:  # noqa: BLE001
        logger.warning("reloadxml failed after operator delete")
    return {"ok": True}


# --- History -----------------------------------------------------------------------

@app.get("/campaigns")
def campaigns():
    return {"campaigns": db.list_campaigns()}


@app.get("/campaigns/{campaign_id}")
def campaign(campaign_id: int):
    c = db.get_campaign(campaign_id)
    if c is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    item = {k: c[k] for k in c.keys() if k != "ivr_flow"}
    return {**item,
            "counts": db.counts(campaign_id),
            "numbers": db.campaign_numbers(campaign_id)}


@app.post("/campaigns/{campaign_id}/retry-failed")
async def retry_failed(campaign_id: int):
    c = db.get_campaign(campaign_id)
    if c is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    failed = [r["number"] for r in db.campaign_numbers(campaign_id)
              if r["status"] in db.RETRYABLE]  # optout is NEVER retried (§8)
    if not failed:
        return JSONResponse({"error": "Немає невдалих номерів для повтору"}, status_code=400)
    if c["profile_id"] is None or db.get_profile(c["profile_id"]) is None:
        return JSONResponse({"error": "SIP-профіль цієї кампанії вже видалено"}, status_code=400)
    if jobs.campaign_running():
        return JSONResponse({"detail": "campaign already running"}, status_code=409)
    new_id = db.create_campaign(
        f"{c['name']} (повтор)", c["campaign_type"], c["message_text"], c["voice"],
        json.loads(c["ivr_flow"]), c["profile_id"], c["profile_label"],
        c["max_concurrent"], failed)
    err = jobs.start_campaign(new_id)
    if err:
        db.set_campaign_status(new_id, "stopped", error=err, finished=True)
        return JSONResponse({"detail": err}, status_code=409)
    return {"ok": True, "campaign_id": new_id, "count": len(failed)}


@app.post("/campaigns/{campaign_id}/resume")
async def resume(campaign_id: int):
    err = jobs.resume_campaign(campaign_id)
    if err:
        return JSONResponse({"error": err}, status_code=409)
    return {"ok": True}


# --- audio ---------------------------------------------------------------------

@app.get("/audio/{name}")
def audio(name: str):
    path = os.path.join(AUDIO_DIR, os.path.basename(name))
    if not os.path.isfile(path):
        return JSONResponse({"error": "not found"}, status_code=404)
    # no-store so an overwritten file is never served stale
    return FileResponse(path, media_type="audio/wav", headers={"Cache-Control": "no-store"})
