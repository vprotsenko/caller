"""SQLite persistence: SIP profiles, operators, campaigns,
per-number outcomes.

Patterns inherited from v1: one file on a mounted volume (DB_DIR), one shared
connection guarded by a lock (one campaign at a time + 1.5 s UI polling =
trivial contention), WAL mode, counters as a GROUP BY over campaign_number.

SECURITY: a SIP profile stores its password in PLAINTEXT here (deliberate POC
trade-off — no TLS either). It is never returned to the browser
(`public_profile` drops it; the API reports only `password_set`) and never
logged. Operator SIP passwords (stage 3) get the same treatment. Keep it that
way; the data/ volume must stay as protected as .env.
"""

import json
import logging
import os
import sqlite3
import threading
import time

logger = logging.getLogger(__name__)

DB_DIR = os.environ.get("DB_DIR", "/app/data")
DB_PATH = os.path.join(DB_DIR, "caller.db")

# campaign.status values
RUNNING = "running"
CAMPAIGN_STATUSES = ("running", "done", "interrupted", "stopped")

# campaign_number.status values
NUMBER_STATUSES = (
    "pending", "ringing", "answered", "transferred", "voicemail-left",
    "machine-hangup", "no-answer", "busy", "failed", "optout",
    "missed-operator",
)
# eligible for retry-failed; optout is NEVER retried
RETRYABLE = ("failed", "busy", "no-answer", "machine-hangup", "missed-operator")
# still in flight — neither pending nor terminal
ACTIVE_STATUSES = ("ringing",)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sip_profile (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    server      TEXT NOT NULL,
    port        INTEGER NOT NULL DEFAULT 5060,
    username    TEXT NOT NULL,
    password    TEXT NOT NULL DEFAULT '',   -- plaintext; never sent to client, never logged
    is_default  INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS operator (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    extension   TEXT NOT NULL UNIQUE,       -- 1001..1009 on FreeSWITCH
    password    TEXT NOT NULL DEFAULT '',   -- SIP password for the softphone (stage 3)
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS scenario (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    campaign_type TEXT NOT NULL DEFAULT 'info',
    message_text  TEXT NOT NULL,
    voice         TEXT NOT NULL,
    voice_params  TEXT NOT NULL DEFAULT '{}',  -- JSON {speed, steps, silence}
    ivr_form      TEXT NOT NULL DEFAULT '{}',  -- JSON of the RECURSIVE FORM, not the compiled graph:
                                               -- the editor round-trips it; compilation happens at start
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS campaign (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    status         TEXT NOT NULL,           -- running|done|interrupted|stopped
    campaign_type  TEXT NOT NULL DEFAULT 'info',  -- info|operator (AMD branch)
    message_text   TEXT NOT NULL,
    voice          TEXT NOT NULL,
    ivr_flow       TEXT NOT NULL,           -- flow JSON snapshot of the scenario
    profile_id     INTEGER REFERENCES sip_profile(id) ON DELETE SET NULL,
    profile_label  TEXT,                    -- for history display if the profile is gone
    max_concurrent INTEGER NOT NULL DEFAULT 1,
    error          TEXT,                    -- why the campaign stopped, if abnormal
    created_at     REAL NOT NULL,
    started_at     REAL,
    finished_at    REAL,
    scenario_id    INTEGER,                 -- which scenario it was started from (may be NULL)
    scenario_name  TEXT,                    -- name snapshot: survives scenario deletion
    ivr_form       TEXT                     -- snapshot of the source form for editing from history
);

CREATE TABLE IF NOT EXISTS campaign_number (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id  INTEGER NOT NULL REFERENCES campaign(id) ON DELETE CASCADE,
    seq          INTEGER NOT NULL,          -- dial order within the campaign
    number       TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    hangup_cause TEXT,                      -- raw Q.850/SIP cause for diagnostics
    amd_result   TEXT,                      -- HUMAN|MACHINE|NOTSURE|NULL (stage 4)
    dtmf         TEXT,                      -- digits pressed in the IVR
    attempts     INTEGER NOT NULL DEFAULT 0,
    updated_at   REAL
);
CREATE INDEX IF NOT EXISTS idx_cn_campaign ON campaign_number(campaign_id, seq);
"""

_conn = None
_lock = threading.RLock()


def _connect():
    global _conn
    if _conn is None:
        os.makedirs(DB_DIR, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
        _conn.executescript(_SCHEMA)
        _migrate(_conn)
        _conn.commit()
        logger.info("SQLite ready at %s", DB_PATH)
    return _conn


def _migrate(conn):
    """Lightweight in-place migrations: CREATE TABLE IF NOT EXISTS covers new
    tables, this covers columns added to existing ones on old DBs."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(campaign)")}
    for col, ddl in (
        ("scenario_id", "ALTER TABLE campaign ADD COLUMN scenario_id INTEGER"),
        ("scenario_name", "ALTER TABLE campaign ADD COLUMN scenario_name TEXT"),
        ("ivr_form", "ALTER TABLE campaign ADD COLUMN ivr_form TEXT"),
    ):
        if col not in cols:
            conn.execute(ddl)


def init(seed_profile=None):
    """Create the schema and, on a fresh DB, seed one profile from the .env
    SIP defaults (SIP_* matter only here, as in v1)."""
    with _lock:
        conn = _connect()
        if seed_profile and seed_profile.get("server"):
            has_any = conn.execute("SELECT 1 FROM sip_profile LIMIT 1").fetchone()
            if not has_any:
                create_profile(
                    name=seed_profile.get("name") or "default",
                    server=seed_profile["server"],
                    port=int(seed_profile.get("port") or 5060),
                    username=seed_profile.get("username", ""),
                    password=seed_profile.get("password", ""),
                    is_default=True,
                )
                logger.info("Seeded default SIP profile from environment")


def close():
    """Close the shared connection (tests use this to reopen on a new path)."""
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


# --- SIP profiles ------------------------------------------------------------

def public_profile(row):
    """Profile dict safe to send to the browser: password replaced by a flag."""
    return {
        "id": row["id"],
        "name": row["name"],
        "server": row["server"],
        "port": row["port"],
        "username": row["username"],
        "password_set": bool(row["password"]),
        "is_default": bool(row["is_default"]),
    }


def list_profiles():
    with _lock:
        rows = _connect().execute(
            "SELECT * FROM sip_profile ORDER BY is_default DESC, name"
        ).fetchall()
    return [public_profile(r) for r in rows]


def get_profile(profile_id):
    """Full row INCLUDING the password — for the worker only, never serialized."""
    if profile_id is None:
        return None
    with _lock:
        return _connect().execute(
            "SELECT * FROM sip_profile WHERE id=?", (profile_id,)
        ).fetchone()


def default_profile_id():
    with _lock:
        row = _connect().execute(
            "SELECT id FROM sip_profile ORDER BY is_default DESC, id LIMIT 1"
        ).fetchone()
    return row["id"] if row else None


def create_profile(name, server, port, username, password, is_default=False):
    with _lock:
        conn = _connect()
        if is_default:
            conn.execute("UPDATE sip_profile SET is_default=0")
        cur = conn.execute(
            "INSERT INTO sip_profile (name, server, port, username, password, is_default, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (name, server, int(port), username, password, 1 if is_default else 0, time.time()),
        )
        conn.commit()
        return cur.lastrowid


def update_profile(profile_id, name, server, port, username, password=None, is_default=False):
    """Update a profile. A blank/None password keeps the stored one unchanged."""
    with _lock:
        conn = _connect()
        if is_default:
            conn.execute("UPDATE sip_profile SET is_default=0")
        if password:
            conn.execute(
                "UPDATE sip_profile SET name=?, server=?, port=?, username=?, password=?, is_default=? WHERE id=?",
                (name, server, int(port), username, password, 1 if is_default else 0, profile_id),
            )
        else:
            conn.execute(
                "UPDATE sip_profile SET name=?, server=?, port=?, username=?, is_default=? WHERE id=?",
                (name, server, int(port), username, 1 if is_default else 0, profile_id),
            )
        conn.commit()


def delete_profile(profile_id):
    with _lock:
        conn = _connect()
        conn.execute("DELETE FROM sip_profile WHERE id=?", (profile_id,))
        conn.commit()


# --- Operators (bridge targets, stage 3) --------------------------------------

def public_operator(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "extension": row["extension"],
        "enabled": bool(row["enabled"]),
    }


def list_operators(enabled_only=False):
    q = "SELECT * FROM operator" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY extension"
    with _lock:
        rows = _connect().execute(q).fetchall()
    return [public_operator(r) for r in rows]


def get_operator(operator_id):
    with _lock:
        return _connect().execute(
            "SELECT * FROM operator WHERE id=?", (operator_id,)).fetchone()


def create_operator(name, extension, password, enabled=True):
    with _lock:
        conn = _connect()
        cur = conn.execute(
            "INSERT INTO operator (name, extension, password, enabled, created_at) VALUES (?,?,?,?,?)",
            (name, extension, password, 1 if enabled else 0, time.time()),
        )
        conn.commit()
        return cur.lastrowid


def delete_operator(operator_id):
    with _lock:
        conn = _connect()
        conn.execute("DELETE FROM operator WHERE id=?", (operator_id,))
        conn.commit()


# --- Scenarios (saved campaign variants; the source for starting) ---------------

def scenario_dict(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "campaign_type": row["campaign_type"],
        "message": row["message_text"],
        "voice": row["voice"],
        "voice_params": json.loads(row["voice_params"] or "{}"),
        "ivr": json.loads(row["ivr_form"] or "{}"),
        "updated_at": row["updated_at"],
    }


def list_scenarios():
    """Full scenario dicts (they are small): the list and the start select are
    fed by a single query; the UI computes the menu digest itself from `ivr`."""
    with _lock:
        rows = _connect().execute("SELECT * FROM scenario ORDER BY name").fetchall()
    return [scenario_dict(r) for r in rows]


def get_scenario(scenario_id):
    if scenario_id is None:
        return None
    with _lock:
        row = _connect().execute(
            "SELECT * FROM scenario WHERE id=?", (scenario_id,)).fetchone()
    return scenario_dict(row) if row else None


def create_scenario(name, campaign_type, message_text, voice, voice_params, ivr_form):
    with _lock:
        conn = _connect()
        now = time.time()
        cur = conn.execute(
            "INSERT INTO scenario (name, campaign_type, message_text, voice,"
            " voice_params, ivr_form, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (name, campaign_type, message_text, voice,
             json.dumps(voice_params, ensure_ascii=False),
             json.dumps(ivr_form, ensure_ascii=False), now, now),
        )
        conn.commit()
        return cur.lastrowid


