"""Operator directory generation + pool logic (verification level 1)."""

import pytest

from app import db, operators


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    db.close()
    monkeypatch.setattr(db, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(operators, "FS_CONF_DIR", str(tmp_path / "fs"))
    operators._domain_cache = None
    yield
    db.close()


# --- directory XML ---------------------------------------------------------------

def test_extension_xml_shape():
    xml = operators.extension_xml("1001", "secret123")
    assert '<user id="1001">' in xml
    assert 'value="secret123"' in xml
    assert 'user_context' in xml


def test_extension_xml_escapes_password():
    xml = operators.extension_xml("1001", 'a"b<c>&d')
    assert 'a"b<c>&d' not in xml          # raw injection impossible
    assert "&quot;" in xml and "&lt;" in xml and "&amp;" in xml


def test_write_and_remove_extension(tmp_path):
    operators.write_extension("1002", "pw123456")
    path = operators.extension_path("1002")
    with open(path) as f:
        assert '<user id="1002">' in f.read()
    operators.remove_extension("1002")
    import os
    assert not os.path.exists(path)
    operators.remove_extension("1002")  # idempotent


@pytest.mark.parametrize("ext,ok", [
    ("1001", True), ("999", True), ("123456", True),
    ("", False), ("12", False), ("1234567", False),
    ("10a1", False), ("1001; rm", False),
])
def test_extension_validation(ext, ok):
    assert bool(operators.EXTENSION_RE.match(ext)) is ok


# --- pool -------------------------------------------------------------------------

class FakeClient:
    """ESL double: scripted registration responses per extension."""

    def __init__(self, registered=()):
        self.registered = set(registered)

    async def api(self, cmd, timeout=None):
        if cmd.startswith("global_getvar domain"):
            return "10.0.0.1\n"
        if cmd.startswith("sofia_contact"):
            ext = cmd.split()[-1].split("@")[0]
            if ext in self.registered:
                return f"sofia/internal/sip:{ext}@10.0.0.5:5060\n"
            return "error/user_not_registered\n"
        return "+OK\n"


async def test_pool_acquires_first_free_registered():
    db.create_operator("Іван", "1001", "pw")
    db.create_operator("Олена", "1002", "pw")
    pool = operators.OperatorPool(FakeClient(registered={"1002"}))
    ext = await pool.acquire()
    assert ext == "1002"            # 1001 not registered -> skipped
    assert await pool.acquire() is None  # 1002 now busy
    pool.release("1002")
    assert await pool.acquire() == "1002"


async def test_pool_skips_disabled_operators():
    op_id = db.create_operator("Іван", "1001", "pw")
    # disable directly in the DB
    with db._lock:
        db._connect().execute("UPDATE operator SET enabled=0 WHERE id=?", (op_id,))
        db._connect().commit()
    pool = operators.OperatorPool(FakeClient(registered={"1001"}))
    assert await pool.acquire() is None


async def test_pool_dial_target_and_free_count():
    db.create_operator("Іван", "1001", "pw")
    db.create_operator("Олена", "1002", "pw")
    pool = operators.OperatorPool(FakeClient(registered={"1001", "1002"}))
    assert await pool.free_count() == 2
    ext = await pool.acquire()
    assert pool.dial_target(ext) == f"user/{ext}@10.0.0.1"
    assert await pool.free_count() == 1
    assert pool.busy_extensions() == {ext}


async def test_is_registered_parsing():
    client = FakeClient(registered={"1001"})
    assert await operators.is_registered(client, "1001") is True
    operators._domain_cache = None
    assert await operators.is_registered(FakeClient(), "1001") is False
