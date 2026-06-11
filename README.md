# Дзвонилка 2.0 (FreeSWITCH + ESL)

Autodialer комерційного рівня: TTS-повідомлення (Supertonic, українська),
IVR з DTMF, перемикання на оператора, AMD, повна статистика по кампаніях.
Технічне завдання — [Plan.md](Plan.md); правила для агента — [CLAUDE.md](CLAUDE.md).

**Стан: етапи 1–5 реалізовані** (PoC → IVR-движок → оператор/bridge → AMD →
повний UI з кампаніями, історією, профілями й операторами + Ansible). Перевірені
рівні §16 1–4; реальні дзвінки/аудіо/AMD на живій пошті — рівень 5 (людина з
телефоном на Linux-хості).

## Запуск

```bash
cp .env.example .env      # заповнити WEB_PASSWORD, ESL_PASSWORD, SIP_* (FlySIP)
docker compose up --build # Linux-хост із публічним IP (бойовий режим)
```

Локальна розробка на macOS (без реальних дзвінків — тільки UI/прев'ю/loopback):

```bash
docker compose -f docker-compose.yml -f docker-compose.macos.yml up --build
```

UI: http://localhost:8000 (Basic Auth — `WEB_USER`/`WEB_PASSWORD` з `.env`).
Три вкладки: **Кампанія** (повідомлення + IVR-форма + набір + живий прогрес),
**Налаштування** (SIP-профілі + оператори), **Історія** (кампанії, деталі по
номерах, resume/retry-failed).

## Реальний AMD (опційно)

Базовий FreeSWITCH-образ не містить `mod_amd`; без нього кожна відповідь
трактується як HUMAN (дзвінок не скидається). Для класифікації автовідповідачів:

```bash
docker build -f Dockerfile.freeswitch -t caller-freeswitch:amd .  # крихка збірка з сирців
echo 'FREESWITCH_IMAGE=caller-freeswitch:amd' >> .env
docker compose up -d
```

## Перевірка (Plan.md §16)

```bash
# рівень 1: юніт-тести без FreeSWITCH
docker compose run --rm app pytest -q

# рівень 2-3: живі контейнери і валідний конфіг
docker compose exec freeswitch fs_cli -p "$ESL_PASSWORD" -x "status"
docker compose exec freeswitch fs_cli -p "$ESL_PASSWORD" -x "sofia status"

# рівень 4: E2E без транка (у .env: DIAL_STRING_TEMPLATE=loopback/{number}/default)
#   запусти кампанію з UI на номер 9999; DTMF/AMD симулюються
#   uuid_recv_dtmf <a-leg> 1   та   originate-змінною amd_test_result
```

Деплой і керування з командного рядка — [ansible/](ansible/) (deploy / call / status).

## Секрети

`.env` і `data/` (SQLite із SIP- і операторськими паролями плейнтекстом) не
комітяться і захищаються правами доступу. Згенеровані `fs/directory/default/*.xml`
(паролі операторів) теж gitignored. Basic Auth без TLS — до продакшену
поставити reverse proxy з TLS (Plan.md §12).
