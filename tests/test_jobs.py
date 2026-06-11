"""Pure call-logic helpers: numbers, dial strings, cause mapping (§16 level 1)."""

import asyncio

import pytest

from app import jobs


# --- number normalization -------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("+380671234567", "+380671234567"),
    ("+380 67 123-45-67", "+380671234567"),
    ("(067) 123.45.67", "0671234567"),
    ("9999", "9999"),
])
def test_normalize_number_accepts(raw, expected):
    assert jobs.normalize_number(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "   ", None, "abc", "12", "+", "123456789012345678901",
    # originate-command injection attempts must die here, by construction
    "123;api shutdown", "{origination_uuid=x}123", "123 &park()", "123|456",
])
def test_normalize_number_rejects(raw):
    assert jobs.normalize_number(raw) is None


# --- dial string / originate command ---------------------------------------------

def test_build_dial_string_substitutes_gateway():
    assert jobs.build_dial_string(
        "+380671234567", gateway="gw_profile_2"
    ) == "sofia/gateway/gw_profile_2/+380671234567"


def test_build_dial_string_loopback_template_ignores_gateway():
    # the loopback override has no {gw}; the gateway arg is harmlessly ignored
    assert jobs.build_dial_string(
        "9999", gateway="gw_profile_9", template="loopback/{number}/default"
    ) == "loopback/9999/default"


def test_build_dial_string_rejects_garbage():
    with pytest.raises(ValueError):
        jobs.build_dial_string("not-a-number")


def test_build_originate_cmd_shape():
    cmd = jobs.build_originate_cmd("loopback/9999/default", "/app/audio/call.wav", "uuid-1")
    assert cmd.startswith("originate {")
    assert "origination_uuid=uuid-1" in cmd
    assert "originate_timeout=" in cmd
    assert "ignore_early_media=true" in cmd
    assert cmd.endswith("loopback/9999/default &playback(/app/audio/call.wav)")


def test_build_campaign_originate_cmd_hands_off_to_socket():
    cmd = jobs.build_campaign_originate_cmd(
        "loopback/9999/default", "uuid-1", ivr_host="127.0.0.1", ivr_port=8084)
    assert "origination_uuid=uuid-1" in cmd
    assert cmd.endswith("loopback/9999/default &socket(127.0.0.1:8084 async full)")


def test_originate_vars_include_caller_id(monkeypatch):
    monkeypatch.setattr(jobs, "CALLER_ID_NUMBER", "380441234567")
    monkeypatch.setattr(jobs, "CALLER_ID_NAME", "Acme")
    cmd = jobs.build_originate_cmd("sofia/gateway/gw/3", "/a.wav", "u1")
    assert "origination_caller_id_number=380441234567" in cmd
    assert "origination_caller_id_name='Acme'" in cmd


def test_originate_vars_no_caller_id_when_unset(monkeypatch):
    monkeypatch.setattr(jobs, "CALLER_ID_NUMBER", "")
    monkeypatch.setattr(jobs, "CALLER_ID_NAME", "")
    cmd = jobs.build_originate_cmd("sofia/gateway/gw/3", "/a.wav", "u1")
    assert "caller_id" not in cmd


@pytest.mark.parametrize("outcome,expected", [
    ({"mark": "optout", "transferred": False}, "optout"),
    ({"mark": None, "transferred": True}, "transferred"),
    ({"mark": None, "transferred": False}, "answered"),
    ({}, "answered"),
    # optout wins: the caller asked out even if an operator was reached
    ({"mark": "optout", "transferred": True}, "optout"),
    # wanted an operator but never got bridged -> missed-operator (§6)
    ({"bridge_attempted": True, "transferred": False}, "missed-operator"),
    ({"bridge_attempted": True, "transferred": True}, "transferred"),
    # AMD terminal actions win over everything (§6)
    ({"amd_action": "voicemail"}, "voicemail-left"),
    ({"amd_action": "machine_hangup"}, "machine-hangup"),
    ({"amd_action": "voicemail", "mark": "optout"}, "voicemail-left"),
])
def test_outcome_status(outcome, expected):
    assert jobs.outcome_status(outcome) == expected


