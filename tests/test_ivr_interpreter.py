"""Flow interpreter against a scripted fake session (§16 level 1).

No FreeSWITCH: the fake session records played prompts and serves DTMF digits
from a script.
"""

import asyncio

from app import flow as flow_mod, ivr


class FakeSession:
    """Scripted session: queue of digits; None = let the wait time out."""

    def __init__(self, digits=()):
        self.digits = list(digits)
        self.played = []
        self.hung_up = False
        self.hangup_cause = None
        self.die_after_plays = None  # simulate callee hangup mid-flow
        self.bridged = False
        self.bridge_answers = False  # does the "operator" pick up?
        self.bridge_targets = []

    async def play(self, path):
        if self.die_after_plays is not None and len(self.played) >= self.die_after_plays:
            self.hangup_cause = "NORMAL_CLEARING"
            raise ivr.CallEnded("NORMAL_CLEARING")
        self.played.append(path)

    async def wait_digit(self, timeout, prompt=None):
        if prompt:  # PAGD грає анонс сам (barge-in); фейк рахує його як play
            await self.play(prompt)
        if not self.digits:
            return None
        digit = self.digits.pop(0)
        if digit is None:
            return None
        return digit

    async def bridge(self, dial_target, ring_timeout, max_seconds, extra_vars=""):
        self.bridge_targets.append(dial_target)
        self.bridged = self.bridge_answers
        return self.bridged

    async def hangup(self, cause="NORMAL_CLEARING"):
        self.hung_up = True

    # --- AMD (stage 4) ---
    amd_verdict = "HUMAN"
    beep_detected = False

    async def detect_amd(self, amd_available=False, timeout=None):
        return self.amd_verdict

    async def wait_beep(self, timeout=None):
        return self.beep_detected


class FakePool:
    """Operator pool double: a fixed list of free extensions."""

    def __init__(self, free=()):
        self.free = list(free)
        self.released = []

    async def acquire(self):
        return self.free.pop(0) if self.free else None

    def release(self, ext):
        self.released.append(ext)

    def dial_target(self, ext):
        return f"user/{ext}@test.domain"


FILES = {"main": "/a/main.wav", "menu": "/a/menu.wav",
         "connect_1": "/a/conn.wav", "optout_ok_0": "/a/opt.wav"}

FORM = {
    "operator": {"enabled": True, "connect_text": "Зачекайте"},
    "repeat": {"enabled": True, "max": 2},
    "optout": {"enabled": True, "confirm_text": "Видалено"},
    "timeout_sec": 1,
    "on_timeout": "hangup",
}


def make_flow(form=FORM):
    return flow_mod.compile_form("Привіт", "F3", form)


async def test_timeout_plays_message_and_hangs_up():
    session = FakeSession(digits=[])  # nobody presses anything
    outcome = await ivr.run_flow(session, make_flow(), FILES)
    # анонс меню звучить на кожному з 3 раундів очікування (max_repeats=2)
    assert session.played == ["/a/main.wav", "/a/menu.wav", "/a/menu.wav", "/a/menu.wav"]
    assert session.hung_up
    assert outcome["dtmf"] == ""
    assert outcome["mark"] is None


async def test_press_2_replays_message():
    session = FakeSession(digits=["2"])
    outcome = await ivr.run_flow(session, make_flow(), FILES)
    assert session.played.count("/a/main.wav") == 2  # replayed once
    assert session.played[:3] == ["/a/main.wav", "/a/menu.wav", "/a/main.wav"]
    assert outcome["dtmf"] == "2"
    assert session.hung_up


async def test_press_0_optout():
    session = FakeSession(digits=["0"])
    outcome = await ivr.run_flow(session, make_flow(), FILES)
    assert "/a/opt.wav" in session.played
    assert outcome["mark"] == "optout"
    assert outcome["dtmf"] == "0"
    assert session.hung_up


async def test_press_1_no_pool_is_missed():
    session = FakeSession(digits=["1"])
    outcome = await ivr.run_flow(session, make_flow(), FILES)
    assert "/a/conn.wav" in session.played
    assert outcome["bridge_attempted"] is True
    assert outcome["transferred"] is False
    assert session.hung_up