def update_scenario(scenario_id, name, campaign_type, message_text, voice,
                    voice_params, ivr_form):
    with _lock:
        conn = _connect()
        conn.execute(
            "UPDATE scenario SET name=?, campaign_type=?, message_text=?, voice=?,"
            " voice_params=?, ivr_form=?, updated_at=? WHERE id=?",
            (name, campaign_type, message_text, voice,
             json.dumps(voice_params, ensure_ascii=False),
             json.dumps(ivr_form, ensure_ascii=False), time.time(), scenario_id),
        )
        conn.commit()


def delete_scenario(scenario_id):
    with _lock:
        conn = _connect()
        conn.execute("DELETE FROM scenario WHERE id=?", (scenario_id,))
        conn.commit()


# --- Campaigns -----------------------------------------------------------------

def create_campaign(name, campaign_type, message_text, voice, ivr_flow,
                    profile_id, profile_label, max_concurrent, numbers,
                    scenario_id=None, scenario_name=None, ivr_form=None):
    """Insert the campaign + one row per number; status starts as 'running'."""
    with _lock:
        conn = _connect()
        cur = conn.execute(
            "INSERT INTO campaign (name, status, campaign_type, message_text, voice,"
            " ivr_flow, profile_id, profile_label, max_concurrent, created_at,"
            " scenario_id, scenario_name, ivr_form)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, RUNNING, campaign_type, message_text, voice,
             json.dumps(ivr_flow, ensure_ascii=False), profile_id, profile_label,
             int(max_concurrent), time.time(), scenario_id, scenario_name,
             json.dumps(ivr_form, ensure_ascii=False) if ivr_form is not None else None),
        )
        cid = cur.lastrowid
        conn.executemany(
            "INSERT INTO campaign_number (campaign_id, seq, number) VALUES (?,?,?)",
            [(cid, i, n) for i, n in enumerate(numbers)],
        )
        conn.commit()
        return cid


