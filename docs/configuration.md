# Configuration reference

All configuration is via environment variables, normally set in `.env`
(copy [`.env.example`](../.env.example)). `.env` is gitignored — never commit
it. Variables marked **required** make `docker compose up` fail fast when
unset.

## Web UI

| Variable | Default | Description |
|---|---|---|
| `WEB_USER` | `admin` | Basic Auth username |
| `WEB_PASSWORD` | — | Basic Auth password. If unset, a random one is generated and logged at startup so the app is never left open |

## FreeSWITCH / ESL

| Variable | Default | Description |
|---|---|---|
| `ESL_PASSWORD` | **required** | Event Socket password (full control over the engine). The socket listens on 127.0.0.1 only |
| `FREESWITCH_IMAGE` | `safarov/freeswitch:1.10.12` | FreeSWITCH image. Set to a `mod_amd`-enabled build for real answering-machine detection (see [deployment.md](deployment.md)) |

## SIP trunk (initial profile)

These seed the first SIP profile in the database on first start; afterwards
profiles are managed in the UI (Налаштування) and live in the database.
Empty values bring the gateway up with placeholders and no registration —
enough for tests.

| Variable | Default | Description |
|---|---|---|
| `SIP_SERVER` | — | Trunk SIP server |
| `SIP_PORT` | `5060` | Trunk port |
| `SIP_USER` | — | Trunk username |
| `SIP_PASSWORD` | — | Trunk password |

## Network

| Variable | Default | Description |
|---|---|---|
| `EXTERNAL_IP` | auto (local IP) | Public IP of the host when it is behind NAT. Goes into the SDP; wrong value = one-way audio (see [operations.md](operations.md)) |

## Dialing

| Variable | Default | Description |
|---|---|---|
| `DIAL_STRING_TEMPLATE` | `sofia/gateway/{gw}/{number}` | `{gw}` = gateway generated from the campaign's SIP profile, `{number}` = dialed number. For E2E tests without a trunk: `loopback/{number}/default` |
| `CALLER_ID_NUMBER` | — | Caller ID (DID) the trunk sees. Without it many providers reject outbound calls with `CALL_REJECTED` / 403 |
| `CALLER_ID_NAME` | — | Caller ID display name |
| `ORIGINATE_EXTRA_VARS` | — | Extra originate channel variables (comma-separated `key=value`). Used by loopback E2E (`absolute_codec_string=PCMA`); keep empty in production |
| `BRIDGE_EXTRA_VARS` | — | Extra bridge channel variables. Same E2E use; keep empty in production |
| `AMD_ENABLED` | `1` | Set `0` to skip AMD even when `mod_amd` is available |

## TTS

| Variable | Default | Description |
|---|---|---|
| `LANG_CODE` | `uk` | TTS language |

## Fixed by docker-compose (not meant to be overridden)

| Variable | Value | Description |
|---|---|---|
| `ESL_HOST` / `ESL_PORT` | `127.0.0.1:8021` | Where the controller reaches the Event Socket |
| `FS_CONF_DIR` | `/app/fs` | FreeSWITCH config directory as seen by the controller (it writes generated gateway and operator XML there) |
| `AUDIO_DIR` | `/app/audio` | Synthesized prompt cache, shared with FreeSWITCH at the same path |

## Audio format

IVR prompts and the voicemail-drop message are WAV **8000 Hz / mono /
16-bit PCM** — the telephony format FreeSWITCH plays without transcoding.
`tts._verify` enforces this on every render.
