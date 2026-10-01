# Инструкция: сборка сервера по образцу

Подробный runbook, как довести **один** VPS до состояния «как эталон парка»:
Hiddify Manager **12.3.3** + DNS + протоколы из `samples/protocols.json` + (опционально) restore users/admins/paths.

Веб-версия: [`docs/web/obrazec.html`](web/obrazec.html) · общая база знаний: [`docs/web/index.html`](web/index.html).

---

## 0. Что считается «образцом»

| Компонент | Значение |
|-----------|----------|
| ОС | Ubuntu **22.04 jammy** |
| Панель / manager | **12.3.3** (`v12.3.3`), не канал `release` |
| `auto_update` | `false` |
| `package_mode` | `v12.3.3` |
| `admin_lang` / `lang` | `ru` |
| `country` | `other` |
| `first_setup` | `false` |
| Протоколы | только из `samples/protocols.json` |
| Домены | зоны только из `domains.yml`, метка `subdomain` из карточки |
| Режим домена | обычно `direct`; для CDN — `cdn` + Cloudflare `proxied: true` |
| Пароль владельца панели | Свой длинный на каждую панель → HiddifySales `panel_admin_password` (в ответ API не возвращается). Временный из `HIDDIFY_ADMIN_PASSWORD` только на установку, затем ротация |
| Пароль SSH | Свой длинный на сервер → `.env` (`password_env`) и HiddifySales `ssh_password` |
| Часовой пояс ОС | `Etc/GMT-4` (UTC+4, без летнего времени) |
| Чистка диска | `scripts/deploy-disk-hygiene.py` + cron (~04:xx), в т.ч. обнуление `/var/log/btmp` |
| Пользователи | не создавать «на всякий случай»; restore из бэкапа **этой** машины, если просили |

Полный дамп чужой панели (`samples/initial/` и т.п.) **не** источник протоколов и доменов.

---

## 1. Входные данные (до любой команды)

1. **Селектор** в inventory: `id` (например `vpn-vp2`). Без селектора на парк не идти.
2. Карточка сервера после merge с `defaults`:
   - `host`, `ssh_user`, `ssh_port`
   - `auth.method` + имя env с секретом (`password_env` / `key_path_env`)
   - `subdomain`, `cloudflare_zones` (`all` или список)
   - `desired.domain_modes` — `[direct]` или `[cdn]`
3. Локальный `.env`: SSH-секрет, `CLOUDFLARE_API_TOKEN`, `HIDDIFY_ADMIN_PASSWORD`.
4. Если нужен restore: файл бэкапа панели **этой** машины в `backups/<id>/` (или свежий дамп), минимум:
   - `admin_users`, `users`
   - в `hconfigs` — `proxy_path`, `proxy_path_admin`, `proxy_path_client`

Секреты в git, чат-отчёт и docs **не** писать — только имена переменных.

---

## 2. Предусловия на сервере

Проверить по SSH способом из `auth` карточки:

```bash
. /etc/os-release && echo "$VERSION_CODENAME"   # должно быть jammy
test -f /opt/hiddify-manager/VERSION && cat /opt/hiddify-manager/VERSION || echo NO_HIDDIFY
```

| Факт | Действие |
|------|----------|
| Нет Hiddify | ставить по §3 |
| `VERSION=12.3.3` | установщик не перезапускать; идти к DNS/профилю при необходимости |
| Другая версия (напр. 13.x) | **стоп**, пока пользователь не скажет «переустанови» (часто = чистая ОС) |
| Нет SSH | чинить доступ (порт, пароль, SOCKS/jump) — без SSH образец не собрать |

Особые случаи доступа (зафиксировано в парке):

- `vpn-vp6` — SSH порт **22022**
- `vpn-vp2` — прямой :22 может быть закрыт; вход через jump `192.168.1.152` и SOCKS **1084/1085** (слушают только `127.0.0.1` на jump)

---

## 3. Установка Hiddify 12.3.3

Только скрипт репозитория (идемпотентен):

```bash
# с рабочей машины: залить LF-версию скрипта
scp scripts/install-hiddify.sh root@HOST:/tmp/install-hiddify.sh
ssh root@HOST 'sed -i "s/\r$//" /tmp/install-hiddify.sh && chmod 700 /tmp/install-hiddify.sh && bash /tmp/install-hiddify.sh'
```

Долгий прогон лучше в фоне с логом:

```bash
setsid bash /tmp/install-hiddify.sh > /var/log/hiddify-install.log 2>&1 < /dev/null &
echo $! > /var/log/hiddify-install.pid
tail -f /var/log/hiddify-install.log
```

Скрипт сам:

1. Проверяет jammy и отсутствие «чужой» версии.
2. `apt update/upgrade` (один раз, маркер `/var/log/hiddify-apt-upgraded.ok`).
3. Отключает IPv6 через `/etc/sysctl.d/99-disable-ipv6.conf`.
4. Поднимает swap (1G или 300M), если его ещё нет.
5. Ставит HAProxy **3.0** из PPA `vbernat/haproxy-3.0`, `apt-mark hold`.
6. Качает Hiddify по тегу `v12.3.3` (не release).
7. `lock_panel`: `auto_update=false`, `package_mode=v12.3.3`, языки `ru`, `country=other`, `first_setup=false`.

