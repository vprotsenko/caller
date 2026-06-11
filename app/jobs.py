"""Stage-1 PoC call logic: originate ONE number and play a WAV into the call.

This module grows into the campaign worker (Plan.md §2) at stage 2; the
call-level helpers (number normalization, dial string, originate command,
hangup-cause mapping) are pure functions so pytest covers them without a live
FreeSWITCH (Plan.md §16 level 1).
"""

import asyncio
import logging
import os
import re
import uuid as uuid_mod

from . import tts

logger = logging.getLogger(__name__)

AUDIO_DIR = os.environ.get("AUDIO_DIR", "/app/audio")
# {number} is substituted; loopback/9999/default in tests (Plan.md §16 level 4)
DIAL_STRING_TEMPLATE = os.environ.get(
    "DIAL_STRING_TEMPLATE", "sofia/gateway/flysip/{number}")
ORIGINATE_TIMEOUT = int(os.environ.get("ORIGINATE_TIMEOUT", "30"))

# Digits only (optional leading +): keeps originate-command injection
# (spaces, braces, pipes) impossible by construction.
_NUMBER_RE = re.compile(r"^\+?\d{3,15}$")

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


def build_originate_cmd(dial_string, wav_path, call_uuid):
    """originate command playing `wav_path` to the callee once answered.

    ignore_early_media: with 183 early media (common on mobile networks) the
    call must NOT count as answered before the real 200 OK, or the message
    plays into the ringing tone (v1 lesson — do not "optimize" away).
    The WAV itself already starts with tts.LEAD_IN_SECONDS of silence.
    """
    overrides = ",".join([
        f"origination_uuid={call_uuid}",
        f"originate_timeout={ORIGINATE_TIMEOUT}",
        "ignore_early_media=true",
    ])
    return f"originate {{{overrides}}}{dial_string} &playback({wav_path})"


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
