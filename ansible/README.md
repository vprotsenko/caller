# Ansible — деплой і керування Дзвонилкою 2.0

Хост — **Linux із публічним IP** (вимога SIP/RTP; на macOS реальні дзвінки не
працюють). Вкажи його в [inventory.ini](inventory.ini).

```bash
cd caller/ansible

# 1) Деплой (Docker + sync + .env + compose up -d --build).
#    ESL_PASSWORD генерується й зберігається в .env на сервері.
ansible-playbook deploy.yml
#    Для реального AMD: зібрати образ із mod_amd і вказати його:
ansible-playbook deploy.yml -e freeswitch_image=caller-freeswitch:amd

# 2) Тестова кампанія (creds Basic Auth читаються з /opt/caller/.env)
ansible-playbook call.yml \
  -e 'message=Добрий день! Це тест.' -e 'numbers=+380671234567' \
  -e campaign_type=operator -e ivr_operator=true

# 3) Стежити до завершення
ansible-playbook status.yml -e wait=1
```

`call.yml` шле JSON-контракт `/start` (Plan.md §15): `numbers` — через кому,
прапори IVR `ivr_operator|ivr_repeat|ivr_optout`. SIP-профіль — дефолтний у БД
(сіється з `SIP_*` при першому старті) або `-e profile_id=N`.

Секрети (`SIP_PASSWORD`, веб-пароль) ідуть із `no_log: true` і не потрапляють
у вивід Ansible. `.env` оновлюється порядково — значення, додані вручну на
сервері, переживають редеплой.
