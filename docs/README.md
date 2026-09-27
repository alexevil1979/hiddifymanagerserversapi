# Веб-документация парка

Открой в браузере:

[`docs/web/index.html`](web/index.html)

## Структура

| Путь | Назначение |
|------|------------|
| `web/index.html` | UI базы знаний |
| `web/styles.css` / `web/app.js` | оформление и логика |
| `web/data/fleet.json` | снимок карточек (без секретов) |
| `web/data/ssh-status.json` | результат SSH-проверки |
| `web/data/changelog.json` | журнал новых знаний |

## Обновление

1. После изменений inventory: пересобрать `fleet.json` (скрипт или агент).
2. SSH-сводка: `powershell -File scripts/check-fleet-ssh.ps1`
3. Новое знание (грабли, пайплайн, особый доступ) — запись в `changelog.json` + правка секции в `index.html` при необходимости.

Агент обязан дополнять эту документацию при появлении новых рабочих знаний (правило `.cursor/rules/docs-web.mdc`).