def get_campaign(campaign_id):
    with _lock:
        return _connect().execute(
            "SELECT * FROM campaign WHERE id=?", (campaign_id,)).fetchone()


def campaign_flow(campaign_id):
    row = get_campaign(campaign_id)
    return json.loads(row["ivr_flow"]) if row else None


def latest_campaign_id():
    with _lock:
        row = _connect().execute("SELECT id FROM campaign ORDER BY id DESC LIMIT 1").fetchone()
    return row["id"] if row else None


def active_campaign_id():
    with _lock:
        row = _connect().execute(
            "SELECT id FROM campaign WHERE status=? ORDER BY id DESC LIMIT 1", (RUNNING,)
        ).fetchone()
    return row["id"] if row else None


def set_campaign_status(campaign_id, status, error=None, started=False, finished=False):
    with _lock:
        conn = _connect()
        sets, args = ["status=?", "error=?"], [status, error]
        if started:
            sets.append("started_at=?")
            args.append(time.time())
        if finished:
            sets.append("finished_at=?")
            args.append(time.time())
        args.append(campaign_id)
        conn.execute(f"UPDATE campaign SET {', '.join(sets)} WHERE id=?", args)
        conn.commit()


def next_pending_number(campaign_id):
    """The next number still to dial, in order. None when nothing is pending."""
    with _lock:
        return _connect().execute(
            "SELECT * FROM campaign_number WHERE campaign_id=? AND status='pending'"
            " ORDER BY seq LIMIT 1",
            (campaign_id,),
        ).fetchone()


