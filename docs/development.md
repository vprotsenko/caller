# Development guide

## Environment

Everything runs in containers; no host Python is needed:

```bash
cp .env.example .env
docker compose up --build                       # app + freeswitch
docker compose run --rm app pytest -q           # unit tests (no FreeSWITCH)
```

For full E2E without a SIP trunk use the loopback setup from
[testing.md](testing.md). Real calls require a Linux host with a public IP —
see [deployment.md](deployment.md).

## Project layout

```
app/            FastAPI controller, campaign worker, IVR engine, TTS
app/static/     Web UI (vanilla HTML/JS, bilingual uk/en)
fs/             FreeSWITCH configuration (static XML; credentials via vars.xml)
ansible/        Deploy and operations playbooks
tests/          pytest suite (pure logic, no FreeSWITCH required)
docs/           This documentation
data/           SQLite database (gitignored, holds credentials)
audio/          Synthesized prompt cache (shared volume)
```

The module-by-module map is in [architecture.md](architecture.md).

## Hard invariants

These look arbitrary until violated; each one is load-bearing.

- **Python 3.11, do not bump**: `tts.py` uses `audioop`, removed in 3.13.
- **Audio is WAV 8000 Hz / mono / 16-bit PCM** for IVR prompts and the
  voicemail drop. `tts._verify` guarantees it — keep the check.
- **Credential containment**: SIP and operator passwords are never returned
  by the API (only `password_set`), never logged, never enter `/status`.
  See [security.md](security.md).
- **`network_mode: host`** for both containers; the Event Socket listens on
  127.0.0.1 only.
- **One campaign at a time**; `max_concurrent` is capped at 5.

## Language conventions

- **Code comments and docstrings: English.** Documentation: English.
- **Spoken TTS content is ALWAYS Ukrainian** (`flow.DIGIT_WORDS`,
  `ANNOUNCE_TEMPLATES`, `DEFAULT_*_TEXT`) — callees hear it regardless of the
  UI language.
- **Server-side strings** (API errors, log lines shown in the UI) stay
  Ukrainian. English happens only client-side: the `EN` map in
  `app/static/app.js` translates the chrome, `trServer`/`trLog` map server
  messages by pattern. A new UI string must be added to the `EN` map (and a
  server message needs a pattern in `SERVER_RE`/`LOG_RE`), otherwise English
  mode silently shows Ukrainian.
- Digits inside menu announcements are written as words
  (`flow.DIGIT_WORDS`) — TTS reads them more reliably than "1"/"0".

## FreeSWITCH config gotchas

- **`X-PRE-PROCESS` understands only double quotes** around attributes; with
  single quotes the scanner silently yields empty values. The `exec-set`
  shell commands in `fs/vars.xml` therefore contain no inner quotes and no
  `&&` (the XML entity `&amp;` is not decoded either) — only `if/then/fi` and
  `${VAR:-default}`.
- A UI SIP profile is not a gateway until materialized
  (`app/gateways.py`); `DIAL_STRING_TEMPLATE` must contain `{gw}`.
- Sofia `rescan` does not apply profile parameters — those need a profile
  restart (`reload-fs.yml -e hard=1` on a deployed host).

## IVR/ESL implementation notes

- Menu announcements play **inside** `play_and_get_digits` as its prompt file
  (not as a separate playback) so a digit can barge in mid-word; a separate
  playback is not interruptible.
- The `OutboundSession.dtmf` event queue is deliberately not consulted for
  menu input: a digit arrives both as an event and in the channel input
  queue, and reading both counted one press twice.
- Prompts are rendered with no baked-in lead-in (silence between menu rounds
  reads as dead air); the single initial "bring the phone to your ear" pause
  is played by the IVR itself (`silence_stream`, `ivr.LEAD_IN_MS`). The
  ad-hoc single-call path (`POST /call`) is the only place a lead-in is baked
  into the WAV.
- `max_chunk_length=10` in synthesis keeps "one sentence — one chunk";
  otherwise the inter-sentence `silence_duration` has no effect.
- `api` commands on the outbound socket are limited; `module_exists mod_amd`
  is probed once over the inbound ESL connection (`jobs.amd_available`).

## Definition of done

A change is done when it passes the relevant verification levels from
[testing.md](testing.md) and the documentation under `docs/` still tells the
truth. In reports, explicitly separate "verified by me" (levels 1–4) from
"needs verification by a human" (level 5).