async def test_press_1_operator_answers_transferred():
    session = FakeSession(digits=["1"])
    session.bridge_answers = True
    pool = FakePool(free=["1001"])
    outcome = await ivr.run_flow(session, make_flow(), FILES, operators=pool)
    assert session.bridge_targets == ["user/1001@test.domain"]
    assert outcome["transferred"] is True
    assert outcome["bridge_target"] == "1001"
    assert pool.released == ["1001"]  # operator freed after the bridge


async def test_press_1_operator_does_not_answer_missed():
    session = FakeSession(digits=["1"])
    session.bridge_answers = False
    pool = FakePool(free=["1001"])
    outcome = await ivr.run_flow(session, make_flow(), FILES, operators=pool)
    assert outcome["bridge_attempted"] is True
    assert outcome["transferred"] is False
    assert pool.released == ["1001"]


async def test_press_1_nobody_free_missed():
    session = FakeSession(digits=["1"])
    pool = FakePool(free=[])
    outcome = await ivr.run_flow(session, make_flow(), FILES, operators=pool)
    assert outcome["bridge_attempted"] is True
    assert outcome["bridge_target"] is None
    assert outcome["transferred"] is False
    assert session.hung_up


async def test_invalid_digit_then_valid_within_repeats():
    session = FakeSession(digits=["7", "0"])
    outcome = await ivr.run_flow(session, make_flow(), FILES)
    assert outcome["dtmf"] == "70"
    assert outcome["mark"] == "optout"


async def test_repeats_exhausted_goes_to_on_timeout():
    # max_repeats=2 -> 3 waiting rounds, all invalid -> bye
    session = FakeSession(digits=["7", "8", "9", "0"])
    outcome = await ivr.run_flow(session, make_flow(), FILES)
    assert outcome["dtmf"] == "789"   # the "0" was never consumed
    assert outcome["mark"] is None
    assert session.hung_up


async def test_callee_hangup_mid_flow_returns_partial_outcome():
    session = FakeSession(digits=["2"])
    session.die_after_plays = 2  # dies on the replay (after main + menu)
    outcome = await ivr.run_flow(session, make_flow(), FILES)
    assert outcome["dtmf"] == "2"
    assert not session.hung_up  # the callee hung up, not us


async def test_cycle_guard_stops_runaway_flow():
    # 2 -> msg -> menu -> 2 -> msg ... forever; MAX_STEPS must cut it
    session = FakeSession(digits=["2"] * 100)
    outcome = await ivr.run_flow(session, make_flow(), FILES)
    assert session.hung_up
    assert len(session.played) <= ivr.MAX_STEPS
    assert len(outcome["dtmf"]) <= ivr.MAX_STEPS


async def test_on_digit_callback_fires():
    seen = []
    session = FakeSession(digits=["0"])
    await ivr.run_flow(session, make_flow(), FILES, on_digit=seen.append)
    assert seen == ["0"]


# --- nested menu tree (рекурсивна форма §15) ---------------------------------------

TREE_FORM = {
    "timeout_sec": 1,
    "menu": {"options": [
        {"digit": "1", "action": "operator"},
        {"digit": "3", "action": "menu", "label": "Графік роботи", "menu": {
            "text": "Працюємо з девʼятої до вісімнадцятої.",
            "options": [
                {"digit": "1", "action": "play", "label": "Субота",
                 "text": "У суботу коротший день.", "then": "stay"},
                {"digit": "9", "action": "back"},
            ],
        }},
        {"digit": "0", "action": "optout"},
    ]},
}


def make_tree():
    flow = flow_mod.compile_form("Привіт", "F3", TREE_FORM)
    files = {name: f"/a/{name}.wav" for name in flow["prompts"]}
    return flow, files


async def test_tree_walk_submenu_info_back_optout():
    # 3 → підменю (текст рівня + анонс), 1 → фраза і назад у те ж меню,
    # 9 → назад у корінь, 0 → відписка
    flow, files = make_tree()
    session = FakeSession(digits=["3", "1", "9", "0"])
    outcome = await ivr.run_flow(session, flow, files)
    assert session.played == [
        "/a/main.wav", "/a/menu.wav",          # корінь: повідомлення + анонс
        "/a/text_3.wav", "/a/menu_3.wav",      # вхід у підменю
        "/a/info_3_1.wav", "/a/menu_3.wav",    # фраза, then=stay → той самий рівень
        "/a/menu.wav",                         # назад: анонс кореня (без повтору msg)
        "/a/optout_ok_0.wav",                  # відписка
    ]
    assert outcome["dtmf"] == "3190"
    assert outcome["mark"] == "optout"
    assert session.hung_up


