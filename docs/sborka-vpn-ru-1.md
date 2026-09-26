# Сборка vpn-ru-1

Зафиксировано 2026-09-27. Это журнал шагов, которые привели сервер `vpn-ru-1` к текущей панели Hiddify `12.3.3`. Секреты, UUID панели, пути панели, пароли и ключи здесь не записаны: они остаются в локальном inventory и в дампе панели.

## Где лежит снимок

Команда снятия:

```bash
cd /opt/hiddify-manager/hiddify-panel
hiddifypanel backup
```

Команду запускать из каталога панели: иначе CLI не видит `app.cfg`. Файл появляется в `backup/ГГГГ_ММ_ДД__ЧЧ_ММ_СС.json` относительно этого каталога.

Постоянные копии текущего состояния:

- на сервере: `/var/backups/hiddify/vpn-ru-1-configured.json`, режим `600`, владелец root
- локально: `samples/initial/hiddify-vpn-ru-1-2026-09-27.json`

Оба файла в git не входят. `samples/initial/` закрыт в `.gitignore`. Из дампа не восстанавливать пользователей, администраторов, домены, пути панели и приватные ключи. Протоколы для следующих серверов по-прежнему из `samples/protocols.json`.

В дампе: 1 дочерний узел, 6 доменов, 150 настроек, 155 строк прокси, один пользователь `default`.

## 1. Карточка сервера

- `id`: `vpn-ru-1`
- облако: Yandex Cloud, Ubuntu 22.04 LTS
- публичный адрес: `84.201.133.81`
- внутренний адрес: `10.128.0.22`, подсеть `default-ru-central1-a`
- вход: SSH-ключ, пользователь `wladmin`, порт 22
- имя переменной с путём ключа: `SSH_KEY_PATH_VPN_RU_1`
- root по этому ключу даёт доступ, затем отказывает в shell. Учётки `ubuntu`, `yc-user` и `NONE` этот ключ не принимает
- `sudo` у `wladmin` без пароля
- группа `ru`, локация `ru`

## 2. Подготовка ОС

Скрипт `scripts/install-hiddify.sh`. Система только Ubuntu 22.04 (jammy): для неё в PPA есть HAProxy 3.0.

До панели:

1. `apt-get update` и `upgrade`, старые конфиги apt не перезаписывать.
2. IPv6 выключен постоянно через `/etc/sysctl.d/99-disable-ipv6.conf`, не разовым `sysctl -w`.
3. Swap один раз, если его ещё нет. На этой машине создан файл 1 ГБ, строка в `fstab` одна. Повторный `fallocate` меньшего размера не запускать: он затирает первый файл.
4. HAProxy из `ppa:vbernat/haproxy-3.0`, пакет точно `3.0`, не маска `haproxy=3.0.*`: apt такую маску не принимает. После установки `apt-mark hold`.

## 3. Установка панели

Версия всегда `v12.3.3`. Канал `release` и короткий адрес «последняя версия» не использовать.

Рабочий запуск:

```bash
NEEDRESTART_MODE=l bash <(curl -fsSL https://raw.githubusercontent.com/hiddify/Hiddify-Manager/refs/tags/v12.3.3/common/download.sh) v12.3.3 --no-gui
```

`--no-gui` нужен без TTY: иначе установщик падает на urwid. `NEEDRESTART_MODE=l` обязателен. Первый прогон записал `VERSION` 12.3.3, после чего needrestart перезапустил unit установки, и второй заход увидел «уже установлено» и не доставил панель. Если `/opt/hiddify-manager/VERSION` уже `12.3.3`, а службы `hiddify-panel`, `hiddify-nginx`, `hiddify-haproxy`, `hiddify-xray`, `hiddify-singbox` не активны, установщик снова не гонять по короткому пути «уже стоит»: довести его тем же тегом `--no-gui` и без имени unit, которое needrestart умеет перезапускать. Другую версию поверх этой не ставить без фразы «переустанови».

После установки:

- `auto_update` = false
- `package_mode` = `v12.3.3`
- CLI: `/opt/hiddify-manager/.venv313/bin/hiddifypanel`
- панель слушает `127.0.0.1:9000`

`admin_uuid` и proxy path записаны только в локальный `inventory.yml`.

## 4. DNS

Зоны из `domains.yml`: `sdfsdfsdfsd.store`, `losttv.site`, `linkusers3.online`. Токен Cloudflare только в локальном `.env`.

