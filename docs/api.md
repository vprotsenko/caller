# REST API reference

All routes require HTTP Basic Auth (`WEB_USER` / `WEB_PASSWORD`). The UI is a
thin client over this same API. Error responses are
`{"error": "<message>"}` (Ukrainian) with a 4xx/5xx status; conflict
responses from campaign control use `{"detail": ...}` with 409.

> Basic Auth credentials travel in cleartext — terminate TLS at a reverse
> proxy before exposing the API (see [security.md](security.md)).

## UI and audio

| Route | Description |
|---|---|
| `GET /` | The web UI (`static/index.html`) |
| `GET /static/{name}` | Static assets (served with `Cache-Control: no-cache`) |
| `GET /audio/{name}` | A synthesized WAV (served with `no-store`) |

## TTS preview

### `POST /preview` (form)

Synthesize text and return a playable URL.

| Field | Default | Notes |
|---|---|---|
| `text` | required | Normalized before synthesis (typographic punctuation → ASCII) |
| `voice` | default voice | Must be one of `tts.VOICES` |
| `speed`, `steps`, `silence` | model defaults | Clamped to safe ranges |
| `lang` | `LANG_CODE` (`uk`) | Synthesis language; one of `tts.LANGS` (the 31 ISO codes Supertonic-3 supports, plus the `na` fallback) |

Returns `{"voice", "url", "secs"}`.

## Scenarios

Scenarios store the **IVR form** (so they round-trip through the editor);
compilation to the executable graph happens at campaign start. Saving
dry-run-compiles the form, so an invalid scenario cannot be saved.

| Route | Description |
|---|---|
| `GET /scenarios` | List saved scenarios |
| `POST /scenarios` | Create. JSON: `name`, `message`, `voice`, `campaign_type` (`info`\|`operator`), `voice_params` (`speed`/`steps`/`silence`/`lang`), `ivr` (recursive form). 409 on duplicate name |
| `POST /scenarios/{id}` | Update (same body) |
| `DELETE /scenarios/{id}` | Delete. Campaign history is unaffected (campaigns keep snapshots) |

## Campaigns

### `POST /start` (JSON)

Start a campaign — either from a saved scenario or from inline fields.

| Field | Notes |
|---|---|
| `scenario_id` | Use a saved scenario as the content source |
| `message`, `voice`, `campaign_type`, `voice_params`, `ivr` | Inline alternative to `scenario_id` (same shape as scenario content). A legacy flat form with flags `ivr_operator`/`ivr_repeat`/`ivr_optout` is also accepted |

`voice_params.lang` sets the synthesis language for every prompt of the
campaign (default `LANG_CODE`, i.e. Ukrainian). The built-in spoken texts —
auto-generated menu announcements, digit words, default connect/optout
phrases — stay Ukrainian; for a non-Ukrainian campaign write the level
announcements and connect/optout texts yourself in the target language.
| `name` | Campaign name (defaults to the scenario name) |
| `numbers` | List of numbers, or a newline-separated string. Each is normalized; any invalid number fails the whole request |
| `max_concurrent` | 1..5, default 1 |
| `profile_id` | SIP profile; defaults to the database default profile |

Returns `{"campaign_id"}`; `409 {"detail": "campaign already running"}` if a
campaign is active (one campaign at a time).

### Status and history

| Route | Description |
|---|---|
| `GET /status` | Snapshot of the active-or-latest campaign: counters, current calls, log tail, plus engine health (`esl_connected`, `freeswitch`) and live operator states. Polled by the UI every 1.5 s; never returns 500 |
| `GET /campaigns` | Campaign history list |
| `GET /campaigns/{id}` | Campaign details: counters + per-number outcomes (see the status table in [architecture.md](architecture.md)) |
| `POST /campaigns/{id}/retry-failed` | Creates and starts a new campaign `«<name> (повтор)»` from the retryable numbers. **Opted-out numbers are never retried.** 400 if there is nothing to retry or the campaign's SIP profile was deleted |
| `POST /campaigns/{id}/resume` | Resume an `interrupted` campaign (explicit because it places real calls). 409 with a reason on failure |

### `POST /call` (form) — ad-hoc single call

Dials one number and plays one synthesized message (no IVR, no database
record). Useful for trunk smoke tests. Fields: `number`, `text`, `voice`,
`speed`, `steps`, `lang`. One ad-hoc call at a time (409 while one is in flight).
Returns `{"ok", "number", "answered", "cause", "status", "uuid"}`.

## SIP profiles

Passwords are write-only: the API returns only `password_set`, never the
password itself.

| Route | Description |
|---|---|
| `GET /config` | `{"profiles": [...], "default_id"}` |
| `POST /config/profiles` (form) | Create: `name`, `server`, `port`, `username`, `password`, `is_default` |
| `POST /config/profiles/{id}` (form) | Update; blank `password` keeps the stored one |
| `DELETE /config/profiles/{id}` | Delete the profile and its generated gateway XML |

## Operators

| Route | Description |
|---|---|
| `GET /config/operators` | Operators with live `registered` / `busy` flags (`registered: null` when the engine is unreachable) |
| `POST /config/operators` (JSON) | Create: `name`, `extension` (3–6 digits), `password` (min 6 chars). Writes the FreeSWITCH directory entry and triggers `reloadxml` |
| `DELETE /config/operators/{id}` | Delete the operator and its directory entry |
