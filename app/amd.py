"""Answering-machine detection policy (Plan.md §6, stage 4).

Pure decision logic, separated from FreeSWITCH so it is unit-tested without a
live engine (§16 level 1). The actual classification runs in mod_amd (compiled
into the freeswitch image via Dockerfile.freeswitch); this module only maps its
verdict + the campaign type to the next action.

Verdicts (mod_amd `amd_result` channel variable):
  HUMAN    — a person answered
  MACHINE  — an answering machine / voicemail
  NOTSURE  — inconclusive (treated as human: we never drop a doubtful call, §6)
  (anything else / mod_amd absent -> treated as HUMAN)
"""

HUMAN = "HUMAN"
MACHINE = "MACHINE"
NOTSURE = "NOTSURE"

# next action after AMD
CONTINUE = "continue"        # run the IVR flow as normal
VOICEMAIL = "voicemail"      # info campaign: drop the message after the beep
MACHINE_HANGUP = "machine_hangup"  # operator campaign: hang up on a machine

# action -> terminal status when the action itself is terminal (§4)
STATUS = {VOICEMAIL: "voicemail-left", MACHINE_HANGUP: "machine-hangup"}


def normalize_verdict(raw):
    """Map a raw amd_result value to HUMAN/MACHINE/NOTSURE (default HUMAN)."""
    v = (raw or "").strip().upper()
    if v == MACHINE:
        return MACHINE
    if v == NOTSURE:
        return NOTSURE
    return HUMAN  # HUMAN, empty, unknown, or mod_amd absent -> never drop


def decide(verdict, campaign_type):
    """What to do after AMD for a given campaign type (§6).

    MACHINE:  info -> drop voicemail; operator -> hang up.
    HUMAN/NOTSURE: always continue (doubtful calls are not dropped).
    """
    verdict = normalize_verdict(verdict)
    if verdict != MACHINE:
        return CONTINUE
    return VOICEMAIL if campaign_type == "info" else MACHINE_HANGUP
