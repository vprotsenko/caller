# Дзвонилка 2.0 (FreeSWITCH + ESL)

Autodialer комерційного рівня: TTS-повідомлення (Supertonic, українська),
IVR з DTMF, перемикання на оператора, AMD. Технічне завдання — [Plan.md](Plan.md);
правила для агента-виконавця — [CLAUDE.md](CLAUDE.md).

**Стан: етап 1 (PoC) — FreeSWITCH + ESL + originate з playback.**
Етапи (Plan.md §11): 1 PoC → 2 IVR-движок → 3 оператор/bridge → 4 AMD → 5 повний UI.

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

## Перевірка (Plan.md §16)

```bash
# рівень 1: юніт-тести без FreeSWITCH
docker run --rm caller-app pytest -q

# рівень 2-3: живі контейнери і валідний конфіг
docker compose exec freeswitch fs_cli -p "$ESL_PASSWORD" -x "status"
docker compose exec freeswitch fs_cli -p "$ESL_PASSWORD" -x "sofia status"

# рівень 4: E2E без транка (у .env: DIAL_STRING_TEMPLATE=loopback/{number}/default)
curl -u admin:... -X POST http://localhost:8000/call \
  -F number=9999 -F text="Тестове повідомлення" -F voice=F3
```

Реальний дзвінок (рівень 5) — людина з телефоном, Linux-хост, креди FlySIP
у `.env`, `DIAL_STRING_TEMPLATE` за замовчуванням.

## Секрети

`.env` і `data/` (зʼявиться на етапі 2: SQLite із SIP-паролями плейнтекстом)
не комітяться і захищаються правами доступу. Basic Auth без TLS — до
продакшену поставити reverse proxy з TLS (Plan.md §12).