def test_normalize_text_typographic_punctuation():
    # Supertonic rejects U+02BC (український апостроф) та типографські знаки
    assert jobs.normalize_text("зʼєднати") == "з'єднати"
    assert jobs.normalize_text("з’єднати") == "з'єднати"
    assert jobs.normalize_text("«Привіт» — сказав він…") == '"Привіт" - сказав він...'
    assert jobs.normalize_text(None) == ""


def test_prompt_path_is_cache_key(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "AUDIO_DIR", str(tmp_path))
    a = jobs.prompt_path("Привіт", "F3")
    assert a == jobs.prompt_path("Привіт", "F3")      # stable
    assert a != jobs.prompt_path("Привіт!", "F3")     # text changes the key
    assert a != jobs.prompt_path("Привіт", "M1")      # voice changes the key


# --- hangup cause -> status (Plan.md §6) -----------------------------------------

@pytest.mark.parametrize("cause,answered,expected", [
    ("NORMAL_CLEARING", True, "answered"),
    ("USER_BUSY", False, "busy"),
    ("NO_ANSWER", False, "no-answer"),
    ("ORIGINATOR_CANCEL", False, "no-answer"),
    ("NO_USER_RESPONSE", False, "no-answer"),
    ("CALL_REJECTED", False, "failed"),
    ("GATEWAY_DOWN", False, "failed"),
    ("NO_ROUTE_DESTINATION", False, "failed"),
    ("", False, "failed"),
    (None, False, "failed"),
    # answered wins even over a weird cause: the callee heard the message
    ("MEDIA_TIMEOUT", True, "answered"),
])
def test_status_for(cause, answered, expected):
    assert jobs.status_for(cause, answered) == expected


# --- call_once against a stub client ---------------------------------------------

class StubClient:
    """ESL client double: scripted bgapi reply + optional hangup event."""

    def __init__(self, bgapi_reply, hangup_event=None):
        self._bgapi_reply = bgapi_reply
        self._hangup_event = hangup_event
        self.killed = []
        self._fut = None

    def expect_event(self, name, uuid):
        self._fut = asyncio.get_running_loop().create_future()
        if self._hangup_event is not None:
            self._fut.set_result(self._hangup_event)
        return self._fut

    def cancel_waiter(self, name, uuid):
        if self._fut and not self._fut.done():
            self._fut.cancel()

    async def bgapi(self, cmd, timeout=None):
        return self._bgapi_reply

    async def api(self, cmd, timeout=None):
        self.killed.append(cmd)
        return "+OK"


async def test_call_once_answered(tmp_path):
    wav = make_silence_wav(tmp_path / "x.wav")
    client = StubClient("+OK abc", {"Hangup-Cause": "NORMAL_CLEARING"})
    result = await jobs.call_once(client, "+380671234567", str(wav))
    assert result["status"] == "answered"
    assert result["answered"] is True
    assert result["cause"] == "NORMAL_CLEARING"


async def test_call_once_busy(tmp_path):
    wav = make_silence_wav(tmp_path / "x.wav")
    client = StubClient("-ERR USER_BUSY")
    result = await jobs.call_once(client, "+380671234567", str(wav))
    assert result == {"answered": False, "cause": "USER_BUSY",
                      "status": "busy", "uuid": result["uuid"]}


async def test_call_once_no_answer(tmp_path):
    wav = make_silence_wav(tmp_path / "x.wav")
    client = StubClient("-ERR ORIGINATOR_CANCEL")
    result = await jobs.call_once(client, "+380671234567", str(wav))
    assert result["status"] == "no-answer"


def make_silence_wav(path, seconds=0.1, rate=8000):
    import wave
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00" * int(seconds * rate) * 2)
    return path