У сервера `subdomain: vp22` и `cloudflare_zones: all`. Созданы A-записи на `84.201.133.81`, прокси Cloudflare выключен:

- `vp22.sdfsdfsdfsd.store`
- `vp22.losttv.site`
- `vp22.linkusers3.online`

Корень зоны не занят.

## 5. Домены в панели

Добавлены как `direct`, не только для подписки. От установщика оставлены и не удалялись:

- `84.201.133.81`, direct
- `84.201.133.81.sslip.io`, direct
- `plus.im`, special_reality_tcp

Фактический UDP-порт Hysteria2 и TUIC равен базе из настроек плюс id домена. База: Hysteria2 `16191`, TUIC `35630`. `plus.im` в подписку этих портов не отдаёт.

| Домен | Hysteria2 | TUIC |
| --- | --- | --- |
| `84.201.133.81` | 16192 | 35631 |
| `84.201.133.81.sslip.io` | 16193 | 35632 |
| `vp22.sdfsdfsdfsd.store` | 16195 | 35634 |
| `vp22.losttv.site` | 16196 | 35635 |
| `vp22.linkusers3.online` | 16197 | 35636 |

Сертификаты Let's Encrypt на три имени `vp22`, срок с 26 сентября по 25 декабря 2026. Имя в сертификате совпадает с доменом.

## 6. Протоколы

Источник: `samples/protocols.json`, снятый с чужой панели только как набор флагов и строк прокси. Пользователи и домены оттуда не переносились.

Включено: VLESS, TCP, HTTP/2, WebSocket, Hysteria2, TUIC, SSH, WireGuard, QUIC, HTTP proxy, obfs Hysteria.

Выключено: VMess, Trojan, Reality, gRPC, HTTPUpgrade, XHTTP, Naive, Mieru, DNSTT, V2Ray, Shadowsocks 2022, ShadowTLS, SSR.

Прочие порты из тех же настроек: TLS `443`, HTTP `80`, SSH-прокси `40362`, WireGuard `22745`, special `22034`. Ядро `xray`. Страна панели осталась `ir`, как в образце.

Применение: залить профиль и выставить эти флаги в базе панели, затем:

```bash
/opt/hiddify-manager/apply_configs.sh --no-gui --no-log
```

Первый apply записал Hysteria2 и TUIC на случайные порты, подписка при этом рекламировала базу плюс id домена. Второй запуск того же `apply_configs.sh` переписал конфиги sing-box на порты из таблицы выше. Процесс не обрывать, пока скрипт сам не выйдет.

WireGuard после apply слушал случайный порт, подписка отдавала `22745`. В `/etc/wireguard/hiddifywg.conf` выставлен `ListenPort = 22745`, служба `wg-quick@hiddifywg` перезапущена, `wg show` подтвердил `22745`.

Дополнительных пользователей не создавали. Остался пользователь `default`, которого панель создаёт при установке.

## 7. Сеть

На госте ufw выключен, политика INPUT — ACCEPT. Пакеты режет группа безопасности Yandex Cloud `default-sg-enp46h1a7fp8up2heis4`. Входящие правила дописаны вручную в консоли, не через API:

- Any с `0.0.0.0/0`
- UDP 443
- UDP `16192-16197`
- UDP `35631-35636`
- UDP `22745`

Исходящие: Any на `0.0.0.0/0`.

Проверка снаружи: TCP `22`, `80`, `443`, `40362` открываются. UDP `16192`, `16196`, `35631`, `35635` и `22745` доходят до `10.128.0.22` на `eth0`.

## 8. Что видно в клиенте

Обновить подписку пользователя `default`. Рабочие строки: VLESS по TLS и TCP, TUIC, Hysteria2, SSH и WireGuard на IP, `sslip.io`, `vp22.sdfsdfsdfsd.store` и `vp22.linkusers3.online`. Балансировщики клиента тоже отвечают.

Не проходят проверка клиента:

- VLESS с HTTP/2 и VLESS с WebSocket на всех именах, хотя порт 443 снаружи принимает ALPN `h2`
- все строки `vp22.losttv.site`, хотя сертификат этого имени валиден и UDP `16196`/`35635` до машины доходят

Для подключения брать `vp22.linkusers3.online` или `vp22.sdfsdfsdfsd.store`.
