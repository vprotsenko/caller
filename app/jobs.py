"""Campaign worker (Plan.md §2, §6, §7) + the stage-1 single-call PoC.

One campaign at a time (a second start gets a 409 upstream). The worker
prerenders all flow prompts to WAV (hash cache — the campaign does not start
if synthesis fails, §5), then dials pending numbers keeping at most
`max_concurrent` calls in flight (§7), writing every outcome to SQLite as it
happens (§6: durable, restart -> 'interrupted', resume is explicit).

Pure helpers (number normalization, dial string, originate command, cause
mapping) are unit-tested without FreeSWITCH (§16 level 1).
"""

import asyncio
import collections
import hashlib
import logging
import os
import re
import time
import uuid as uuid_mod

from . import db, esl, ivr, operators as operators_mod, tts

logger = logging.getLogger(__name__)

AUDIO_DIR = os.environ.get("AUDIO_DIR", "/app/audio")
# {number} is substituted; loopback/9999/default in tests (Plan.md §16 level 4)
DIAL_STRING_TEMPLATE = os.environ.get(
    "DIAL_STRING_TEMPLATE", "sofia/gateway/flysip/{number}")
ORIGINATE_TIMEOUT = int(os.environ.get("ORIGINATE_TIMEOUT", "30"))
# Extra channel vars prepended to every originate. Empty in production (the
# trunk negotiates codecs); the loopback E2E env sets e.g.
# absolute_codec_string=PCMA so the loopback leg and the SIP operator leg share
# a codec and the bridge needs no transcoding (Plan.md §16).
ORIGINATE_EXTRA_VARS = os.environ.get("ORIGINATE_EXTRA_VARS", "").strip()
# Same idea for the operator bridge leg (loopback test pins a shared codec).
BRIDGE_EXTRA_VARS = os.environ.get("BRIDGE_EXTRA_VARS", "").strip()
IVR_HOST = os.environ.get("IVR_HOST", "127.0.0.1")
IVR_PORT = int(os.environ.get("IVR_PORT", "8084"))
# hard per-call budget after answer: longest message + menus must fit
ANSWERED_CALL_BUDGET = int(os.environ.get("ANSWERED_CALL_BUDGET", "600"))
# operator bridging (stage 3)
OPERATOR_RING_TIMEOUT = int(os.environ.get("OPERATOR_RING_TIMEOUT", "25"))
BRIDGE_MAX_SECONDS = int(os.environ.get("BRIDGE_MAX_SECONDS", "3600"))

# Digits only (optional leading +): keeps originate-command injection
# (spaces, braces, pipes) impossible by construction.
_NUMBER_RE = re.compile(r"^\+?\d{3,15}$")

# Supertonic rejects typographic apostrophes/dashes common in Ukrainian text
# (U+02BC у «зʼєднати» тощо) — map them to supported ASCII before synthesis.
_TEXT_REPLACEMENTS = str.maketrans({
    "ʼ": "'",  # MODIFIER LETTER APOSTROPHE (український апостроф)
    "’": "'",  # RIGHT SINGLE QUOTATION MARK
    "‘": "'",
    "ʹ": "'",
    "“": '"',
    "”": '"',
    "«": '"',
    "»": '"',
    "–": "-",
    "—": "-",
    "…": "...",
    " ": " ",  # non-breaking space
})


def normalize_text(text):
    """Make typographic punctuation synthesizable (Supertonic rejects it)."""
    return (text or "").translate(_TEXT_REPLACEMENTS)

# Hangup cause -> campaign_number.status (Plan.md §6). Anything else, or an
# originate refused by the provider, is "failed" — often a billing/route
# problem on the trunk, not an app bug.
_BUSY_CAUSES = {"USER_BUSY"}
_NO_ANSWER_CAUSES = {"NO_ANSWER", "ORIGINATOR_CANCEL", "NO_USER_RESPONSE"}


def normalize_number(raw):
    """Strip separators; return '+380...' style string or None if invalid."""
    if raw is None:
        return None
    cleaned = re.sub(r"[\s\-().]", "", raw.strip())
    return cleaned if _NUMBER_RE.match(cleaned) else None


def status_for(cause, answered):
    """Map a Q.850 hangup cause name to a campaign_number status."""
    if answered:
        return "answered"
    cause = (cause or "").upper()
    if cause in _BUSY_CAUSES:
        return "busy"
    if cause in _NO_ANSWER_CAUSES:
        return "no-answer"
    return "failed"


