"""AMD decision policy (verification level 1)."""

import pytest

from app import amd


@pytest.mark.parametrize("raw,expected", [
    ("MACHINE", amd.MACHINE),
    ("machine", amd.MACHINE),
    ("  Machine ", amd.MACHINE),
    ("NOTSURE", amd.NOTSURE),
    ("HUMAN", amd.HUMAN),
    ("", amd.HUMAN),        # mod_amd absent / no verdict -> never drop
    (None, amd.HUMAN),
    ("garbage", amd.HUMAN),
])
def test_normalize_verdict(raw, expected):
    assert amd.normalize_verdict(raw) == expected


@pytest.mark.parametrize("verdict,ctype,expected", [
    # machine: branch on campaign type
    ("MACHINE", "info", amd.VOICEMAIL),
    ("MACHINE", "operator", amd.MACHINE_HANGUP),
    # human / notsure: always continue, regardless of campaign type
    ("HUMAN", "info", amd.CONTINUE),
    ("HUMAN", "operator", amd.CONTINUE),
    ("NOTSURE", "info", amd.CONTINUE),       # doubtful -> not dropped
    ("NOTSURE", "operator", amd.CONTINUE),
    ("", "info", amd.CONTINUE),
])
def test_decide(verdict, ctype, expected):
    assert amd.decide(verdict, ctype) == expected


def test_status_mapping():
    assert amd.STATUS[amd.VOICEMAIL] == "voicemail-left"
    assert amd.STATUS[amd.MACHINE_HANGUP] == "machine-hangup"
    assert amd.CONTINUE not in amd.STATUS
