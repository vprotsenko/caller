# Ansible — deploying and operating Dialer 2.0

The host is **Linux with a public IP** (a SIP/RTP requirement). Set it in
[inventory.ini](inventory.ini). The full deployment guide (requirements, TLS,
AMD image, post-deploy checks) is [docs/deployment.md](../docs/deployment.md);
this page is the playbook reference.
`ansible.cfg` keeps one SSH connection for the whole playbook (ControlMaster)
+ pipelining — less overhead per task.

## Which playbook when (speed)

| What changed | Playbook | Time | What it does |
|---|---|---|---|
| Python code in `app/` | `redeploy-app.yml` | ~10-30 s | rebuild+restart **app only**; FreeSWITCH stays up |
| `fs/` config (sofia, gateway, sip-trace) | `reload-fs.yml` | ~5 s | sync + `reloadxml` + sofia profile restart, **no** container restart |
| `fs/modules.conf` (new module) | `reload-fs.yml -e hard=1` | ~15-20 s | FreeSWITCH container restart |
| Everything / `requirements.txt` / first time | `deploy.yml` | full | sync everything + build + recreates only the changed service |

```bash
cd caller/ansible

# Fast: changed controller code
ansible-playbook redeploy-app.yml

# Fast: changed FreeSWITCH config (sip-trace, gateway, codecs, etc.)
ansible-playbook reload-fs.yml
ansible-playbook reload-fs.yml -e hard=1     # if modules.conf changed

# Full deploy (first time / dependency changes)
ansible-playbook deploy.yml
#    For real AMD: build the image with mod_amd and point at it:
ansible-playbook deploy.yml -e freeswitch_image=caller-freeswitch:amd

# Test campaign (Basic Auth creds are read from /opt/caller/.env)
ansible-playbook call.yml \
  -e 'message=Добрий день! Це тест.' -e 'numbers=+380671234567' \
  -e campaign_type=operator -e ivr_operator=true

# Watch until completion
ansible-playbook status.yml -e wait=1
```

`call.yml` sends the `/start` JSON contract: `numbers` are
comma-separated, the IVR flags are `ivr_operator|ivr_repeat|ivr_optout`. The
SIP profile is the default one in the DB (seeded from `SIP_*` on first start)
or `-e profile_id=N`.

Secrets (`SIP_PASSWORD`, the web password) go through `no_log: true` and never
appear in Ansible output. `.env` is updated line-by-line — values added by
hand on the server survive a redeploy.
