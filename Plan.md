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
   Базовий образ — **рішення етапу 1: `safarov/freeswitch:1.10.12`**
   (Docker Hub, без SignalWire-токена; лише amd64 — на Apple Silicon працює
   під Rosetta, чого досить для локальних тестів). У ньому є всі модулі
   етапів 1–3 і `mod_avmd`, але **немає `mod_amd`**. **Рішення етапу 4:**
   `mod_amd` компілюється з сирців FreeSWITCH 1.10.12 в окремому build-стейджі
   (`Dockerfile.freeswitch`: спершу збирається spandsp3-форк, потім FS
   `./configure` генерує заголовки, далі `make` лише `mod_amd`, а `.so`
   копіюється в рантайм-образ). Контролер толерує відсутність `mod_amd`
   (трактує відповідь як HUMAN, дзвінок не скидає), тож деградація безпечна.
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

ESL-бібліотека: **рішення етапу 1 — власний мінімальний asyncio-клієнт**
(`app/esl.py`): `greenswitch` ґрунтується на gevent і не співіснує з
asyncio-процесом uvicorn, а текстовий протокол достатньо малий і повністю
покривається юніт-тестами (§16 рівень 1).

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

Скелет конфігу **не писати з нуля** (і не тягнути весь vanilla): взяти
vanilla-конфіг офіційного образу за основу й агресивно вирізати. Мінімальний
набір файлів: `freeswitch.xml`,
`autoload_configs/{modules,switch,sofia,event_socket,amd,avmd}.conf.xml`,
`sip_profiles/external.xml` (+ `external/flysip.xml` — gateway),
`directory/default/100X.xml` (оператори), `dialplan/default.xml`.

## 4. Схема БД (SQLite, WAL)

