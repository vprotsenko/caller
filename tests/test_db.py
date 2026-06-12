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


# --- scenarios (бібліотека збережених варіантів кампаній) --------------------------

IVR_FORM = {"timeout_sec": 5, "max_repeats": 2,
            "menu": {"options": [{"digit": "1", "action": "operator"}]}}
VP = {"speed": 1.1, "steps": 8, "silence": 0.3}


def make_scenario(name="Акція"):
    return db.create_scenario(name, "info", "Привіт", "F3", VP, IVR_FORM)


def test_scenario_roundtrip():
    sid = make_scenario()
    s = db.get_scenario(sid)
    assert s["name"] == "Акція"
    assert s["message"] == "Привіт"
    assert s["voice_params"] == VP
    assert s["ivr"] == IVR_FORM          # форма (§15), не скомпільований граф


def test_scenario_list_sorted_and_full():
    make_scenario("Б")
    make_scenario("А")
    names = [s["name"] for s in db.list_scenarios()]
    assert names == ["А", "Б"]
    assert all("ivr" in s for s in db.list_scenarios())  # дайджест UI рахує з ivr


def test_scenario_unique_name():
    make_scenario("Те саме")
    import pytest as _pytest
    with _pytest.raises(Exception):
        make_scenario("Те саме")


def test_scenario_update_and_delete():
    sid = make_scenario()
    db.update_scenario(sid, "Нова назва", "operator", "Текст 2", "M1",
                       {"speed": 0.9}, {"menu": {"options": []}})
    s = db.get_scenario(sid)
    assert (s["name"], s["campaign_type"], s["voice"]) == ("Нова назва", "operator", "M1")
    db.delete_scenario(sid)
    assert db.get_scenario(sid) is None


def test_campaign_keeps_scenario_snapshot():
    """Назва сценарію в кампанії — знімок: переживає видалення сценарію."""
    sid = make_scenario()
    pid = db.create_profile("t2", "sip.example", 5060, "u", "pw", True)
    cid = db.create_campaign("К", "info", "Привіт", "F3", FLOW, pid, "u@s", 1,
                             ["+380670000001"], scenario_id=sid,
                             scenario_name="Акція", ivr_form=IVR_FORM)
    db.delete_scenario(sid)
    c = db.get_campaign(cid)
    assert c["scenario_name"] == "Акція"
    import json as _json
    assert _json.loads(c["ivr_form"]) == IVR_FORM
    assert any(x["scenario_name"] == "Акція" for x in db.list_campaigns())


def test_campaign_migration_adds_columns(tmp_path):
    """Стара БД без scenario-колонок мігрує на льоту (ALTER TABLE)."""
    import sqlite3
    db.close()
    old = sqlite3.connect(db.DB_PATH)
    old.execute("""CREATE TABLE campaign (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
        status TEXT NOT NULL, campaign_type TEXT NOT NULL DEFAULT 'info',
        message_text TEXT NOT NULL, voice TEXT NOT NULL, ivr_flow TEXT NOT NULL,
        profile_id INTEGER, profile_label TEXT,
        max_concurrent INTEGER NOT NULL DEFAULT 1, error TEXT,
        created_at REAL NOT NULL, started_at REAL, finished_at REAL)""")
    old.execute("INSERT INTO campaign (name, status, campaign_type, message_text,"
                " voice, ivr_flow, created_at) VALUES ('стара','done','info','м','F3','{}',1.0)")
    old.commit()
    old.close()
    c = db.get_campaign(1)  # _connect() → _migrate()
    assert c["name"] == "стара"
    assert c["scenario_id"] is None and c["scenario_name"] is None
