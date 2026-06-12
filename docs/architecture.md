# Architecture

## Components

Two containers, both with `network_mode: host` (SIP/RTP must see the host's
real IP; ESL is shared over 127.0.0.1):

- **freeswitch** — the telephony engine. Configuration is static XML from
  [`fs/`](../fs/), mounted read-only; credentials are injected at container
  start by `fs/vars.xml` from environment variables.
- **app** — the controller: a FastAPI application (HTTP Basic Auth) serving
  the static web UI and orchestrating calls.

The controller talks to FreeSWITCH over two ESL channels:

- **Inbound ESL client** ([`app/esl.py`](../app/esl.py)) — a hand-rolled
  asyncio client to `127.0.0.1:8021` used for `originate`, health checks and
  events. One shared connection, created lazily and re-established on drop.
  (greenswitch was rejected: gevent is incompatible with the asyncio loop.)
- **Outbound socket server** ([`app/ivr.py`](../app/ivr.py)) — listens on
  `127.0.0.1:8084`. Every answered call is originated with
  `&socket(... async full)`: FreeSWITCH opens a TCP connection back to the
  controller, and an `OutboundSession` drives the call (playback,
  `play_and_get_digits`, bridge, AMD handling).

```
 browser ──HTTP/Basic──► app (FastAPI + UI)
                          │            ▲
              ESL inbound │ :8021      │ outbound socket :8084
                          ▼            │
                        freeswitch ────┘
                          │
                     SIP/RTP (trunk, operator softphones)
```

## Campaign lifecycle

1. `POST /start` ([`app/main.py`](../app/main.py)) — validation plus a
   dry-run compilation of the IVR form.
2. A `campaign` row is created with snapshots of both the compiled graph
   (`ivr_flow`) and the source form (`ivr_form`), so later edits to the
   scenario cannot affect the running campaign or its history.
3. The worker ([`app/jobs.py`](../app/jobs.py)):
   - prerenders every prompt to WAV (cache keyed by a hash of
     text+voice+params — a campaign must never start half-mute);
   - materializes the campaign's SIP profile into a FreeSWITCH gateway
     ([`app/gateways.py`](../app/gateways.py) →
     `fs/sip_profiles/external/gw_profile_<id>.xml` + profile rescan);
   - keeps up to `max_concurrent` (1..5) calls in flight:
     `claim_next_pending` → `originate`.
4. When a call is answered, FreeSWITCH connects to the outbound socket; the
   session finds its `ivr.CallContext` in `REGISTRY` by `origination_uuid`
   and runs the call: AMD → lead-in silence → IVR graph interpretation.
5. Each number's outcome is written to SQLite immediately (durable; a process
   restart marks the campaign `interrupted`, and resume is an explicit user
   action because it places real calls).

One campaign runs at a time; a second `POST /start` gets `409`.

## Module map

| Module | Responsibility |
|---|---|
| [`app/main.py`](../app/main.py) | FastAPI routes, auth, request validation |
| [`app/jobs.py`](../app/jobs.py) | Campaign worker: prompt prerender, dialing loop, outcome mapping |
| [`app/flow.py`](../app/flow.py) | Compiler of the recursive IVR form into flat `play\|menu\|bridge\|hangup` nodes + validator. Option actions: `operator\|replay\|menu\|play\|back\|home\|optout\|hangup`. The legacy flat form (checkboxes, sent by `ansible/call.yml`) is converted by `_legacy_menu`. Pure functions, fully unit-tested |
| [`app/ivr.py`](../app/ivr.py) | Outbound socket server, `OutboundSession`, IVR graph interpreter |
| [`app/esl.py`](../app/esl.py) | Asyncio inbound ESL client (shared connection) |
| [`app/db.py`](../app/db.py) | SQLite persistence (WAL, one guarded connection) |
| [`app/gateways.py`](../app/gateways.py) | SIP profile → FreeSWITCH gateway XML materialization |
| [`app/operators.py`](../app/operators.py) | Operator extensions: directory XML + `reloadxml`; live availability via `sofia_contact`; `OperatorPool` paces dialing by free operators |
| [`app/amd.py`](../app/amd.py) | Pure AMD policy (see below) |
| [`app/tts.py`](../app/tts.py) | Supertonic synthesis (lazy model, serialized by a lock) + resampling to telephony format |
| [`app/static/`](../app/static/) | Web UI: `index.html` + `app.js`, bilingual uk/en |

## AMD policy

[`app/amd.py`](../app/amd.py) is pure policy: on a MACHINE verdict an info
campaign waits for the beep and leaves the message (`voicemail-left`), an
operator campaign hangs up (`machine-hangup`); HUMAN and NOTSURE follow the
normal flow — a doubtful call is never dropped. Without `mod_amd` in the
FreeSWITCH image every answer is treated as HUMAN (graceful degradation; see
[deployment.md](deployment.md) for the AMD-enabled image).

## Data model

SQLite (WAL mode, one shared connection guarded by a lock) on a mounted
volume (`data/`). Tables:

| Table | Contents |
|---|---|
| `sip_profile` | SIP trunk profiles (server, port, username, password) |
| `operator` | Operator extensions and credentials |
| `scenario` | Saved scenarios — stores the **form** (round-trips through the editor), not the compiled graph; compilation happens at campaign start |
| `campaign` | Campaign runs with snapshots of the compiled graph and the source form |
| `campaign_number` | Per-number outcomes |

## Number status state machine

`pending → ringing →` one of:

| Status | Meaning |
|---|---|
| `answered` | Listened to the message / IVR, no special outcome |
| `transferred` | Connected to a live operator |
| `missed-operator` | Requested an operator but was never bridged |
| `optout` | Pressed the opt-out option — **never redialed** |
| `voicemail-left` | AMD: message left after the beep (info campaign) |
| `machine-hangup` | AMD: machine detected, hung up (operator campaign) |
| `busy` | `USER_BUSY` |
| `no-answer` | `NO_ANSWER` / `ORIGINATOR_CANCEL` / `NO_USER_RESPONSE` |
| `failed` | Everything else |

Retry-failed re-dials the statuses in `db.RETRYABLE`; `optout` is excluded
unconditionally.

## UI

[`app/static/index.html`](../app/static/index.html) +
[`app/static/app.js`](../app/static/app.js); tabs Кампанія / Сценарії /
Налаштування / Історія; `/status` is polled every 1.5 s. The UI is bilingual:
Ukrainian is canonical in the markup and JS literals; the `EN` map in `app.js`
translates the chrome, `trServer`/`trLog` map Ukrainian server messages by
pattern, and the language toggle persists in `localStorage`. The backend stays
Ukrainian-only — see [development.md](development.md) for the language
conventions.
