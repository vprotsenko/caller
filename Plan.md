# Технічне завдання: Дзвонилка 2.0 на FreeSWITCH

Гілка `dialer-v2`. Попередня версія (PJSIP, послідовний обдзвін) живе на `main`
і залишається робочою для внутрішніх задач; ця гілка — окрема лінія розробки,
мердж у `main` не планується.

## 1. Мета

Autodialer комерційного рівня: обдзвін списку номерів синтезованим
повідомленням (TTS Supertonic, українська), IVR-меню з реакцією на DTMF,
перемикання зацікавлених абонентів на живого оператора, детекція
автовідповідачів (AMD), повна статистика по кампаніях.

**Поза скоупом цього ТЗ** (окреслено як майбутні фази): predictive-темп
набору, черги `mod_callcenter` на багатьох операторів, інтеграція з білінгом
(Sippy), запис розмов, multi-tenant, TLS (до продакшену — reverse proxy).

## 2. Архітектура

```
Веб-UI (статичний, свій) ──HTTP──> Контролер (FastAPI, Python 3.11)
                                      │            │
                                   SQLite      ESL (inbound: originate/події;
                                 (кампанії,     outbound socket: керування
                                  номери,       дзвінком — IVR, bridge)
                                  статуси,         │
                                  сценарії)    FreeSWITCH 1.10.x ──SIP/RTP──> транк FlySIP
                                                   │
                                            SIP-оператори (софтфони,
                                            extension 1001, 1002…)
```

Два Docker-контейнери (`docker compose`, `network_mode: host` для обох або
лише для FreeSWITCH):

1. **freeswitch** — телефонний движок. Конфігурація — статичні XML у репо,
   монтуються volume'ом. Потрібні модулі: `mod_sofia`, `mod_event_socket`,
   `mod_dptools`, `mod_amd`, `mod_avmd`, `mod_sndfile`, `mod_commands`.
   Базовий образ: офіційні пакети SignalWire (потрібен безкоштовний
   personal access token — кладеться в `.env`) або збірка з сирців у
   Dockerfile, якщо токен незручний.
2. **app** — FastAPI-контролер + статичний UI + TTS. Python **3.11**
   (переїжджає `tts.py` з його `audioop`-ресемплером; апгрейд Python — окрема
   задача із заміною ресемплера, не зараз).

### Компоненти контролера

| Модуль | Відповідальність |
|---|---|
| `app/main.py` | HTTP-маршрути, Basic Auth (як у v1) |
| `app/db.py` | SQLite (WAL), схема нижче |
| `app/tts.py` | без змін з v1: синтез + ресемплінг у WAV 8 kHz/16-bit/mono |
| `app/esl.py` | inbound-з'єднання до FreeSWITCH: `bgapi originate`, підписка на події (`CHANNEL_ANSWER`, `CHANNEL_HANGUP_COMPLETE`, реєстрації операторів) |
| `app/ivr.py` | outbound-socket-сервер: на кожен відповіданий дзвінок FreeSWITCH підключається сюди, модуль інтерпретує JSON-сценарій (AMD → playback → меню → bridge) |
| `app/jobs.py` | воркер кампанії: бере pending-номери, тримає темп, пише результати |
| `app/static/index.html` | UI, як у v1 — одна сторінка, три вкладки |

ESL-бібліотека: `greenswitch` (має готовий outbound-socket-сервер);
якщо на PoC виявиться проблемною — протокол текстовий, пишемо мінімальний
власний клієнт на asyncio. Рішення фіксується на етапі 1.

## 3. Конфігурація FreeSWITCH

Статичні файли в `fs/` (репо), мінімум:

- `sofia` external-профіль: `ext-rtp-ip` / `ext-sip-ip` (NAT), кодеки
  PCMA/PCMU.
- Gateway до FlySIP: register + digest auth. **Перша версія: один gateway,
  креди підставляються з `.env` при старті** (шаблонізація конфігу
  entrypoint-скриптом). Керування кількома профілями з UI — етап 5:
  контролер генерує `gateway/*.xml` із таблиці `sip_profile` і робить
  `sofia profile external rescan`.
- Directory: extension'и операторів (1001…1009) зі своїми паролями — щоб
  софтфони реєструвалися прямо на FreeSWITCH.
- Dialplan: майже порожній — вихідні дзвінки створює контролер через
  `originate ... &socket(127.0.0.1:8084 async full)`, тобто кожен
  відповіданий дзвінок передається в керування `app/ivr.py`.
- `event_socket.conf`: слухати тільки на 127.0.0.1, пароль з `.env`.

## 4. Схема БД (SQLite, WAL)