```sql
sip_profile(id, name, server, username, password, is_default, created_at)
operator(id, name, extension, enabled)            -- цілі для bridge
scenario(                      -- бібліотека збережених варіантів кампаній
  id, name UNIQUE, campaign_type, message_text, voice,
  voice_params TEXT,           -- JSON {speed, steps, silence}
  ivr_form TEXT,               -- JSON РЕКУРСИВНОЇ ФОРМИ (§15), не граф:
                               -- редактор робить round-trip, компіляція при старті
  created_at, updated_at
)
campaign(
  id, name, status,            -- running|done|interrupted|stopped
  campaign_type,               -- info|operator (гілка AMD, див. §6)
  message_text, voice,
  ivr_flow TEXT,               -- JSON-знімок сценарію (див. §5)
  profile_id,                  -- транк
  max_concurrent INTEGER,      -- темп, див. §7
  created_at, started_at, finished_at,
  scenario_id, scenario_name,  -- звідки стартували; назва — знімок
  ivr_form TEXT                -- знімок вихідної форми для редагування з історії
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

UI — **рекурсивна параметризована форма** (еволюція первісного «рівня 1»,
червень 2026): дерево рівнів меню, де кожна опція — це клавіша + дія, а дія
«підменю» відкриває ще один такий самий рівень. Дії опції:
`operator` (bridge), `replay` (переграти контент рівня), `menu` (рівень
глибше), `play` (фраза + «потім»: stay/back/hangup), `back` (меню батька),
`home` (головне меню — з рівнів, куди `back` не дістає),
`optout`, `hangup`. Кожен текст має кнопку «Прослухати» (`/preview`).
Форма компілюється в JSON, який зберігається знімком у `campaign.ivr_flow`:

```json
{
  "start": "msg",
  "nodes": {
    "msg":    {"type": "play",   "prompt": "main", "next": "menu"},
    "menu":   {"type": "menu",   "prompt": "menu", "timeout_sec": 5, "max_repeats": 2,
               "branches": {"1": "to_op", "2": "msg", "0": "optout"},
               "on_timeout": "bye"},
    "to_op":  {"type": "bridge", "prompt": "connecting"},
    "optout": {"type": "play",   "prompt": "optout_ok", "mark": "optout", "next": "bye"},
    "bye":    {"type": "hangup"}
  },
  "prompts": {
    "main":       {"text": "<повідомлення>", "voice": "F3"},
    "menu":       {"text": "Щоб з'єднатися з оператором, натисніть один. ...", "voice": "F3"},
    "connecting": {"text": "Зачекайте, з'єднуємо з оператором", "voice": "F3"},
    "optout_ok":  {"text": "Вас видалено зі списку", "voice": "F3"}
  }
}
```

- Типи вузлів незмінні: `play`, `menu`, `bridge`, `hangup`. Вкладені меню —
  це просто гілки `menu`, що ведуть на інші `menu`-вузли: формат снапшота
  НЕ змінився з появою дерева, старі кампанії resume/retry без міграції.
- Імена вузлів/промптів вкладених рівнів — за шляхом цифр (`menu_3`,
  `text_3`, `info_3_1`); промпт контенту кореня завжди зветься `main` — на
  нього зав'язаний voicemail-drop (`ivr.CallContext.main_prompt`).
- `menu.prompt` — анонс опцій; програється на КОЖНОМУ раунді очікування
  цифри (без нього абонент чує мертву тишу — play_and_get_digits грає лише
  silence_stream). Компілятор (`app/flow.py`) генерує текст автоматично з
  опцій рівня (`announce_for_options`: цифри словами; опції `menu`/`play`
  описуються обовʼязковим `label`); форма може перекрити його полем
  `announce_text` на кожному рівні (§15).
- На старті кампанії всі `prompts` пререндеряться у WAV (кеш за
  hash(text+voice), каталог `audio/`); кампанія не стартує, якщо синтез
  упав.
- Серверна валідація: всі гілки ведуть на існуючі вузли, у `menu` є
  `on_timeout`, ліміт повторів ≤ 5, клавіші рівня унікальні, глибина дерева
  ≤ 4 (`flow.MAX_DEPTH`), промптів ≤ 40 (`flow.MAX_PROMPTS`), `back` на
  корені заборонений. Цикли в графі легальні (replay/back); рантайм обмежує
  кількість переходів (`ivr.run_flow`: `max(MAX_STEPS, 5 × вузлів)`).
- Інтерпретатор (`app/ivr.py`) не знає про форму — тільки про JSON, тож
  складніший редактор у майбутньому не зачіпає рантайм (дерево саме так і
  з'явилося — рантайм не мінявся).

## 6. Сценарій дзвінка (життєвий цикл номера)

1. Воркер бере pending-номер →
   `bgapi originate {originate_timeout=30,...}sofia/gateway/<gw>/<number> &socket(...)`.
2. Немає відповіді / зайнято / відмова провайдера → мапінг hangup cause:
   `USER_BUSY→busy`, `NO_ANSWER|ORIGINATOR_CANCEL|NO_USER_RESPONSE→no-answer`,
   `CALL_REJECTED|інше→failed` (сирий код — у `hangup_cause`).
3. Відповідь → FreeSWITCH конектиться до outbound-socket → **AMD**
   (`mod_amd`, дефолтні пороги в `fs/autoload_configs/amd.conf.xml`, тюнінг на
   реальних дзвінках; вердикт читається зі змінної каналу `amd_result`):
   - `MACHINE`: інфо-кампанія → чекати біп (`mod_avmd`), програти
     повідомлення → `voicemail-left`; операторська кампанія → hangup →
     `machine-hangup`;
   - `HUMAN` / `NOTSURE` → далі (сумнівних не скидаємо).
   У loopback-тестах (§16) вердикт симулюється змінною каналу
   `amd_test_result` (`uuid_setvar <a-leg> amd_test_result MACHINE`), як DTMF
   через `uuid_recv_dtmf`; реальна точність AMD — рівень 5.
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
  зареєстрований і не в активному мості. **Реалізація етапу 3 (переграно):**
  замість трекінгу подій `sofia::register` реєстрація перевіряється наживо
  запитом `sofia_contact <ext>@<domain>` (`app/operators.is_registered`) —
  запит не може розійтися з реальністю і переживає реконект ESL; «зайнятість»
  (in-bridge) контролер тримає у `OperatorPool` (acquire/release навколо
  bridge-вузла).
- Ліміти транка FlySIP (одночасні дзвінки, CPS) — з'ясувати у провайдера
  до етапу 5; `max_concurrent` обмежується цим значенням.

## 8. HTTP API (еволюція v1)

Без змін за духом: Basic Auth усюди (`WEB_USER`/`WEB_PASSWORD`).

| Маршрут | Зміни щодо v1 |
|---|---|
| `GET /` | той самий статичний UI |
| `POST /preview` | без змін (синтез + URL для плеєра) |
| `POST /start` | `scenario_id` + номери/профіль/`max_concurrent` (UI), або інлайн-поля IVR-форми (call.yml, старі клієнти); компіляція у `ivr_flow` |
| `GET/POST/DELETE /scenarios[/{id}]` | **нове**: бібліотека сценаріїв (назва, текст, голос, параметри, IVR-форма); збереження валідується dry-run-компіляцією |
| `GET /status` | + поля: `amd_result`-зведення, активні мости з операторами |
| `GET/POST/DELETE /config/profiles` | як у v1 (пароль → тільки `password_set`) |
| `GET/POST/DELETE /config/operators` | **нове**: CRUD операторів (ім'я, extension) |
| `GET /campaigns`, `GET /campaigns/{id}`, `…/retry-failed`, `…/resume` | як у v1; retry-failed не чіпає `optout` |

## 9. Веб-UI

Той самий підхід: один статичний `index.html`, чотири вкладки. Авторство
сценаріїв і запуск кампаній розділені: сценарії — підготовлені активи,
запуск — вибір активу + база номерів.

- **Кампанія** (запуск): селект збереженого сценарію + дайджест (текст,
  голос, тип, однорядкова схема меню) + ▶ прослухати + «✎ редагувати»
  (відкриває сценарій у редакторі); назва кампанії (порожньо = назва
  сценарію); SIP-профіль, `max_concurrent`, список номерів; живий прогрес
  (полінг `/status` 1.5 с) з лічильниками за всіма статусами §4.
- **Сценарії** (бібліотека + редактор): список (назва, тип, голос, дайджест
  меню; кнопки ✎ редагувати / ⧉ клонувати / 🗑); редактор: назва, тип,
  текст повідомлення, голос + параметри з прев'ю, IVR-редактор — рекурсивне
  дерево рівнів (§5): рядок опції = клавіша + дія + поля дії, «підменю»
  розгортає вкладений рівень зі своїм текстом/анонсом; плейсхолдер анонсу
  дзеркалить серверний автотекст; таймаут/повтори — глобальні поля.
  «Клонувати» відкриває копію «… (копія)» без id — швидкий схожий сценарій.
- **Налаштування**: SIP-профілі (як у v1) + список операторів.
- **Історія**: кампанії з лічильниками (+ з якого сценарію стартували),
  resume, retry-failed; розгортання до по-номерної таблиці зі
  статусом/AMD/DTMF.

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

Етапи 1–4 тестуються скриптом/`call.yml` без повного UI. Що з критеріїв
агент перевіряє сам, а що вимагає людини з телефоном — §16.

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
3. ~~**`greenswitch`** як ESL-клієнт~~ — **переграно на етапі 1**: власний
   asyncio-клієнт у `app/esl.py` (gevent-база greenswitch несумісна з
   uvicorn/asyncio в одному процесі).
4. **Один транк у `.env`** до етапу 5, потім UI-профілі з rescan.
5. Імена статусів/таблиць — українські назви лише в UI, у коді/БД англійські.

## 14. Джерела коду v1 (обов'язково для виконавця)

Ця гілка — orphan: коду v1 у робочому дереві **немає**, але він у цьому ж
репозиторії на гілці `main`. Не писати з нуля те, що вже є:

```bash
git show main:app/tts.py > app/tts.py   # ПЕРЕНОСИТЬСЯ ЯК Є (синтез + ресемплінг)
git show main:app/db.py                 # зразок: WAL, лок, public_profile (пароль → password_set)
git show main:app/main.py               # зразок: Basic Auth, /preview, валідація номерів
git show main:app/static/index.html     # зразок UI: вкладки, полінг /status, форми
git show main:Dockerfile                # зразок бейку моделі Supertonic (PJSIP-частини викинути)
git show main:ansible/deploy.yml        # деплой-патерн: .env через lineinfile, compose up
```

Для перегляду всього дерева v1 поруч: `git worktree add ../caller-v1 main`
(тільки читання, не комітити туди).

## 15. API-контракти (JSON)

Точні схеми, щоб реалізація не розходилася між сесіями. Загальне правило:
**будь-яке поле з паролем — тільки на запис, назад ніколи не повертається.**

### Сценарії (бібліотека)

- `GET /scenarios` → `{"scenarios": [{id, name, campaign_type, message, voice,
  voice_params, ivr, updated_at}]}` — повні обʼєкти, дайджест меню UI рахує сам.
- `POST /scenarios` (створити) / `POST /scenarios/{id}` (оновити) ←
  `{name, campaign_type, message, voice, voice_params, ivr}`; сервер робить
  dry-run `compile_form` — сценарій з помилками не зберігається; дубль назви
  → 409. `ivr` — рекурсивна форма (див. нижче), НЕ скомпільований граф.
- `DELETE /scenarios/{id}` — кампанії, що стартували з нього, зберігають
  знімок (`scenario_name`, `ivr_form`).

### POST /start

Запуск зі збереженого сценарію (основний шлях UI):

```json
{"scenario_id": 7, "name": "", "numbers": ["+380671234567"],
 "profile_id": 1, "max_concurrent": 1}