Успех:

```text
Hiddify 12.3.3 installed
# или
Hiddify 12.3.3 already installed
cat /opt/hiddify-manager/VERSION   # 12.3.3
```

После установки временно можно выставить пароль владельца из `HIDDIFY_ADMIN_PASSWORD`. **До сдачи сервера** обязательна ротация: свой длинный пароль панели и свой длинный SSH, запись в локальный `.env` и в HiddifySales (`panel_admin_password`, `ssh_password`). См. §7.

---

## 4. DNS (Cloudflare)

Зоны только из `domains.yml`:

- `sdfsdfsdfsd.store`
- `losttv.site`
- `linkusers3.online`

Для каждой выбранной зоны создать/обновить **A**-запись:

| Поле | direct | cdn |
|------|--------|-----|
| name | `{subdomain}` | `{subdomain}` |
| content | `host` сервера | `host` сервера |
| proxied | **false** (DNS only) | **true** (оранжевое облако) |
| ttl | 120 или auto | auto (1) при proxied |

Корень зоны не занимать. Пустой `subdomain` — DNS не трогать.

Токен: `CLOUDFLARE_API_TOKEN`. Без токена DNS не угадывать.

Пример имён при `subdomain: vp2` и `cloudflare_zones: all`:

- `vp2.sdfsdfsdfsd.store`
- `vp2.losttv.site`
- `vp2.linkusers3.online`

---

## 5. Профиль: домены + протоколы

Источник протоколов: **`samples/protocols.json`**.

На сервере (после бэкапа панели):

1. Залить `/tmp/protocols.json`.
2. В Python/CLI панели:
   - добавить IP сервера как domain (если ещё нет);
   - добавить домены `{subdomain}.*` с режимом:
     - `DomainType.direct` или
     - `DomainType.cdn` (если `desired.domain_modes: [cdn]`);
   - применить `hconfigs` и `proxies` из JSON (bool/str + enable флагов proxy);
   - снова выставить lock-поля: `auto_update`, `package_mode`, `admin_lang`, `lang`, `country`, `first_setup=false`.
3. Снять карточку: `admin_uuid`, `proxy_path_admin`, `proxy_path_client` → в inventory.
4. Применить конфиги:

```bash
/opt/hiddify-manager/apply_configs.sh --no-gui --no-log
# при сомнении — второй прогон
/opt/hiddify-manager/apply_configs.sh --no-gui --no-log
```

5. Ещё раз явно: `first_setup=false`.

Не тащить из бэкапа: чужие домены, reality keys, полный `set_settings=True`.

---

## 6. Секреты, часовой пояс, чистка диска

Обязательный финал каждой сборки по образцу (до `state: configured`):

1. **Пароль владельца панели** — сгенерировать длинный случайный (≥24), `AdminUser.update_password`, сохранить локально (не в git), `PATCH` биллинга:
   ```json
   { "panel_admin_password": "<secret>" }
   ```
   В ответе только `has_panel_admin_password: true`. Общий `HIDDIFY_ADMIN_PASSWORD` на сданной панели не оставлять.
2. **Пароль SSH** (если `auth.method: password`) — сгенерировать длинный случайный, `chpasswd` на сервере, обновить `.env` (`password_env`), проверить вход новым паролем, `PATCH`:
   ```json
   { "ssh_password": "<secret>" }
   ```
   В ответе только `has_ssh_password: true`. Входной пароль провайдера после сборки не оставлять.
3. **Часовой пояс ОС**:
   ```bash
   timedatectl set-timezone Etc/GMT-4
   date +%z   # ожидание +0400
   ```
4. **Ежедневная чистка диска**:
   ```bash
   python scripts/deploy-disk-hygiene.py --no-test <id>
   ```
   На сервере: `/opt/hiddify-manager/scripts/disk-hygiene/disk-hygiene.sh`, cron `/etc/cron.d/hiddify-disk-hygiene`. Скрипт чистит journal/apt/логи, обнуляет `/var/log/btmp`, режет старые panel/var backups. Если прямой SSH timeout — выкладка через jump/SOCKS (как vp2/vp8/vp13).

Секреты в git, docs и Telegram не писать.

---

## 7. Restore users / admins / paths

Только если до установки на сервере не было никакой версии Hiddify. Если каталог `/opt/hiddify-manager`, файл `VERSION` или сервис `hiddify-*` уже есть, сборка по образцу не стартует и ничего не меняет. Restore после строки установщика `Hiddify 12.3.3 installed` в этом же прогоне. На живую старую версию пользователей не накатывать.

Кнопка в HiddifySales: `C:\Users\1\Documents\fullvpnservice\docs\HIDDIFY_REINSTALL_BY_SAMPLE.md`.

