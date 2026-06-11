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

    async def wait_digit(self, timeout):
        if not self.digits:
            return None
        digit = self.digits.pop(0)
        if digit is None:
            return None
        return digit

    async def bridge(self, dial_target, ring_timeout, max_seconds):
        self.bridge_targets.append(dial_target)
        self.bridged = self.bridge_answers
        return self.bridged

    async def hangup(self, cause="NORMAL_CLEARING"):
        self.hung_up = True


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


FILES = {"main": "/a/main.wav", "connecting": "/a/conn.wav", "optout_ok": "/a/opt.wav"}

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
    assert session.played == ["/a/main.wav"]
    assert session.hung_up
    assert outcome["dtmf"] == ""
    assert outcome["mark"] is None


async def test_press_2_replays_message():
    session = FakeSession(digits=["2"])
    outcome = await ivr.run_flow(session, make_flow(), FILES)
    assert session.played == ["/a/main.wav", "/a/main.wav"]  # replayed once
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
    session.die_after_plays = 1  # dies on the replay
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
