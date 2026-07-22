# Price Check — AGENTS.md

## Статус (на 2026-07-22)
- Сервер: Andrew, папка `/mnt/backup/price-check`, systemd `price-check` (User=andrew)
- База: `price_check.db` (SQLite), 11 юзеров, 33 активных товара
- Бот `active`. Расписание проверок — **фиксированное по Киеву**: 08:00, 12:00, 17:00, 23:00.
- IP сервера — украинский белый.
- `ADMIN_ID=311174242` в `.env` на сервере (уведомления админу работают)
- `deploy.sh` — добавлен `--exclude '.env'` (не сносит серверный `.env`)

## Таблицы магазинов
- `known_shops` (98 доменов) — белый список проверенных магазинов.
- `unknown_shops` (0) — магазины, где цену не взяли, но НЕ в чёрном списке.
- Чёрный список `PROXY_REQUIRED_HOSTS` в `monitor.py`: `ya.ua`, `deka.ua`, `4f.ua`, `hm.com`.
- `_PLAYWRIGHT_ALWAYS` в `monitor.py`: `styx.odessa.ua`, `primeauto.com.ua`.

## Что сделано за сессию (2026-07-22)
- ✅ **pending_urls** — новая таблица + логика: если юзер не аппрувнут, ссылка сохраняется в БД. После аппрува админом — все сохранённые ссылки автоматически обрабатываются (добавляются на мониторинг с проверкой цены). Работает и для обычного ввода ссылки, и для `/add <url>`.
- ✅ v1.8.4: закоммичено, запушено, задеплоено, сервис рестартнут.
- ✅ v1.8.5: primeauto.com.ua (кастомний JS-challenge) — додано в _PLAYWRIGHT_ALWAYS + retry goto в _fetch_playwright після location.reload. Ціна береться: 1302 UAH.

## Универсальные фиксы парсера (накоплено)
- `_is_js_challenge()` — кастомный JS-челлендж (biom.ua: `challenge_passed` + reload).
- AWS WAF (makeup.com.ua): НЕТ `extra_http_headers` (Sec-CH-UA детектит WAF), НЕ `--headless=new` — только `headless=True` + `AutomationControlled off`.
- `_is_cloudflare()` — англо- и украиноязычные заглушки Cloudflare.
- `_looks_empty_spa()` — React/Vue-скелет без цены → Playwright.
- `_PLAYWRIGHT_ALWAYS` — домены, где цена грузится JS/AJAX (всегда браузер).
- Поддержка deeplink `link.silpo.ua` (JS-редирект → `silpo.ua`, кэш в `resolved_url`).

## Как деплоить
```bash
bash /root/price-check/deploy.sh   # из локали, НЕ из другой папки!
sudo systemctl restart price-check  # на сервере (может висеть на stop ~минуту)
```

## Подводные камни
- ⚠️ `rsync ./` в deploy.sh БЕЗ cd в папку проекта стирает код на сервере — не запускать скрипт из /root!
- ⚠️ При рестарте systemd сервис долго останавливается (PTB job_queue) — таймаут 60с норма, проверяй `is-active` потом
- ⚠️ `_fetch_playwright`: НЕЛЬЗЯ `extra_http_headers` (Sec-CH-UA детектит WAF как бота) и НЕ `--headless=new`. Только `headless=True` + `AutomationControlled off`.
- ⚠️ job_queue в PTB >=22.7 не создаётся автомато — инициализируем явно `.job_queue(JobQueue()).build()`
- ⚠️ При `git pull --rebase` конфликты по AGENTS.md решать `git checkout --ours AGENTS.md` (локальная версия — самая свежая), иначе откатится к серверной

## TODO
- [ ] Резидентный прокси (`PROXY_URL`) для Cloudflare/CDN (ya.ua, deka.ua, 4f.ua и др.)
- [ ] Расширить `_DEEPLINK_HOSTS` если появятся другие сокращатели (rozetka, eva, atb)
- [ ] Периодически прогонять живую проверку `known_shops` (магазины могут сломаться при редизайне)
