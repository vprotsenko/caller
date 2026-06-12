# Dialer 2.0

An outbound call-automation platform built on FreeSWITCH: voice campaigns with
synthesized speech (Supertonic TTS, Ukrainian), interactive IVR menus driven by
DTMF, transfer to live SIP operators, answering-machine detection (AMD), and
full per-campaign reporting.

## Key features

- **Campaign dialing** — a list of numbers dialed with up to 5 concurrent
  calls, live progress in the UI, durable per-number outcomes in SQLite.
- **Text-to-speech** — Supertonic (Ukrainian by default) with voice, speed and
  pause controls and instant audio preview.
- **IVR constructor** — a visual form that builds menus of arbitrary nesting
  (up to 4 levels); option actions: transfer to operator, replay, submenu,
  play a message, back, home, opt-out, hang up.
- **Operator transfer** — SIP softphone extensions (MicroSIP, Zoiper, …);
  dialing pace adapts to the number of free registered operators.
- **Answering-machine detection** — info campaigns leave the message after the
  beep, operator campaigns hang up; a doubtful verdict is never dropped.
- **Scenario library** — saved, editable campaign scenarios; a running
  campaign keeps its own snapshot, so editing or deleting a scenario never
  corrupts history.
- **Campaign history** — per-number details, resume of interrupted campaigns,
  retry of failed numbers (opted-out numbers are never redialed).
- **Bilingual UI** — Ukrainian (canonical) and English.

## Quick start

Requirements: Docker + Docker Compose. Real calls additionally require a
Linux host with a public IP and a SIP trunk (see
[docs/deployment.md](docs/deployment.md)).

```bash
cp .env.example .env        # fill in WEB_PASSWORD, ESL_PASSWORD, SIP_*
docker compose up --build
```

UI: http://localhost:8000 (Basic Auth — `WEB_USER` / `WEB_PASSWORD` from
`.env`). Four tabs: **Кампанія** (message + IVR form + dialing + live
progress), **Сценарії** (scenario library), **Налаштування** (SIP profiles +
operators), **Історія** (campaigns, per-number details, resume/retry).

To try the system without a SIP trunk, run an end-to-end loopback campaign —
see [docs/testing.md](docs/testing.md).

## Documentation

| Document | Contents |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Components, call lifecycle, data model, module map |
| [docs/configuration.md](docs/configuration.md) | Full environment-variable reference |
| [docs/api.md](docs/api.md) | REST API reference |
| [docs/deployment.md](docs/deployment.md) | Production deployment (Ansible), AMD image, TLS |
| [docs/operations.md](docs/operations.md) | Runbook: health checks, diagnostics, troubleshooting |
| [docs/security.md](docs/security.md) | Secrets handling, network exposure, hardening checklist |
| [docs/testing.md](docs/testing.md) | Verification levels, unit tests, loopback E2E |
| [docs/development.md](docs/development.md) | Dev environment, conventions, invariants |
| [ansible/README.md](ansible/README.md) | Playbook reference (deploy / call / status) |

## Verification

```bash
docker compose run --rm app pytest -q                                        # unit tests
docker compose exec freeswitch fs_cli -p "$ESL_PASSWORD" -x "status"         # engine health
docker compose exec freeswitch fs_cli -p "$ESL_PASSWORD" -x "sofia status"   # trunk status
```

The full verification ladder — from unit tests to a real call against live
voicemail — is described in [docs/testing.md](docs/testing.md).

## Security

`.env` and `data/` (the SQLite database holds SIP and operator credentials)
are gitignored and must be protected by file permissions; generated FreeSWITCH
XML with credentials is gitignored too. The built-in Basic Auth has no TLS —
terminate TLS at a reverse proxy before exposing the UI. Details and the
hardening checklist: [docs/security.md](docs/security.md).
