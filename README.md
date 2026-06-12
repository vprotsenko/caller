# Dialer 2.0 (FreeSWITCH + ESL)

A commercial-grade autodialer: TTS messages (Supertonic, Ukrainian), IVR with
DTMF, transfer to a live operator, AMD, full per-campaign statistics.
The agent rules are [CLAUDE.md](CLAUDE.md).

**Status: stages 1–5 implemented** (PoC → IVR engine → operator/bridge → AMD →
full UI with campaigns, history, profiles and operators + Ansible). Verification
levels 1–4 pass; real calls/audio/AMD against live voicemail are level 5
(a human with a phone on a Linux host).

## Running

```bash
cp .env.example .env      # fill in WEB_PASSWORD, ESL_PASSWORD, SIP_* (FlySIP)
docker compose up --build # Linux host with a public IP (production mode)
```

Local development on macOS (no real calls — UI/preview/loopback only):

```bash
docker compose -f docker-compose.yml -f docker-compose.macos.yml up --build
```

UI: http://localhost:8000 (Basic Auth — `WEB_USER`/`WEB_PASSWORD` from `.env`).
Three tabs: **Кампанія** (message + IVR form + dialing + live progress),
**Налаштування** (SIP profiles + operators), **Історія** (campaigns,
per-number details, resume/retry-failed).

## Real AMD (optional)

The base FreeSWITCH image does not include `mod_amd`; without it every answer
is treated as HUMAN (the call is never dropped). For answering-machine
classification:

```bash
docker build -f Dockerfile.freeswitch -t caller-freeswitch:amd .  # fragile from-source build
echo 'FREESWITCH_IMAGE=caller-freeswitch:amd' >> .env
docker compose up -d
```

## Verification

```bash
# level 1: unit tests, no FreeSWITCH
docker compose run --rm app pytest -q

# levels 2-3: live containers and valid config
docker compose exec freeswitch fs_cli -p "$ESL_PASSWORD" -x "status"
docker compose exec freeswitch fs_cli -p "$ESL_PASSWORD" -x "sofia status"

# level 4: E2E without a trunk (in .env: DIAL_STRING_TEMPLATE=loopback/{number}/default)
#   start a campaign from the UI to number 9999; DTMF/AMD are simulated
#   with uuid_recv_dtmf <a-leg> 1   and the amd_test_result originate variable
```

Deployment and command-line control — [ansible/](ansible/) (deploy / call / status).

## Secrets

`.env` and `data/` (SQLite with SIP and operator passwords in plaintext) are
not committed and are protected by file permissions. The generated
`fs/directory/default/*.xml` (operator passwords) are gitignored too. Basic
Auth has no TLS — put a reverse proxy with TLS in front before production.
