# Operations runbook

## Health checks

```bash
docker compose exec freeswitch fs_cli -p "$ESL_PASSWORD" -x "status"         # engine health
docker compose exec freeswitch fs_cli -p "$ESL_PASSWORD" -x "sofia status"   # SIP profiles & trunks
curl -u "$WEB_USER:$WEB_PASSWORD" localhost:8000/status                      # controller + ESL + operators
```

`/status` reports `esl_connected`; the UI surfaces the same data and polls it
every 1.5 s. `fs_cli` inside the container always needs `-p "$ESL_PASSWORD"`.

## Logs and call diagnostics

- Application logs: `docker compose logs -f app`.
- FreeSWITCH call logs require `mod_logfile` and `sip-trace=yes` on the
  external profile (both are enabled in the bundled config — without them this
  image starts with `-nf` and produces **no** call logs).
- RTP-level diagnosis on the host:
  `tcpdump -ni any 'udp and host <trunk-media-ip> and greater 120'` — count
  packets per direction.

## Interrupted campaigns

A controller restart marks the running campaign `interrupted`. It is **not**
resumed automatically — resuming places real calls, so it is an explicit
action: the **Історія** tab or `POST /campaigns/{id}/resume`.

## Troubleshooting

### Calls rejected with `CALL_REJECTED` / 403

On a registered trunk this is the provider's side: account provisioning or a
missing/wrong Caller ID. Set `CALLER_ID_NUMBER` to the DID issued by the
provider. It is not an application bug.

### Call goes through but there is no audio (one-way silence)

The known causes, in the order worth checking:

1. **Wrong IP in the SDP (NAT/STUN).** The host is behind NAT and
   `EXTERNAL_IP` is unset (or detected wrongly). Set `EXTERNAL_IP` in `.env`
   to the public IP. The bundled config deliberately avoids
   `ext-rtp-ip="auto"`/STUN — on cloud hosts STUN has been seen detecting a
   foreign IP.
2. **`a=sendonly` early media from the trunk (seen with FlySIP) + a
   FreeSWITCH 1.10.12 quirk:** the engine latches the receive-only media mode
   from the second 183 and silently drops all outbound RTP; the `a=sendrecv`
   in the 200 OK never resets it. The application works around this with a
   **two-phase re-INVITE after answer** (`jobs.media_reneg_after_answer`):
   the first plain re-INVITE reopens the write gate, the second (with a
   one-shot `origination_audio_mode=sendrecv`) restores a two-way SDP
   contract, with a `MEDIA_RENEG_PAUSE` (1 s) between them to avoid 491. On
   healthy trunks both are no-ops. Do not disable it; a single re-INVITE does
   not cure the condition, and `disable-hold` has no effect on it.
3. **Provider-side media.** If `tcpdump` shows a *symmetric* RTP flow (our
   src:port ↔ their dst:port, both directions) and the callee still hears
   nothing — it is account provisioning on the provider side, not this
   system.

### IVR starts playing into the ringback

`ignore_early_media=true` is required on originate (set by the application) —
without it `&socket()` starts on the 183 Pre-Answer and the IVR plays before
pickup.

### Calls die with `failed` / `APP_TIMEOUT` mid-prompt

The ESL command/reply for `sendmsg execute` arrives only **after** the
application finishes (with `CHANNEL_EXECUTE_COMPLETE`) on this FreeSWITCH
build. `OutboundSession.execute` therefore uses the execution timeout as the
reply timeout. If a custom prompt or menu round can run longer, raise the
execution timeout — do not lower the reply timeout back to a constant.

### Every answer is classified as HUMAN

`mod_amd` is not in the image. This is graceful degradation, not an error —
build and deploy the AMD image to enable real classification (see
[deployment.md](deployment.md)).

### TTS swallows words or rejects text

- Speed values above ~1.3 silently drop words (measured: 1.4 → 3 of 5
  sentences survive); below 0.7 the model raises an error. The configured
  `tts.SPEED_MIN/MAX` bounds reflect this — do not widen them without
  re-measuring.
- Typographic punctuation (U+02BC apostrophe «зʼєднати», guillemets, em
  dashes) makes synthesis fail with `unsupported character`;
  `jobs.normalize_text` maps them to ASCII before synthesis in both
  `/preview` and campaign prerender.

## Reference: number statuses

See the status table in [architecture.md](architecture.md#number-status-state-machine).
Operational notes:

- Retry-failed redials only `db.RETRYABLE` statuses; `optout` is **never**
  redialed.
- `busy` = `USER_BUSY`; `no-answer` = `NO_ANSWER` / `ORIGINATOR_CANCEL` /
  `NO_USER_RESPONSE`; everything else maps to `failed`.