def claim_next_pending(campaign_id):
    """Atomically take the next pending number and mark it 'ringing'.

    The single worker is asyncio-based, but claim+mark in one critical section
    keeps this safe even with max_concurrent dial tasks."""
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT * FROM campaign_number WHERE campaign_id=? AND status='pending'"
            " ORDER BY seq LIMIT 1",
            (campaign_id,),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE campaign_number SET status='ringing', updated_at=? WHERE id=?",
            (time.time(), row["id"]),
        )
        conn.commit()
        return row


def set_number_status(number_id, status, hangup_cause=None, amd_result=None,
                      bump_attempt=False):
    with _lock:
        conn = _connect()
        sets = ["status=?", "updated_at=?"]
        args = [status, time.time()]
        if hangup_cause is not None:
            sets.append("hangup_cause=?")
            args.append(hangup_cause)
        if amd_result is not None:
            sets.append("amd_result=?")
            args.append(amd_result)
        if bump_attempt:
            sets.append("attempts=attempts+1")
        args.append(number_id)
        conn.execute(f"UPDATE campaign_number SET {', '.join(sets)} WHERE id=?", args)
        conn.commit()


def append_dtmf(number_id, digits):
    """Append pressed digit(s) to the number's dtmf column."""
    if not digits:
        return
    with _lock:
        conn = _connect()
        conn.execute(
            "UPDATE campaign_number SET dtmf=COALESCE(dtmf,'') || ?, updated_at=? WHERE id=?",
            (digits, time.time(), number_id),
        )
        conn.commit()


def get_number(number_id):
    with _lock:
        return _connect().execute(
            "SELECT * FROM campaign_number WHERE id=?", (number_id,)).fetchone()


def campaign_numbers(campaign_id):
    with _lock:
        rows = _connect().execute(
            "SELECT * FROM campaign_number WHERE campaign_id=? ORDER BY seq", (campaign_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def counts(campaign_id):
    """{total, done, <status>: n, ...} — a GROUP BY over campaign_number."""
    base = {s: 0 for s in NUMBER_STATUSES}
    with _lock:
        rows = _connect().execute(
            "SELECT status, COUNT(*) c FROM campaign_number WHERE campaign_id=? GROUP BY status",
            (campaign_id,),
        ).fetchall()
    for r in rows:
        base[r["status"]] = r["c"]
    total = sum(base.values())
    in_flight = base["pending"] + sum(base[s] for s in ACTIVE_STATUSES)
    return {"total": total, "done": total - in_flight, **base}


def list_campaigns(limit=50):
    with _lock:
        rows = _connect().execute(
            "SELECT id, name, status, campaign_type, voice, profile_id, profile_label,"
            " max_concurrent, error, created_at, started_at, finished_at,"
            " scenario_id, scenario_name"
            " FROM campaign ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["counts"] = counts(r["id"])
        out.append(item)
    return out


def reset_ringing_to_pending(campaign_id):
    """A 'ringing' number when the process died has an unknown outcome — re-dial it."""
    with _lock:
        conn = _connect()
        conn.execute(
            "UPDATE campaign_number SET status='pending' WHERE campaign_id=? AND status='ringing'",
            (campaign_id,),
        )
        conn.commit()


def mark_interrupted_on_startup():
    """Campaigns left 'running' by a crash/restart become 'interrupted'
    (NOT auto-resumed — they place real calls; resuming is an explicit user
    action, as in v1). Their in-flight numbers go back to pending."""
    with _lock:
        conn = _connect()
        rows = conn.execute("SELECT id FROM campaign WHERE status=?", (RUNNING,)).fetchall()
        for r in rows:
            conn.execute(
                "UPDATE campaign_number SET status='pending' WHERE campaign_id=? AND status='ringing'",
                (r["id"],),
            )
            conn.execute("UPDATE campaign SET status='interrupted' WHERE id=?", (r["id"],))
        conn.commit()
        if rows:
            logger.warning("Marked %d interrupted campaign(s) on startup", len(rows))