```

`name` порожній → кампанія успадковує назву сценарію. Альтернатива —
інлайн-поля (call.yml, старі клієнти):

```json
{
  "name": "Акція червня",
  "message": "Добрий день! ...",
  "voice": "F3",
  "voice_params": {"speed": 1.05, "steps": 8, "silence": 0.3},
  "numbers": ["+380671234567", "+380501112233"],
  "profile_id": 1,
  "campaign_type": "operator",
  "max_concurrent": 1,
  "ivr": {
    "timeout_sec": 5,
    "max_repeats": 2,
    "menu": {
      "announce_text": "",
      "options": [
        {"digit": "1", "action": "operator", "connect_text": ""},
        {"digit": "2", "action": "replay"},
        {"digit": "3", "action": "menu", "label": "Графік роботи",
         "menu": {
           "text": "Працюємо з девʼятої до вісімнадцятої.",
           "announce_text": "",
           "options": [
             {"digit": "1", "action": "play", "label": "Субота",
              "text": "У суботу з десятої до пʼятнадцятої.", "then": "stay"},
             {"digit": "9", "action": "back"}
           ]
         }},
        {"digit": "0", "action": "optout", "confirm_text": ""}
      ]
    }
  }
}
```

`ivr.menu` — рекурсивне дерево рівнів (§5). Рівень: `text` (контент, грає
один раз при вході; на корені ігнорується — там грає `message`),
`announce_text` (порожній → автотекст з опцій, `flow.announce_for_options`),
`options`, опційні `timeout_sec`/`max_repeats` (дефолт — глобальні з `ivr`).
Опція: `digit` (0–9, унікальна в межах рівня) + `action`
(`operator|replay|menu|play|back|home|optout|hangup`; `back` — меню батька,
`home` — головне меню, обидва поза коренем); для `play` — `text` і
`then` (`stay|back|hangup`), для `menu` — вкладений `menu`; `label`
обовʼязковий для `menu`/`play`, коли анонс рівня автогенерується.
`menu.options: []` → виродження у програти-повідомлення-і-покласти (v1).

**Сумісність:** стара пласка форма (`operator/repeat/optout/menu_text` —
перша версія цього контракту) досі приймається і конвертується у дерево
(`flow._legacy_menu`) — нею користується `ansible/call.yml`.

`voice_params` — необовʼязкові параметри синтезу, clamp-яться сервером
(`tts.clamp`): `speed` 0.7–1.3 (дефолт 1.05; модель формально приймає до 2.0,
але вище ~1.3 мовчки ковтає слова, нижче 0.7 — кидає ValueError), `steps`
1–32 (кроки дифузії,
дефолт 8), `silence` 0.0–2.0 с (пауза між реченнями, дефолт 0.3). Компілятор
кладе їх у кожен `prompt` снапшота (`{"text", "voice", "speed", "steps",
"silence"}`), тож resume/retry синтезують ідентично; вони ж — частина ключа
WAV-кешу (`jobs.prompt_path`).

Відповіді: `200 {"campaign_id": 7}`; `409 {"detail": "campaign already
running"}`; `400` з описом помилки валідації (номери, IVR-форма, синтез).
Сервер компілює `ivr` у JSON-сценарій §5 і зберігає знімком у
`campaign.ivr_flow`.

### GET /status

```json
{
  "campaign_id": 7,
  "name": "Акція червня",
  "phase": "running",
  "total": 10,
  "counts": {"pending": 4, "ringing": 1, "answered": 2, "transferred": 1,
             "missed-operator": 0, "voicemail-left": 0, "machine-hangup": 1,
             "no-answer": 1, "busy": 0, "failed": 0, "optout": 0},
  "current": {"number": "+380671234567", "state": "ivr"},
  "operators": [{"extension": "1001", "name": "Іван", "registered": true, "busy": false}],
  "log": ["10:02:11 +380671234567 answered (AMD=HUMAN)"]
}
```

`phase`: `idle|running|done|interrupted|stopped`;
`current.state`: `dialing|ringing|amd|ivr|bridged`. Без активної кампанії —
знімок останньої (як у v1).

### Оператори

- `GET /config/operators` → `[{"id": 1, "name": "Іван", "extension": "1001", "registered": true}]`
- `POST /config/operators` ← `{"name": "Іван", "extension": "1001", "password": "..."}` —
  контролер пише запис у БД, генерує `directory/default/1001.xml`, робить
  `reloadxml`.
- `DELETE /config/operators/{id}`

### Кампанії та профілі

Як у v1: `GET /campaigns` → список з лічильниками;
`GET /campaigns/{id}` → деталі + `"numbers": [{"number", "status",
"amd_result", "dtmf", "hangup_cause", "attempts"}]`;
`POST /campaigns/{id}/retry-failed` (не чіпає `optout`),
`POST /campaigns/{id}/resume`; `GET/POST/DELETE /config/profiles` (назад —
лише `password_set`).

## 16. Перевірка без реальних дзвінків

Реальний дзвінок вимагає Linux-хоста з публічним IP, кредів FlySIP і людини
з телефоном. Усе інше перевіряється локально (зокрема на macOS) — і виконавець
зобов'язаний проганяти ці рівні сам, **не заявляючи неперевірене перевіреним**:

1. **pytest, без FreeSWITCH**: компіляція IVR-форми → JSON §5; валідація
   сценарію (биті гілки, цикли, ліміти); мапінг hangup cause → статус;
   шар БД (лічильники, retry, optout).
2. **Контейнери живі**: `docker compose up` →
   `fs_cli -x "status"` відповідає; контролер під'єднався до ESL
   (`api status` через ESL проходить); `GET /` віддає UI за Basic Auth.
3. **Конфіг валідний**: `fs_cli -x "sofia status"` показує external-профіль;
   з реальними кредами gateway у стані `REGED`, без них достатньо, що профіль
   піднявся і gateway з'явився.
4. **E2E без транка — loopback**: тестовий режим набору (env
   `DIAL_STRING_TEMPLATE`, дефолт `sofia/gateway/{gw}/{number}`, у тесті
   `loopback/9999/default`): `originate loopback/9999 &socket(...)` ганяє
   повний сценарій §6 усередині FreeSWITCH без жодного зовнішнього дзвінка;
   DTMF симулюється **`uuid_recv_dtmf <a-leg-uuid> 1`** (не `uuid_send_dtmf`:
   той шле цифру в бік віддаленої сторони; а loopback-канали взагалі не
   генерують DTMF-подій, тож меню читає цифри через `play_and_get_digits`,
   який споживає вхідну чергу каналу — куди `uuid_recv_dtmf` і кладе);
   перевіряються переходи статусів у БД (`answered`, `transferred`-гілка до
   моменту bridge, `optout`...).
5. **Людина з телефоном** (чек-лист для користувача, агент сюди не претендує):
   чутність і гучність повідомлення, lead-in, реальне натискання «1» і розмова
   з оператором через софтфон, поведінка AMD на реальній голосовій пошті,
   NAT/RTP на бойовому хості.

Рівні 1–4 — обов'язкові критерії приймання етапів 1–4 з §11 у частині,
яку виконує агент; рівень 5 закриває етап остаточно.
