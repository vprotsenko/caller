"""Trunk gateway generation from DB profiles (verification level 1)."""

import os

import pytest

from app import gateways


@pytest.fixture(autouse=True)
def conf_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(gateways, "FS_CONF_DIR", str(tmp_path))
    yield


def make_profile(**kw):
    base = {"id": 2, "server": "sip.flysip.example", "port": 5060,
            "username": "u100", "password": "secret9"}
    base.update(kw)
    return base


def test_gateway_name():
    assert gateways.gateway_name(2) == "gw_profile_2"
    assert gateways.gateway_name("3") == "gw_profile_3"


def test_gateway_xml_shape():
    xml = gateways.gateway_xml("gw_profile_2", "sip.x.com", 5070, "u1", "pw1")
    assert '<gateway name="gw_profile_2">' in xml
    assert 'value="sip.x.com:5070"' in xml          # proxy
    assert 'value="u1"' in xml                        # username
    assert 'value="pw1"' in xml                       # password
    assert 'name="register" value="true"' in xml


def test_gateway_xml_escapes_password():
    xml = gateways.gateway_xml("gw_profile_2", "h", 5060, "u", 'a"b<c>&d')
    assert 'a"b<c>&d' not in xml
    assert "&quot;" in xml and "&lt;" in xml and "&amp;" in xml


def test_write_and_remove_gateway():
    name = gateways.write_gateway(make_profile())
    assert name == "gw_profile_2"
    path = gateways.gateway_path(name)
    with open(path) as f:
        assert "sip.flysip.example:5060" in f.read()
    gateways.remove_gateway(2)
    assert not os.path.exists(path)
    gateways.remove_gateway(2)  # idempotent


class FakeClient:
    """ESL double: scripted gateway state transitions."""

    def __init__(self, states):
        self.states = list(states)  # consumed per `sofia status gateway`
        self.calls = []

    async def api(self, cmd, timeout=None):
        self.calls.append(cmd)
        if cmd.startswith("sofia status gateway"):
            state = self.states.pop(0) if self.states else "NOREG"
            return f"Name\tgw\nState\t{state}\n"
        return "+OK\n"


async def test_ensure_gateway_registers():
    client = FakeClient(states=["TRYING", "REGED"])
    name, state = await gateways.ensure_gateway(client, make_profile(), wait_registered=5)
    assert name == "gw_profile_2"
    assert state == "REGED"
    assert "reloadxml" in client.calls
    assert "sofia profile external rescan" in client.calls


async def test_ensure_gateway_noreg_does_not_raise():
    # IP-authenticated trunk never registers — must still return, not raise
    client = FakeClient(states=["NOREG", "NOREG", "NOREG"])
    name, state = await gateways.ensure_gateway(client, make_profile(), wait_registered=2)
    assert name == "gw_profile_2"
    assert state in ("NOREG", "")


async def test_gateway_state_parsing():
    client = FakeClient(states=["REGED"])
    assert await gateways.gateway_state(client, "gw_profile_2") == "REGED"