def build_dial_string(number, template=None):
    num = normalize_number(number)
    if num is None:
        raise ValueError(f"invalid number: {number!r}")
    return (template or DIAL_STRING_TEMPLATE).format(number=num)


def _originate_vars(call_uuid):
    """ignore_early_media: with 183 early media (common on mobile networks)
    the call must NOT count as answered before the real 200 OK, or the message
    plays into the ringing tone (v1 lesson — do not "optimize" away)."""
    parts = [
        f"origination_uuid={call_uuid}",
        f"originate_timeout={ORIGINATE_TIMEOUT}",
        "ignore_early_media=true",
    ]
    if ORIGINATE_EXTRA_VARS:
        parts.append(ORIGINATE_EXTRA_VARS)
    return ",".join(parts)


def build_originate_cmd(dial_string, wav_path, call_uuid):
    """Stage-1 PoC shape: play one WAV to the callee once answered."""
    return (f"originate {{{_originate_vars(call_uuid)}}}"
            f"{dial_string} &playback({wav_path})")


def build_campaign_originate_cmd(dial_string, call_uuid,
                                 ivr_host=None, ivr_port=None):
    """Campaign shape: hand the answered call to the outbound socket (§3)."""
    host = ivr_host or IVR_HOST
    port = ivr_port or IVR_PORT
    return (f"originate {{{_originate_vars(call_uuid)}}}"
            f"{dial_string} &socket({host}:{port} async full)")


def outcome_status(outcome):
    """Final status of an ANSWERED call from the IVR outcome (Plan.md §6)."""
    if outcome.get("mark") == "optout":
        return "optout"
    if outcome.get("transferred"):
        return "transferred"
    if outcome.get("bridge_attempted"):
        return "missed-operator"  # wanted an operator, never got bridged
    return "answered"


def prompt_path(text, voice):
    digest = hashlib.sha1(f"{text}|{voice}|{tts.DEFAULT_LANG}".encode()).hexdigest()[:16]
    return os.path.join(AUDIO_DIR, f"prompt_{digest}.wav")


def prerender_prompts(flow):
    """Synthesize every flow prompt to a telephony WAV (cache by hash).

    Returns {prompt_name: wav_path}. Raises if any synthesis fails — the
    campaign must not start half-mute (§5)."""
    files = {}
    for name, prompt in flow["prompts"].items():
        text = normalize_text(prompt["text"])
        path = prompt_path(text, prompt["voice"])
        if not os.path.isfile(path):
            native = path + ".native.wav"
            tts.synthesize_telephony(text, prompt["voice"], native, path)
            try:
                os.remove(native)
            except OSError:
                pass
        files[name] = path
    return files


# --- live campaign state (durable state lives in SQLite) ------------------------

_active = {
    "campaign_id": None,
    "task": None,
    "calls": {},   # call_uuid -> {"number": ..., "state": dialing|ivr}
    "log": collections.deque(maxlen=40),
    "stopping": False,
    "pool": None,  # OperatorPool while an operator campaign runs
}


def busy_extensions():
    pool = _active.get("pool")
    return pool.busy_extensions() if pool else set()


def _log(msg):
    line = time.strftime("%H:%M:%S ") + msg
    _active["log"].append(line)
    logger.info("campaign: %s", msg)


def campaign_running():
    task = _active["task"]
    return task is not None and not task.done()


def start_campaign(campaign_id):
    """Spawn the worker for a freshly created campaign. Returns error or None."""
    if campaign_running():
        return "campaign already running"
    _active.update(campaign_id=campaign_id, calls={}, stopping=False)
    _active["log"].clear()
    _active["task"] = asyncio.get_running_loop().create_task(_run_campaign(campaign_id))
    return None


def resume_campaign(campaign_id):
    """Re-run an interrupted campaign over its still-pending numbers."""
    if campaign_running():
        return "campaign already running"
    row = db.get_campaign(campaign_id)
    if row is None:
        return "campaign not found"
    if row["status"] != "interrupted":
        return f"campaign is {row['status']}, not interrupted"
    db.reset_ringing_to_pending(campaign_id)
    db.set_campaign_status(campaign_id, db.RUNNING)
    return start_campaign(campaign_id)


