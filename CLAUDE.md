# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project line

This repository is Dialer 2.0 (FreeSWITCH + ESL) only; the `dialer-v2` branch
was merged into `main` (PR #1), so `main` is the current line. Dialer v1
(PJSIP, sequential dialing) is a separate legacy project outside this
repository — PJSIP/pjsua2/pyVoIP instructions do NOT apply here, and the v1
project must not be modified from here. "Inherited from v1" in comments
refers to patterns carried over from it (tts/db/main/ansible).

## What this is

A commercial-grade autodialer: web UI (Basic Auth) → a campaign dials a list
of numbers with a synthesized message (Supertonic TTS, Ukrainian by default),
IVR menus of arbitrary nesting (up to 4 levels) reacting to DTMF, transfer to
a live SIP operator, AMD (answering-machine detection, voicemail drop), a
library of saved scenarios, campaign history with resume/retry-failed. State
lives in SQLite on a mounted volume. One campaign at a time, but up to
`max_concurrent` (1..5) calls in flight.

User-facing documentation lives in [docs/](docs/) (architecture,
configuration, API, deployment, operations, security, testing, development).
When a change alters behavior, config, or the API, update the matching
docs/ page in the same change.

## Commands

```bash
cp .env.example .env        # WEB_PASSWORD, ESL_PASSWORD, SIP_* (FlySIP)
docker compose up --build   # production mode: Linux host with a public IP

docker compose run --rm app pytest -q              # unit tests, no FreeSWITCH
docker compose run --rm app pytest tests/test_flow.py -q   # one file
docker compose run --rm app pytest -k announce -q           # by pattern

docker compose exec freeswitch fs_cli -p "$ESL_PASSWORD" -x "status"        # health
docker compose exec freeswitch fs_cli -p "$ESL_PASSWORD" -x "sofia status"  # trunks
```

UI: http://localhost:8000. E2E without a trunk: set
`DIAL_STRING_TEMPLATE=loopback/{number}/default` in `.env`, run a campaign to
number 9999; digits are simulated with `uuid_recv_dtmf <a-leg-uuid> <digit>`,
the AMD verdict with the `amd_test_result` originate variable.

Real AMD is optional: the base FreeSWITCH image lacks `mod_amd` (without it
every answer = HUMAN, the call is never dropped — graceful degradation).
Build: `docker build -f Dockerfile.freeswitch -t caller-freeswitch:amd .` +
`FREESWITCH_IMAGE=caller-freeswitch:amd` in `.env`.

Deploy ([ansible/](ansible/), host in `inventory.ini`):

| What changed | Playbook |
|---|---|
| Python code in `app/` | `redeploy-app.yml` (~10-30 s, FreeSWITCH stays up) |
| `fs/` config | `reload-fs.yml` (reloadxml + rescan, no container restart) |
| `fs/modules.conf` or sofia profile params | `reload-fs.yml -e hard=1` (FS restart — `rescan` does NOT apply profile params) |
| Everything / requirements / first time | `deploy.yml` |

`call.yml` POSTs `/start` (Basic Auth creds are read from `/opt/caller/.env`),
`status.yml -e wait=1` waits for completion.

## Architecture

Two containers (`network_mode: host` on Linux): **freeswitch** (the engine;
config is static XML from [fs/](fs/), mounted read-only; credentials are
injected by `fs/vars.xml` from env) and **app** (FastAPI controller + static
UI). The controller talks to FreeSWITCH over two ESL channels:

- **inbound** ([app/esl.py](app/esl.py)) — hand-rolled asyncio client to :8021
  (greenswitch was rejected: gevent is incompatible with the asyncio loop).
  `originate`, health checks, events; one shared connection, lazy,
  re-established on drop.
- **outbound socket** ([app/ivr.py](app/ivr.py)) — server on 127.0.0.1:8084.
  Every answered call is originated with `&socket(... async full)`, FS opens a
  TCP connection here, and `OutboundSession` drives the call (playback,
  `play_and_get_digits`, bridge, AMD).

Campaign lifecycle: `POST /start` ([app/main.py](app/main.py)) → validation +
dry-run compilation of the IVR form → a `campaign` row with snapshots of the
compiled graph (`ivr_flow`) and the source form (`ivr_form`) → the worker
([app/jobs.py](app/jobs.py)) prerenders all prompts to WAV (cache keyed by
hash of text+voice+params; a campaign must not start half-mute), materializes
the SIP profile into a gateway ([app/gateways.py](app/gateways.py) →
`fs/sip_profiles/external/gw_profile_<id>.xml` + rescan), then keeps up to
`max_concurrent` calls in flight: `claim_next_pending` → originate → the
answer is picked up by an outbound session, which finds its
`ivr.CallContext` in `REGISTRY` by `origination_uuid` → `run_call`: AMD →
lead-in silence → graph interpretation. Each number's outcome is written to
SQLite immediately.

- **[app/flow.py](app/flow.py)** — compiler of the recursive IVR form (UI/API)
  into flat `play|menu|bridge|hangup` nodes + validator. Option actions:
  `operator|replay|menu|play|back|home|optout|hangup`. The old flat form
  (checkboxes, sent by `call.yml`) is converted by `_legacy_menu`. Pure
  functions — fully covered by pytest.
- **[app/db.py](app/db.py)** — SQLite (WAL, one connection + lock). Tables:
  `sip_profile`, `operator`, `scenario`, `campaign`, `campaign_number`.
  `scenario` stores the FORM (round-trips through the editor), not the graph —
  compilation happens at start; a campaign keeps a snapshot, so deleting a
  scenario does not corrupt history.
- **[app/operators.py](app/operators.py)** — operator extensions: XML in
  `fs/directory/default/` + `reloadxml`; availability is checked live with
  `sofia_contact` (not registration events). `OperatorPool` paces dialing in
  operator campaigns by the number of free registered operators.
- **[app/amd.py](app/amd.py)** — pure policy: MACHINE → an info campaign drops
  the message after the beep (`voicemail-left`), an operator campaign hangs up
  (`machine-hangup`); HUMAN/NOTSURE → normal flow (a doubtful call is never
  dropped).
- **[app/tts.py](app/tts.py)** — Supertonic (lazy model, synthesis serialized
  by `_gen_lock`) + resample to telephony format via `audioop`.
- UI — [app/static/index.html](app/static/index.html) +
  [app/static/app.js](app/static/app.js), tabs Кампанія / Сценарії /
  Налаштування / Історія; `/status` is polled every 1.5 s. The UI is
  bilingual (uk/en): Ukrainian is canonical in the markup and JS literals;
  the `EN` map in app.js translates the chrome, `trServer`/`trLog` map the
  Ukrainian server messages by pattern, and the toggle persists in
  `localStorage` (`lang`). The backend stays Ukrainian-only.

### Number statuses

`pending → ringing →` one of: `answered`, `transferred` (connected to an
operator), `missed-operator` (wanted an operator, never got bridged),
`optout`, `voicemail-left`, `machine-hangup`, `busy` (USER_BUSY), `no-answer`
(NO_ANSWER/ORIGINATOR_CANCEL/NO_USER_RESPONSE), `failed` (everything else).
Retry-failed takes `db.RETRYABLE`; **`optout` is NEVER redialed**. A `failed`
with CALL_REJECTED/403 on a registered trunk = the provider's side
(provisioning/Caller-ID — there is a `CALLER_ID_NUMBER` env), not an app bug.
A process restart marks the campaign `interrupted`; resume is explicit only
(it places real calls).

## Invariants (inherited from v1, verified through pain)

- **Python 3.11, do not bump**: `tts.py` uses `audioop`, removed in 3.13.
- **Audio: WAV 8000 Hz / mono / 16-bit** — IVR prompts and voicemail drop.
  `tts._verify` guarantees this — keep it.
- **Secrets**: SIP passwords (plaintext in the DB — a documented limitation,
  see docs/security.md) and operator passwords are never returned by the API (only
  `password_set`), never logged, never enter `/status`. `data/`, `.env`, the
  generated `fs/directory/default/*.xml` and
  `fs/sip_profiles/external/gw_*.xml` are secret and gitignored. Never commit
  them under any circumstances.
- **`network_mode: host`** for FreeSWITCH; real calls work only on Linux with
  a public IP.
- **event_socket listens on 127.0.0.1 only**, password from `.env`.
- **Ukrainian stays canonical**: spoken TTS content (`flow.DIGIT_WORDS`,
  `ANNOUNCE_TEMPLATES`, `DEFAULT_*_TEXT`) is ALWAYS Ukrainian — callees hear
  it regardless of the UI language. Server-side strings (API errors,
  `_log(...)` lines) stay Ukrainian; English happens only client-side via the
  `EN` map / `trServer` / `trLog` in app.js. A new UI string must be added to
  the `EN` map (and, for server messages, a pattern to `SERVER_RE`/`LOG_RE`),
  or English mode silently shows Ukrainian. Code comments/docstrings are
  English.

## Gotchas: FreeSWITCH config and Docker

- **`X-PRE-PROCESS` understands ONLY double quotes** around attributes: with
  single quotes the scanner silently yields empty values. Hence the `exec-set`
  shell commands in `fs/vars.xml` contain no inner quotes and no `&&` (the
  XML entity `&amp;` is not decoded either) — only `if/then/fi` and
  `${VAR:-default}`.
- **`fs_cli` inside the container** needs `-p "$ESL_PASSWORD"`.
- Call diagnostics: `mod_logfile` + `sip-trace=yes` on the external profile
  (without them the safarov image starts with `-nf` and there are NO call
  logs), plus `tcpdump` on the host — count RTP packets per direction.

## Gotchas: ESL / outbound session / DTMF

- **The command/reply to `sendmsg execute` arrives AFTER the application
  finishes** (together with CHANNEL_EXECUTE_COMPLETE), at least on this FS
  build. Hence the reply timeout in `OutboundSession.execute` equals the
  execution timeout: with the default 15 s, any prompt or PAGD round longer
  than 15 s killed the session with a TimeoutError → `failed/APP_TIMEOUT`.
- **Loopback channels emit no `DTMF` events** — menus read digits via
  `play_and_get_digits` (consumes the channel input queue; works on loopback
  and on real calls). The `self.dtmf` event queue is deliberately NOT
  consulted: a digit arrives both as an event and in the channel queue, and
  reading both sources counted one press twice.
- **The announcement goes INSIDE PAGD as its file** (not a separate playback)
  — a digit barges in mid-word and acts immediately; a separate playback is
  not interruptible.
- Simulating a digit in tests: `uuid_recv_dtmf <a-leg-uuid> <digit>`
  (`uuid_send_dtmf` instead sends the digit to the remote side — different
  thing). Digits queued BEFORE PAGD starts are eaten by playback — send when
  `play_and_get_digits` is already running on the a-leg (the `application`
  column in `show channels`).
- `api` commands on the outbound socket are limited; `module_exists mod_amd`
  is probed once over the inbound connection (`jobs.amd_available`).

## Gotchas: TTS (Supertonic)

- **Typographic punctuation kills synthesis** (`unsupported character`):
  U+02BC Ukrainian apostrophe «зʼєднати», guillemets, em dashes, … —
  `jobs.normalize_text` maps them to ASCII before synthesis (both in
  `/preview` and in prerender). Do not remove.
- **Speed above ~1.3 SILENTLY swallows words** (measured: 1.4 → 3 of 5
  sentences, 2.0 → 2), below 0.7 — ValueError. Do not widen
  `tts.SPEED_MIN/MAX` without re-measuring.
- `max_chunk_length=10` in synthesize: "one sentence — one chunk", otherwise
  `silence_duration` (the inter-sentence pause) has no effect.
- Digits in menu announcements are words (`flow.DIGIT_WORDS`): TTS reads them
  more reliably than "1"/"0".
- **Prompts carry NO baked-in lead-in** (between menu rounds it reads as dead
  air); the single initial "bring the phone to your ear" pause is played by
  the IVR itself (`silence_stream`, `ivr.LEAD_IN_MS`). A lead-in inside the
  WAV remains only on the ad-hoc single-call `&playback` path (`POST /call`).

## Gotchas: real calls (NAT/RTP/early media) — diagnosed painfully

The chain of failures behind "the call goes through but there is no audio";
all fixed — keep, do not roll back:

- **A UI profile ≠ a gateway**: without materialization (`app/gateways.py`)
  the call goes through the static `flysip` from `.env` (empty →
  NORMAL_TEMPORARY_FAILURE). `DIAL_STRING_TEMPLATE` must contain `{gw}`.
- **One-way silence #1 — STUN**: `ext-rtp-ip="auto"` pulls in STUN, which on a
  cloud host detected a foreign IP and put it into the SDP. The default in
  `fs/vars.xml` is `$${local_ip_v4}`; for real NAT set `EXTERNAL_IP` in `.env`.
- **One-way silence #2 — `a=sendonly` early media (FlySIP) + FS 1.10.12 bug**:
  FS latches smode=RECVONLY from the second 183 and silently drops all
  outbound RTP, and `a=sendrecv` in the 200 OK never resets smode. The cure is
  a **two-phase re-INVITE** (`jobs.media_reneg_after_answer`): #1 plain (the
  sendonly answer sets smode=SENDONLY — the write gate opens), #2 with a
  one-shot `origination_audio_mode=sendrecv` (the SDP contract is two-way
  again). The pause between them is `MEDIA_RENEG_PAUSE` (1 s, otherwise 491);
  the IVR's initial silence covers them. On healthy trunks both are no-ops.
  ONE re-INVITE does not cure it; `disable-hold` has no effect on this bug.