```sql
sip_profile(id, name, server, username, password, is_default, created_at)
operator(id, name, extension, enabled)            -- цілі для bridge
campaign(
  id, name, status,            -- running|done|interrupted|stopped
  message_text, voice,
  ivr_flow TEXT,               -- JSON-знімок сценарію (див. §5)
  profile_id,                  -- транк
  max_concurrent INTEGER,      -- темп, див. §7
  created_at, started_at, finished_at
)
campaign_number(
  id, campaign_id, number,
  status,                      -- pending|ringing|answered|transferred|
                               -- voicemail-left|machine-hangup|no-answer|
                               -- busy|failed|optout|missed-operator
  hangup_cause TEXT,           -- сирий Q.850/SIP-код для діагностики
  amd_result TEXT,             -- HUMAN|MACHINE|NOTSURE|NULL
  dtmf TEXT,                   -- натиснуті цифри
  attempts INTEGER, updated_at
)
```

Правила з v1 зберігаються: пароль профілю ніколи не повертається клієнту
(тільки `password_set`), не логується, не потрапляє у `/status`; каталог
`data/` із БД — секретний.

## 5. Формат IVR-сценарію

UI — **параметризована форма** (рівень 1, рішення зафіксоване): клієнт ставить
галочки і пише тексти, кожен текст має кнопку «Прослухати» (`/preview`).
Форма компілюється в JSON, який зберігається знімком у `campaign.ivr_flow`:

```json
{
  "start": "msg",
  "nodes": {
    "msg":    {"type": "play",   "prompt": "main", "next": "menu"},
    "menu":   {"type": "menu",   "timeout_sec": 5, "max_repeats": 2,
               "branches": {"1": "to_op", "2": "msg", "0": "optout"},
               "on_timeout": "bye"},
    "to_op":  {"type": "bridge", "prompt": "connecting"},
    "optout": {"type": "play",   "prompt": "optout_ok", "mark": "optout", "next": "bye"},
    "bye":    {"type": "hangup"}
  },
  "prompts": {
    "main":       {"text": "<повідомлення>", "voice": "F3"},
    "connecting": {"text": "Зачекайте, з'єднуємо з оператором", "voice": "F3"},
    "optout_ok":  {"text": "Вас видалено зі списку", "voice": "F3"}
  }
}
```

- Типи вузлів першої версії: `play`, `menu`, `bridge`, `hangup`.
- На старті кампанії всі `prompts` пререндеряться у WAV (кеш за
  hash(text+voice), каталог `audio/`); кампанія не стартує, якщо синтез
  упав.
- Серверна валідація: всі гілки ведуть на існуючі вузли, у `menu` є
  `on_timeout`, ліміт повторів ≤ 5, глибина обходу обмежена — цикл
  неможливий.
- Інтерпретатор (`app/ivr.py`) не знає про форму — тільки про JSON, тож
  складніший редактор у майбутньому не зачіпає рантайм.

## 6. Сценарій дзвінка (життєвий цикл номера)

1. Воркер бере pending-номер →
   `bgapi originate {originate_timeout=30,...}sofia/gateway/<gw>/<number> &socket(...)`.
2. Немає відповіді / зайнято / відмова провайдера → мапінг hangup cause:
   `USER_BUSY→busy`, `NO_ANSWER|ORIGINATOR_CANCEL|NO_USER_RESPONSE→no-answer`,
   `CALL_REJECTED|інше→failed` (сирий код — у `hangup_cause`).
3. Відповідь → FreeSWITCH конектиться до outbound-socket → **AMD**
   (`mod_amd`, дефолтні пороги, тюнінг на реальних дзвінках):
   - `MACHINE`: інфо-кампанія → чекати біп (`mod_avmd`), програти
     повідомлення → `voicemail-left`; операторська кампанія → hangup →
     `machine-hangup`;
   - `HUMAN` / `NOTSURE` → далі (сумнівних не скидаємо).
4. Інтерпретація `ivr_flow`: `play` → `playback`; `menu` →
   `play_and_get_digits`; натиснута цифра пишеться у `dtmf`.
5. Вузол `bridge`: фраза → `bridge user/<extension>` на вільного оператора
   (визначення вільності — §7). Міст відбувся → `transferred`; абонент
   не дочекався/скинув → `missed-operator`.
6. Будь-який нормальний кінець після прослуховування → `answered`;
   `optout` → статус `optout` (номер виключається з майбутніх retry).
7. Кожна зміна статусу одразу пишеться в БД (durable, як у v1):
   рестарт процесу → кампанія `interrupted`, явний resume з UI.

## 7. Темп набору (перша версія — без predictive)

- `campaign.max_concurrent` — скільки номерів набирається одночасно
  (дефолт 1; UI дозволяє 1–5).
- Для кампаній з оператором додаткове правило: нових `originate` не більше,
  ніж вільних операторів. Вільність оператора контролер веде сам:
  зареєстрований (події `sofia::register`) і не в активному мості.
- Ліміти транка FlySIP (одночасні дзвінки, CPS) — з'ясувати у провайдера
  до етапу 5; `max_concurrent` обмежується цим значенням.

## 8. HTTP API (еволюція v1)

Без змін за духом: Basic Auth усюди (`WEB_USER`/`WEB_PASSWORD`).

