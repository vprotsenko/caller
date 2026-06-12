# Security

## Secrets inventory

| Secret | Where it lives | Protection |
|---|---|---|
| Web UI password (`WEB_PASSWORD`) | `.env` | Gitignored; if unset, a random password is generated at startup and logged once, so the app is never left open |
| ESL password (`ESL_PASSWORD`) | `.env` | Gitignored; the Event Socket listens on **127.0.0.1 only** |
| SIP trunk passwords | SQLite (`data/`), generated `fs/sip_profiles/external/gw_*.xml` | Gitignored; never returned by the API (only `password_set`), never logged, never appear in `/status` |
| Operator SIP passwords | SQLite (`data/`), generated `fs/directory/default/*.xml` | Same treatment |

`data/`, `.env`, and the generated XML files are gitignored — **never commit
them under any circumstances**. Protect `data/` with the same file
permissions as `.env`.

## Known limitations (accepted, documented)

- **SIP and operator passwords are stored in plaintext** in the SQLite
  database. FreeSWITCH needs the cleartext for digest authentication, so the
  mitigation is containment, not hashing: passwords are write-only at the API
  (only `password_set` is ever returned), never logged, and `data/` must be
  permission-protected like `.env`. Keep these properties when touching
  profile or operator code.
- **The application serves plain HTTP**; Basic Auth credentials travel in
  cleartext. A TLS-terminating reverse proxy in front of the app is a
  production requirement, not an option.
- **SIP signaling has no TLS** (the trunk and operator profiles use UDP/TCP
  with digest auth). Enable SIP TLS per provider/softphone support if the
  network path is untrusted.

## Network exposure

| Port | Service | Exposure |
|---|---|---|
| 8000 | Web UI / API (Basic Auth, plain HTTP) | Put behind a TLS reverse proxy; do not expose directly |
| 8021 | FreeSWITCH Event Socket | 127.0.0.1 only, password-protected |
| 8084 | Outbound IVR socket | 127.0.0.1 only |
| 5060 + RTP range | SIP/RTP (host networking) | Required for the trunk and operator softphones |

## Hardening checklist

- [ ] Strong, unique `WEB_PASSWORD` and `ESL_PASSWORD` in `/opt/caller/.env`
- [ ] Reverse proxy with TLS in front of port 8000; application port
      firewalled from the outside
- [ ] `data/` and `.env` readable only by the service user
- [ ] Firewall: SIP/RTP open only to the trunk provider's ranges where
      practical
- [ ] `CALLER_ID_NUMBER` set to a DID you are authorized to use
- [ ] Operator passwords ≥ 6 characters (enforced by the API) and unique

## Operational safety

- One campaign at a time; concurrent starts are rejected with 409.
- Numbers that opted out (`optout`) are **never** redialed by retry-failed.
- An interrupted campaign is never resumed automatically — resuming places
  real calls and requires an explicit user action.
