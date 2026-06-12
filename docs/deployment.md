# Deployment

## Requirements

- **Linux host with a public IP** — a hard SIP/RTP requirement. Both
  containers run with `network_mode: host`; real calls do not work behind
  Docker NAT or on macOS.
- Docker + Docker Compose on the host.
- A SIP trunk account (server, username, password) and a Caller ID (DID)
  issued by the provider — without `CALLER_ID_NUMBER` many providers reject
  outbound calls with `CALL_REJECTED` / 403.
- Ansible on the workstation for automated deploys.

## Automated deploy (Ansible)

Playbooks live in [`ansible/`](../ansible/); set the host in
`ansible/inventory.ini`. Full playbook reference (including test-call and
status playbooks): [`ansible/README.md`](../ansible/README.md).

```bash
cd ansible
ansible-playbook deploy.yml          # first deploy / dependency changes
```

Pick the cheapest playbook for the change:

| What changed | Playbook | Effect |
|---|---|---|
| Python code in `app/` | `redeploy-app.yml` | ~10–30 s, rebuilds and restarts the app only; FreeSWITCH stays up |
| `fs/` config | `reload-fs.yml` | ~5 s, `reloadxml` + sofia rescan, no container restart |
| `fs/modules.conf` or sofia profile params | `reload-fs.yml -e hard=1` | FreeSWITCH container restart (`rescan` does not apply profile params) |
| Everything / `requirements.txt` / first time | `deploy.yml` | Full sync + build |

Secrets never appear in Ansible output (`no_log: true`); the server-side
`.env` (`/opt/caller/.env`) is updated line-by-line, so values added by hand
on the server survive a redeploy.

## Manual deploy

```bash
cp .env.example .env    # fill in WEB_PASSWORD, ESL_PASSWORD, SIP_*, CALLER_ID_NUMBER
docker compose up --build -d
```

See [configuration.md](configuration.md) for every variable. If the host is
behind NAT, set `EXTERNAL_IP` to the public IP — otherwise expect one-way
audio (see [operations.md](operations.md)).

## Answering-machine detection (optional)

The default FreeSWITCH image does not include `mod_amd`; without it every
answered call is classified as HUMAN and is never dropped — the system
degrades gracefully. For real AMD, build the bundled from-source image and
point the deployment at it:

```bash
docker build -f Dockerfile.freeswitch -t caller-freeswitch:amd .
echo 'FREESWITCH_IMAGE=caller-freeswitch:amd' >> .env
docker compose up -d
# or with Ansible:
ansible-playbook deploy.yml -e freeswitch_image=caller-freeswitch:amd
```

## TLS

The application itself serves plain HTTP with Basic Auth. In production put a
reverse proxy (nginx, Caddy, Traefik) with TLS in front of port 8000 and do
not expose the application port directly. See [security.md](security.md).

## Post-deploy checks

```bash
docker compose exec freeswitch fs_cli -p "$ESL_PASSWORD" -x "status"         # engine up
docker compose exec freeswitch fs_cli -p "$ESL_PASSWORD" -x "sofia status"   # trunk REGED
ansible-playbook call.yml -e 'message=Тест' -e 'numbers=+380…'               # test call
ansible-playbook status.yml -e wait=1                                        # watch it finish
```

The full verification ladder is in [testing.md](testing.md); the runbook for
anything that fails is [operations.md](operations.md).