Нужен, если на машине уже были клиенты/CRM и есть бэкап.

Подготовить JSON (локально можно срезать до нужных ключей):

```json
{
  "admin_users": [ ... ],
  "users": [ ... ],
  "hconfigs": {
    "proxy_path": "...",
    "proxy_path_admin": "...",
    "proxy_path_client": "..."
  }
}
```

На сервере:

```bash
export BACKUP_JSON=/tmp/restore-users.json
export ADMIN_PASS_FILE=/tmp/admin.pass          # временный; после restore снова ротация (§6) + Sales
export SERVER_ID=vpn-vpN                        # id из inventory
bash /tmp/restore-users-admins.sh
```

Скрипт `scripts/restore-users-admins.sh`:

1. Бэкап текущего состояния в `/var/backups/hiddify/`.
2. Восстанавливает **только** admins + users (`set_settings=False`, домены/прокси не трогает).
3. Пишет три path из бэкапа (чистая установка генерирует новые — без этого CRM/подписки ломаются).
4. Сбрасывает пароль владельца из `ADMIN_PASS_FILE` (далее снова свой длинный + Sales).
5. `apply-users` + полный `apply_configs.sh` (path меняет HAProxy/nginx maps).

Записать в inventory восстановленные `admin_uuid`, `admin_proxy_path`, `client_proxy_path`.

### Если админка 400 / decoy после restore

1. Проверить, что в БД paths = из бэкапа (при рассинхроне — `systemctl restart hiddify-redis hiddify-panel`).
2. Ещё один `apply_configs.sh`.
3. `systemctl restart hiddify-haproxy` (или `haproxy`).
4. Проверить maps: `/opt/hiddify-manager/haproxy/maps/path*` содержат admin path.

---

## 8. Проверки (обязательные)

| Проверка | Ожидание |
|----------|----------|
| `cat /opt/hiddify-manager/VERSION` | `12.3.3` |
| `auto_update` | `false` |
| `package_mode` | `v12.3.3` |
| `first_setup` | `false` |
| Админка | Dashboard, не `/admin/quick-setup/` |
| URL админки | `https://{domain}/{proxy_path_admin}/{admin_uuid}/` |
| API | `…/{proxy_path_admin}/api/v2/admin/server_status/` + header `Hiddify-API-Key: {admin_uuid}` → 200 |
| DNS | A на `host`; для CDN — proxied=true |
| CDN без UUID в path | часто 400; с UUID — 200 |
| Подписка пользователя | не пустая (если users восстановлены) |
| `date +%z` | `+0400` |
| disk-hygiene | скрипт + cron на месте |
| HiddifySales | `has_ssh_password` / `has_panel_admin_password` = true (сами значения не в ответе) |

Inventory: `state: configured`, обновить `status_note` / `status_updated_at`, `panel_domain`, paths, uuid.

---

## 9. Чеклист одной строкой

```text
[ ] карточка inventory + секреты в .env
[ ] SSH ок (порт/ключ/пароль/socks)
[ ] jammy, нет чужой версии (или «переустанови»)
[ ] install-hiddify.sh → 12.3.3 + lock_panel
[ ] DNS A × зоны (direct DNS-only / cdn proxied)
[ ] protocols.json + домены (direct|cdn) + apply_configs
[ ] first_setup=false
[ ] свой SSH + пароль панели → .env + HiddifySales
[ ] timezone Etc/GMT-4 (+0400)
[ ] deploy-disk-hygiene.py на этот id
[ ] (опц.) restore-users-admins.sh + maps ok + снова ротация пароля панели
[ ] проверки UI/API/DNS
[ ] inventory state=configured + docs/web data при изменении парка
```

---

## 10. Чего не делать

- Ставить `i.hiddify.com/release` или свежий release поверх 12.3.3.
- Ставить поверх другой версии без фразы **«переустанови»**.
- Брать протоколы/домены из полного бэкапа чужой панели.
- Менять SSH-ключи/порт ОС без отдельной задачи.
- Оставлять на сданной панели общий `HIDDIFY_ADMIN_PASSWORD` или пароль провайдера.
- Массово создавать юзеров «на всякий случай».
- Открывать все порты firewall.
- Писать пароли, токены, полные admin URL с секретами в git/docs.

---

## 11. Связанные файлы

| Файл | Роль |
|------|------|
| `scripts/install-hiddify.sh` | установка + lock |
| `scripts/restore-users-admins.sh` | users/admins + 3 path |
| `scripts/deploy-disk-hygiene.py` | выкладка чистки диска + cron |
| `scripts/disk-hygiene/disk-hygiene.sh` | ежедневная чистка (в т.ч. btmp) |
| `samples/protocols.json` | профиль протоколов |
| `domains.yml` | разрешённые зоны |
| `inventory.yml` | карточки (локально) |
| `docs/web/index.html` | база знаний парка |
| `scripts/check-fleet-ssh.ps1` | проверка SSH по парку |

Обновлять эту инструкцию при изменении пайплайна (правило `.cursor/rules/docs-web.mdc`).
