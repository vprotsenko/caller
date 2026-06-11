"""SQLite layer: counters, retry set, optout protection, interrupted marking
(§16 level 1). Runs on a throwaway DB file per test."""

import pytest

from app import db


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    db.close()
    monkeypatch.setattr(db, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    yield
    db.close()


FLOW = {"start": "msg", "nodes": {"msg": {"type": "play", "prompt": "main", "next": "bye"},
                                  "bye": {"type": "hangup"}},
        "prompts": {"main": {"text": "Привіт", "voice": "F3"}}}


def make_campaign(numbers=("+380670000001", "+380670000002", "+380670000003")):
    pid = db.create_profile("test", "sip.example", 5060, "user", "pw", True)
    return db.create_campaign("Тест", "info", "Привіт", "F3", FLOW,
                              pid, "user@sip.example", 1, list(numbers))


def test_seed_profile_only_on_fresh_db():
    db.init(seed_profile={"name": "default", "server": "s1", "port": 5060,
                          "username": "u", "password": "p"})
    db.init(seed_profile={"name": "other", "server": "s2", "port": 5060,
                          "username": "u2", "password": "p2"})
    profiles = db.list_profiles()
    assert len(profiles) == 1
    assert profiles[0]["server"] == "s1"


def test_public_profile_never_leaks_password():
    db.create_profile("p", "srv", 5060, "u", "supersecret", True)
    for profile in db.list_profiles():
        assert "password" not in profile
        assert profile["password_set"] is True
    assert "supersecret" not in str(db.list_profiles())


def test_counts_group_by():
    cid = make_campaign()
    rows = db.campaign_numbers(cid)
    db.set_number_status(rows[0]["id"], "answered", hangup_cause="NORMAL_CLEARING")
    db.set_number_status(rows[1]["id"], "busy", hangup_cause="USER_BUSY")
    c = db.counts(cid)
    assert c["total"] == 3
    assert c["answered"] == 1
    assert c["busy"] == 1
    assert c["pending"] == 1
    assert c["done"] == 2


def test_claim_next_pending_marks_ringing_in_order():
    cid = make_campaign()
    first = db.claim_next_pending(cid)
    second = db.claim_next_pending(cid)
    assert first["seq"] == 0 and second["seq"] == 1
    assert db.get_number(first["id"])["status"] == "ringing"
    db.claim_next_pending(cid)
    assert db.claim_next_pending(cid) is None


def test_dtmf_append():
    cid = make_campaign()
    row = db.campaign_numbers(cid)[0]
    db.append_dtmf(row["id"], "1")
    db.append_dtmf(row["id"], "20")
    db.append_dtmf(row["id"], "")  # no-op
    assert db.get_number(row["id"])["dtmf"] == "120"


def test_retryable_excludes_optout_and_answered():
    assert "optout" not in db.RETRYABLE
    assert "answered" not in db.RETRYABLE
    assert "transferred" not in db.RETRYABLE
    for s in ("failed", "busy", "no-answer"):
        assert s in db.RETRYABLE


def test_mark_interrupted_on_startup_resets_ringing():
    cid = make_campaign()
    row = db.claim_next_pending(cid)
    assert db.get_campaign(cid)["status"] == "running"
    db.mark_interrupted_on_startup()
    assert db.get_campaign(cid)["status"] == "interrupted"
    assert db.get_number(row["id"])["status"] == "pending"


def test_campaign_flow_roundtrip():
    cid = make_campaign()
    assert db.campaign_flow(cid) == FLOW


def test_active_campaign_id():
    cid = make_campaign()
    assert db.active_campaign_id() == cid
    db.set_campaign_status(cid, "done", finished=True)
    assert db.active_campaign_id() is None
    assert db.latest_campaign_id() == cid
