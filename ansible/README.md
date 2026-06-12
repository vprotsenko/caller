# Ansible — деплой і керування Дзвонилкою 2.0

Хост — **Linux із публічним IP** (вимога SIP/RTP; на macOS реальні дзвінки не
працюють). Вкажи його в [inventory.ini](inventory.ini).
`ansible.cfg` тримає один SSH-конект на весь плейбук (ControlMaster) +
pipelining — менше оверхеду на кожен таск.

## Який плейбук коли (швидкість)

| Що змінив | Плейбук | Час | Що робить |
|---|---|---|---|
| Python-код у `app/` | `redeploy-app.yml` | ~10-30 c | rebuild+restart **лише** app; FreeSWITCH живий |
| Конфіг `fs/` (sofia, gateway, sip-trace) | `reload-fs.yml` | ~5 c | sync + `reloadxml` + restart sofia-профілю, **без** рестарту контейнера |
| `fs/modules.conf` (новий модуль) | `reload-fs.yml -e hard=1` | ~15-20 c | рестарт контейнера FreeSWITCH |
| Все / `requirements.txt` / перший раз | `deploy.yml` | повний | sync усього + build + перестворює лише змінений сервіс |

```bash
cd caller/ansible

# Швидко: змінив код контролера
ansible-playbook redeploy-app.yml

# Швидко: змінив конфіг FreeSWITCH (sip-trace, gateway, кодеки тощо)
ansible-playbook reload-fs.yml
ansible-playbook reload-fs.yml -e hard=1     # якщо мінявся modules.conf

# Повний деплой (перший раз / зміна залежностей)
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