- **`ignore_early_media=true` is REQUIRED** — otherwise `&socket()` starts on
  Pre-Answer (183) and the IVR plays into the ringback before pickup.
- **Verifying success**: `tcpdump -ni any 'udp and host <trunk-media-ip> and
  greater 120'` — a symmetric flow (our src:port ↔ their dst:port). If RTP is
  symmetric and the callee hears nothing — it's account provisioning on the
  provider side, not our code.
- **E2E bridge on loopback**: to keep the bridge from failing with
  INCOMPATIBLE_DESTINATION — `ORIGINATE_EXTRA_VARS=absolute_codec_string=PCMA`
  + `BRIDGE_EXTRA_VARS=absolute_codec_string=PCMA` (empty in production).
  The test "operator" is a self-registered gateway + a dialplan stub
  (`fs/sip_profiles/external/test_*.xml`, `fs/dialplan/public/test_*.xml`,
  both gitignored).

## Definition of done

Verification levels: 1 — pytest; 2-3 — live containers + valid config
(`fs_cli status` / `sofia status`); 4 — loopback E2E (DTMF/AMD simulated);
5 — **a real call/audio/AMD against live voicemail — done by a human with a
phone on a Linux host**. In reports explicitly separate "verified by me"
(1-4) from "needs verification by a human" (5). Never claim the unverified as
verified.
