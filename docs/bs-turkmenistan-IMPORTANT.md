# IMPORTANT — рабочая BS-связка для Туркменистана

**Статус:** ПОДТВЕРЖДЕНО клиентом 2026-09-28 — «заработало».
**Не удалять и не перезаписывать без новой проверки на реальном клиенте в TM.**

## Пара серверов
- **console1** (RU entry / whitelist) — `84.201.131.208` — вход для клиентов из Туркменистана
- **vpn-vp16** (EU exit) — `185.249.154.200` — выход в интернет через WireGuard

## Что лежит в этой папке
| Файл | Назначение |
|------|------------|
| `console1-BS-Turkmenistan-working.json` | Полный бэкап панели console1 (users/paths/domains/proxies) |
| `vp16-BS-Turkmenistan-working.json` | Полный бэкап панели vp16 |
| `console1-BS-runtime.tgz` | Снимок xray outbounds/reality/maps (sockopt wg-bs-exit) |
| `vp16-BS-runtime.tgz` | Снимок xray/maps exit |
| `console1-wg-show.txt` / `vp16-wg-show.txt` | Состояние WG на момент снимка (без private key в удобном виде — `wg show` скрывает ключ) |
| `README.md` | Этот файл |

Копии на серверах:
- `/var/backups/hiddify/console1-BS-Turkmenistan-working.json`
- `/var/backups/hiddify/vp16-BS-Turkmenistan-working.json`

## Критичные настройки (без них клиент в TM снова ломается)
1. **WireGuard** `wg-bs-exit`: console1 → vp16 `:51888`, подсеть `10.88.88.0/24`
2. **sockopt** на console1: `06_outbounds.json` + `.j2` → `freedom.streamSettings.sockopt.interface = wg-bs-exit` (после каждого `apply_configs` проверять!)
3. **Alias:** console1 = `🇷🇺 Обход БС`; vp16 = `🇪🇺 Выход EU` (IP + sslip + домены)
4. **show_domains** у console1.*: обязательно **IP** + **IP.sslip.io** + домены (+ Reality). Иначе в клиенте нет узла «Обход БС» без домена — а на мобильном BS доменные часто с крестиком, работает именно IP.
5. **Reality** на console1: `www.metacrawler.com`, `reality_enable=true`
6. **Reality** на vp16: `adium.im`
7. Панель **12.3.3**, `auto_update=false`, `package_mode=v12.3.3`
8. Клиент должен брать подписку **console1** (или CRM с console1+vp16), не чужой vpN. После правок — обновить подписку в приложении.

## Проверка после любых правок
- `wg show` — свежий handshake
- egress с console1 через WG = IP vp16
- в sub есть vless WS на `IP` и `IP.sslip.io` с alias «Обход БС»
- E2E: VLESS WS → ifconfig = vp16

## Восстановление
Не делать полный `set_settings=True` из чужого/старого дампа.
1. При необходимости users/admins/paths — точечный restore (как `scripts/restore-users-admins.sh`)
2. Заново выставить sockopt + show_domains + alias
3. Reality enable + apply
4. Сверить с этим снимком

Дата снимка: 2026-09-28 (после подтверждения клиента).