async def _run_campaign(campaign_id):
    try:
        campaign = db.get_campaign(campaign_id)
        flow = db.campaign_flow(campaign_id)
        _log(f"кампанія #{campaign_id} «{campaign['name']}»: синтез промптів")
        try:
            files = await asyncio.to_thread(prerender_prompts, flow)
        except Exception as exc:  # noqa: BLE001 — campaign must not start half-mute
            logger.exception("prompt synthesis failed")
            db.set_campaign_status(campaign_id, "stopped",
                                   error=f"Синтез не вдався: {exc}", finished=True)
            _log("синтез не вдався — кампанію зупинено")
            return

        try:
            client = await esl.shared_client()
        except Exception as exc:  # noqa: BLE001
            logger.exception("ESL unavailable")
            db.set_campaign_status(campaign_id, "stopped",
                                   error=f"FreeSWITCH недоступний: {exc.__class__.__name__}",
                                   finished=True)
            _log("FreeSWITCH недоступний — кампанію зупинено")
            return

        db.set_campaign_status(campaign_id, db.RUNNING, started=True)
        max_concurrent = max(1, int(campaign["max_concurrent"] or 1))
        pool = None
        if campaign["campaign_type"] == "operator":
            pool = operators_mod.OperatorPool(client)
            _active["pool"] = pool
        _log(f"набір почато (одночасно: {max_concurrent})")

        tasks = set()
        waiting_logged = False
        while not _active["stopping"]:
            allowed = max_concurrent
            if pool is not None:
                # §7: no more new originates than free registered operators
                allowed = min(allowed, len(tasks) + await pool.free_count())
            while len(tasks) < allowed:
                row = db.claim_next_pending(campaign_id)
                if row is None:
                    break
                waiting_logged = False
                tasks.add(asyncio.create_task(
                    _dial_number(client, row, flow, files, pool)))
            if not tasks:
                if db.next_pending_number(campaign_id) is None:
                    break
                # operator campaign with nobody free/registered: wait, don't dial
                if not waiting_logged:
                    _log("очікую вільного зареєстрованого оператора")
                    waiting_logged = True
                await asyncio.sleep(2)
                continue
            done, tasks = await asyncio.wait(
                tasks, timeout=5, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if t.exception():
                    logger.error("dial task failed", exc_info=t.exception())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        final = "stopped" if _active["stopping"] else "done"
        db.set_campaign_status(campaign_id, final, finished=True)
        _log(f"кампанія завершена: {final}")
    except Exception:  # noqa: BLE001 — the worker must record its own death
        logger.exception("campaign worker crashed")
        db.set_campaign_status(campaign_id, "stopped",
                               error="внутрішня помилка воркера", finished=True)
    finally:
        _active["calls"] = {}
        _active["pool"] = None


async def _dial_number(client, row, flow, files, pool=None):
    number_id, number = row["id"], row["number"]
    call_uuid = str(uuid_mod.uuid4())
    ctx = ivr.CallContext(number_id, flow, files, operators=pool,
                          ring_timeout=OPERATOR_RING_TIMEOUT,
                          bridge_max=BRIDGE_MAX_SECONDS,
                          bridge_vars=BRIDGE_EXTRA_VARS)
    ivr.REGISTRY[call_uuid] = ctx
    hangup_fut = client.expect_event("CHANNEL_HANGUP_COMPLETE", call_uuid)
    _active["calls"][call_uuid] = {"number": number, "state": "dialing"}
    try:
        cmd = build_campaign_originate_cmd(build_dial_string(number), call_uuid)
        try:
            reply = await client.bgapi(cmd, timeout=ORIGINATE_TIMEOUT + 30)
        except Exception as exc:  # noqa: BLE001 — ESL drop mid-campaign
            db.set_number_status(number_id, "failed",
                                 hangup_cause=f"ESL:{exc.__class__.__name__}",
                                 bump_attempt=True)
            _log(f"{number} failed (ESL)")
            return

        if not reply.startswith("+OK"):
            cause = reply.replace("-ERR", "").strip() or "UNKNOWN"
            status = status_for(cause, answered=False)
            db.set_number_status(number_id, status, hangup_cause=cause, bump_attempt=True)
            _log(f"{number} {status} ({cause})")
            return

        _active["calls"][call_uuid]["state"] = "ivr"
        budget = ANSWERED_CALL_BUDGET + (BRIDGE_MAX_SECONDS if pool else 0)
        try:
            outcome = await asyncio.wait_for(ctx.done, budget)
        except asyncio.TimeoutError:
            await client.api(f"uuid_kill {call_uuid}")
            db.set_number_status(number_id, "failed", hangup_cause="APP_TIMEOUT",
                                 bump_attempt=True)
            _log(f"{number} failed (застряг після відповіді)")
            return
        except Exception as exc:  # noqa: BLE001 — broken IVR session
            db.set_number_status(number_id, "failed",
                                 hangup_cause=f"IVR:{exc.__class__.__name__}",
                                 bump_attempt=True)
            _log(f"{number} failed (IVR)")
            return

        db.append_dtmf(number_id, outcome.get("dtmf", ""))
        status = outcome_status(outcome)
        cause = outcome.get("hangup_cause") or "NORMAL_CLEARING"
        db.set_number_status(number_id, status, hangup_cause=cause, bump_attempt=True)
        dtmf_note = f", dtmf={outcome['dtmf']}" if outcome.get("dtmf") else ""
        _log(f"{number} {status} ({cause}{dtmf_note})")
    finally:
        ivr.REGISTRY.pop(call_uuid, None)
        client.cancel_waiter("CHANNEL_HANGUP_COMPLETE", call_uuid)
        _active["calls"].pop(call_uuid, None)


def snapshot():
    """Active-or-latest campaign state for GET /status (Plan.md §15)."""
    campaign_id = _active["campaign_id"] if campaign_running() else None
    campaign_id = campaign_id or db.latest_campaign_id()
    if campaign_id is None:
        return {"phase": "idle", "campaign_id": None, "log": list(_active["log"])}
    campaign = db.get_campaign(campaign_id)
    counts = db.counts(campaign_id)
    current = None
    for state in ("ivr", "dialing"):
        for info in _active["calls"].values():
            if info["state"] == state:
                current = dict(info)
                break
        if current:
            break
    return {
        "campaign_id": campaign_id,
        "name": campaign["name"],
        "phase": campaign["status"],
        "error": campaign["error"],
        "total": counts["total"],
        "counts": counts,
        "current": current,
        "operators": [],  # stage 3
        "log": list(_active["log"]),
    }


# --- stage-1 PoC: one ad-hoc call, no DB ----------------------------------------

async def call_once(client, number, wav_path, template=None):
    """Dial one number, play the WAV, return the outcome.

    Returns {"answered": bool, "cause": str, "status": str, "uuid": str}.
    """
    call_uuid = str(uuid_mod.uuid4())
    hangup_fut = client.expect_event("CHANNEL_HANGUP_COMPLETE", call_uuid)
    cmd = build_originate_cmd(build_dial_string(number, template), wav_path, call_uuid)
    logger.info("Originating %s (uuid=%s)", number, call_uuid)

    reply = await client.bgapi(cmd, timeout=ORIGINATE_TIMEOUT + 30)
    if not reply.startswith("+OK"):
        client.cancel_waiter("CHANNEL_HANGUP_COMPLETE", call_uuid)
        cause = reply.replace("-ERR", "").strip() or "UNKNOWN"
        result = {"answered": False, "cause": cause,
                  "status": status_for(cause, False), "uuid": call_uuid}
        logger.info("Call %s not answered: %s", number, result)
        return result

    # Answered: originate returns +OK only on the real 200 OK. Wait for the
    # hangup to learn how the playback leg ended.
    budget = tts.wav_seconds(wav_path) + 60
    try:
        event = await asyncio.wait_for(hangup_fut, timeout=budget)
        cause = event.get("Hangup-Cause", "NONE")
    except asyncio.TimeoutError:
        client.cancel_waiter("CHANNEL_HANGUP_COMPLETE", call_uuid)
        logger.error("Call %s stuck after answer, killing %s", number, call_uuid)
        await client.api(f"uuid_kill {call_uuid}")
        return {"answered": True, "cause": "APP_TIMEOUT",
                "status": "failed", "uuid": call_uuid}

    result = {"answered": True, "cause": cause,
              "status": status_for(cause, True), "uuid": call_uuid}
    logger.info("Call %s finished: %s", number, result)
    return result