async def test_tree_deep_wandering_survives_step_guard():
    # блукання туди-сюди по дереву легітимне: ліміт кроків росте з графом
    # (10 раундів × 3 переходи + вхід/вихід = 34 кроки > старого MAX_STEPS=30)
    flow, files = make_tree()
    session = FakeSession(digits=["3", "9"] * 10 + ["0"])
    outcome = await ivr.run_flow(session, flow, files)
    assert outcome["mark"] == "optout"
    assert outcome["dtmf"].endswith("0")


# --- AMD step in run_call (§6, stage 4) -------------------------------------------

def make_ctx(session_flow=None, campaign_type="info", amd_enabled=True):
    ctx = ivr.CallContext(1, session_flow or make_flow(), FILES,
                          campaign_type=campaign_type, amd_enabled=amd_enabled)
    return ctx


async def test_amd_disabled_runs_flow_directly():
    session = FakeSession(digits=[])
    ctx = make_ctx(amd_enabled=False)
    outcome = await ivr.run_call(session, ctx)
    # перед потоком — початкова тиша (ear-to-phone + медіа-реузгодження)
    assert session.played[0] == f"silence_stream://{ivr.LEAD_IN_MS}"
    assert session.played[1] == "/a/main.wav"  # flow ran
    assert outcome["amd_result"] is None


async def test_amd_human_continues_to_flow():
    session = FakeSession(digits=["0"])
    session.amd_verdict = "HUMAN"
    outcome = await ivr.run_call(session, make_ctx(campaign_type="info"))
    assert outcome["amd_result"] == "HUMAN"
    assert outcome["mark"] == "optout"           # the flow ran, 0 pressed
    assert "/a/main.wav" in session.played


async def test_amd_machine_info_drops_voicemail():
    session = FakeSession()
    session.amd_verdict = "MACHINE"
    session.beep_detected = True
    outcome = await ivr.run_call(session, make_ctx(campaign_type="info"))
    assert outcome["amd_result"] == "MACHINE"
    assert outcome["amd_action"] == ivr.amd_mod.VOICEMAIL
    assert session.played == ["/a/main.wav"]      # message dropped once
    assert session.hung_up
    assert outcome["dtmf"] == ""                  # no menu on a machine


async def test_amd_machine_operator_hangs_up():
    session = FakeSession()
    session.amd_verdict = "MACHINE"
    outcome = await ivr.run_call(session, make_ctx(campaign_type="operator"))
    assert outcome["amd_action"] == ivr.amd_mod.MACHINE_HANGUP
    assert session.played == []                   # nothing played, just hangup
    assert session.hung_up


async def test_amd_notsure_continues():
    session = FakeSession(digits=[])
    session.amd_verdict = "NOTSURE"
    outcome = await ivr.run_call(session, make_ctx(campaign_type="operator"))
    assert outcome["amd_result"] == "NOTSURE"
    assert "/a/main.wav" in session.played        # doubtful -> human path


# --- OutboundSession.detect_amd reads vars from connect data (no api) ----------

def bare_session(channel_vars):
    sess = ivr.OutboundSession.__new__(ivr.OutboundSession)
    sess.channel = channel_vars
    sess.uuid = "u1"
    return sess


async def test_detect_amd_uses_connect_override():
    # amd_test_result set via originate vars arrives as variable_* in connect data
    sess = bare_session({"variable_amd_test_result": "MACHINE"})
    assert await sess.detect_amd(amd_available=False) == "MACHINE"


async def test_detect_amd_without_module_is_human():
    sess = bare_session({})           # no override, mod_amd absent
    assert await sess.detect_amd(amd_available=False) == "HUMAN"


async def test_detect_amd_override_beats_missing_module():
    sess = bare_session({"variable_amd_test_result": "notsure"})
    assert await sess.detect_amd(amd_available=False) == "NOTSURE"