| Маршрут | Зміни щодо v1 |
|---|---|
| `GET /` | той самий статичний UI |
| `POST /preview` | без змін (синтез + URL для плеєра) |
| `POST /start` | + поля IVR-форми (галочки/тексти/таймаут), `max_concurrent`; компіляція у `ivr_flow` |
| `GET /status` | + поля: `amd_result`-зведення, активні мости з операторами |
| `GET/POST/DELETE /config/profiles` | як у v1 (пароль → тільки `password_set`) |
| `GET/POST/DELETE /config/operators` | **нове**: CRUD операторів (ім'я, extension) |
| `GET /campaigns`, `GET /campaigns/{id}`, `…/retry-failed`, `…/resume` | як у v1; retry-failed не чіпає `optout` |

## 9. Веб-UI

Той самий підхід: один статичний `index.html`, три вкладки.

- **Кампанія**: повідомлення + голос + прев'ю; IVR-форма (галочки «1 →
  оператор», «2 → повторити (N разів)», «0 → відписатися», тексти фраз із
  прев'ю, таймаут/дія за замовчуванням); список номерів; `max_concurrent`;
  живий прогрес (полінг `/status` 1.5 с) з лічильниками за всіма статусами
  §4.
- **Налаштування**: SIP-профілі (як у v1) + список операторів.
- **Історія**: кампанії з лічильниками, resume, retry-failed; розгортання
  до по-номерної таблиці зі статусом/AMD/DTMF.

## 10. Деплой

- `docker-compose.yml`: сервіси `freeswitch` + `app`, volumes `./data`
  (SQLite — секрети!), `./audio` (WAV-кеш), `./fs` (конфіг FreeSWITCH).
- Ansible адаптується з v1: `deploy.yml` (Docker + sync + `.env`
  line-by-line + compose up), `call.yml`/`status.yml` — проти нових
  ендпойнтів. Хост — Linux із публічним IP (вимога RTP, як у v1; на macOS
  Docker Desktop дзвінки не працюють).
- `.env`: `WEB_USER/WEB_PASSWORD`, `SIP_*` (перший gateway),
  `ESL_PASSWORD`, `SIGNALWIRE_TOKEN` (якщо пакетна збірка), `LANG_CODE`.

## 11. Етапи та критерії приймання

| # | Етап | Готово, коли |
|---|---|---|
| 1 | **PoC**: образ FreeSWITCH, gateway FlySIP, ESL-з'єднання, `originate` одного номера з `playback` WAV | тестовий дзвінок на мобільний: чути синтезоване повідомлення; обраний ESL-клієнт зафіксовано |
| 2 | **IVR-движок**: outbound socket, інтерпретатор JSON (§5), збір DTMF, статуси в БД | дзвінок проходить сценарій «повідомлення → меню → повтор/завершення», `dtmf` і статус у БД |
| 3 | **Оператор**: directory, софтфон (MicroSIP/Zoiper), `bridge`, вільність операторів | «натисни 1» з'єднує з софтфоном; `transferred`/`missed-operator` коректні |
| 4 | **AMD**: `mod_amd`/`mod_avmd` у сценарії, нові статуси | на тестах: людина → IVR, голосова пошта → `voicemail-left`/`machine-hangup`; пороги задокументовані |
| 5 | **Повний цикл**: UI з IVR-формою, кампанії, історія, retry/resume, оператори й профілі в UI, Ansible | кампанія на 10+ номерів керується повністю з браузера; рестарт контейнера не втрачає прогрес |
| 6 | (поза ТЗ) `mod_callcenter`, predictive, запис розмов, Sippy | — |

Етапи 1–4 тестуються скриптом/`call.yml` без повного UI.

## 12. Ризики й обмеження

- **AMD ~80–90% точності**; `NOTSURE` → до людини. Людина чує ~2 с тиші,
  поки йде аналіз — компенсується коротким `LEAD_IN`.
- **NAT/RTP** — перше, що ламається: перевіряти `ext-rtp-ip` одразу на
  етапі 1 реальним дзвінком.
- **Дві платні ніжки** при бриджі на мобільний оператора → оператори
  тільки на SIP-софтфонах.
- **Секрети**: SIP-паролі у SQLite (plaintext, як у v1 — усвідомлений
  трейд-оф), `data/` і `.env` захищені; Basic Auth без TLS — до продакшену
  reverse proxy з TLS.
- **Юридичне**: згода абонентів на обдзвін, обов'язковий пункт «0 —
  відписатися» у комерційних кампаніях.

## 13. Прийняті за замовчуванням рішення (можна переграти)

1. **SQLite, не PostgreSQL** — одна кампанія за раз і десятки одночасних
   дзвінків SQLite+WAL тримає; PG — коли з'явиться multi-tenant/predictive.
2. **Python 3.11** — заради `tts.py` без змін.
3. **`greenswitch`** як ESL-клієнт — перевіряється на етапі 1.
4. **Один транк у `.env`** до етапу 5, потім UI-профілі з rescan.
5. Імена статусів/таблиць — українські назви лише в UI, у коді/БД англійські.
