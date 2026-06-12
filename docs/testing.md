# Testing and verification

Changes are verified on a five-level ladder. Levels 1–4 are automatable;
level 5 requires a human with a phone. When reporting verification results,
always separate what was verified at levels 1–4 from what still needs a
level-5 check — never report the unverified as verified.

| Level | What it proves | How |
|---|---|---|
| 1 | Pure logic (flow compiler, number normalization, cause mapping, AMD policy) | `pytest`, no FreeSWITCH |
| 2–3 | Containers run, config is valid | `fs_cli status` / `sofia status` |
| 4 | Full call flow end-to-end (IVR, DTMF, AMD policy, bridge) | Loopback E2E without a trunk |
| 5 | Real audio, real AMD against live voicemail | A real call by a human, Linux host with a public IP |

## Level 1 — unit tests

```bash
docker compose run --rm app pytest -q                       # all
docker compose run --rm app pytest tests/test_flow.py -q    # one file
docker compose run --rm app pytest -k announce -q           # by pattern
```

## Levels 2–3 — live containers and config

```bash
docker compose exec freeswitch fs_cli -p "$ESL_PASSWORD" -x "status"
docker compose exec freeswitch fs_cli -p "$ESL_PASSWORD" -x "sofia status"
```

## Level 4 — loopback E2E (no trunk)

Set in `.env`:

```
DIAL_STRING_TEMPLATE=loopback/{number}/default
```

then start a campaign from the UI to number `9999`.

- **Simulate a DTMF digit**: `uuid_recv_dtmf <a-leg-uuid> <digit>` from
  `fs_cli`. (`uuid_send_dtmf` is the wrong tool — it sends the digit to the
  remote side.) Send the digit while `play_and_get_digits` is already running
  on the a-leg (check the `application` column in `show channels`) — digits
  queued before it starts are consumed by playback.
- **Simulate an AMD verdict**: the `amd_test_result` originate variable
  (e.g. via `ORIGINATE_EXTRA_VARS=amd_test_result=MACHINE`).
- **Bridge to a test operator**: loopback legs need a fixed codec or the
  bridge fails with `INCOMPATIBLE_DESTINATION`. Set
  `ORIGINATE_EXTRA_VARS=absolute_codec_string=PCMA` and
  `BRIDGE_EXTRA_VARS=absolute_codec_string=PCMA` (keep both **empty in
  production**). The test "operator" is a self-registered gateway plus a
  dialplan stub (`fs/sip_profiles/external/test_*.xml`,
  `fs/dialplan/public/test_*.xml`, both gitignored).

Note: loopback channels emit no `DTMF` events — the IVR reads digits via
`play_and_get_digits` (the channel input queue), which works both on loopback
and on real calls.

## Level 5 — real call

A human with a phone, against a deployment on a Linux host with a public IP:

1. Start a small campaign at a real number; verify two-way audio, prompt
   timing and DTMF reactions.
2. Let it hit a real voicemail to verify the AMD verdict and the
   voicemail-drop behavior (requires the `mod_amd` image — see
   [deployment.md](deployment.md)).
3. Verify RTP symmetry if anything sounds wrong:
   `tcpdump -ni any 'udp and host <trunk-media-ip> and greater 120'` (see
   [operations.md](operations.md)).

`ansible/call.yml` and `ansible/status.yml` script the test call and the wait
for completion — see [ansible/README.md](../ansible/README.md).
