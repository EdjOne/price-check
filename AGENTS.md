# Price Check — AGENTS.md

## Статус (на 2026-07-15)
- Версия: см. git log (commit 0e38fd9)
- Сервер: Andrew (`andrew-server`), папка `/mnt/backup/price-check`, systemd `price-check` (User=andrew)
- База: `price_check.db` (SQLite), 4 юзера, 17 активных товаров (все EdjOne)
- Бот `active`, проверка каждые 6 ч + `/check` вручную

## Что сделано за сессию
- ✅ Детект кастомного JS-челленджа магазина (biom.ua: `challenge_passed` cookie + `location.reload()`). Добавлен `_is_js_challenge()` в `monitor.py`, подключён в `fetch()` рядом с `_is_cloudflare()` → такие страницы уходят в headless-браузер (Playwright). Фикс commit `0e38fd9`. Товар #35 (biom.ua батарея) теперь парсится: цена 76106.0 UAH
- ✅ Обход AWS WAF (makeup.com.ua и др.): убраны `extra_http_headers` (Sec-CH-UA и т.п. — AWS WAF их детектит как бота и отдаёт заглушку «JavaScript is disabled»). В `_fetch_playwright` оставлен `headless=True` (НЕ `--headless=new` — тоже детектится WAF). Добавлен маркер `awswafintegration`/`awsWafCookieDomainList` в `_is_js_challenge()`. Проверено: makeup.com.ua → 1099.0 UAH, notino.ua → 7200.0 UAH, biom.ua → 76106.0 UAH
- ✅ Поддержка коротких ссылок `link.silpo.ua` (JS-редирект через Playwright → резолв в `silpo.ua/product/...`, кэш в `resolved_url`)
- ✅ Авто-апрув админа при старте бота (статус не слетает после рестартов)
- ✅ Убраны команды `/add` и `/remove` (товары добавляются только ссылкой в чат)
- ✅ Бэкап БД: `ExecStartPre` (копия `price_check.db.bak`) + ежедневный cron (03:00 UTC, ротация 7 копий в `backups/`)
- ✅ Починен `deploy.sh` (cd в папку скрипта + `--ignore-times --delete`, исключены `*.db`/`*.bak`/`backups`/`bot.log`)

## Как деплоить
```bash
bash /root/price-check/deploy.sh   # из локали, НЕ из другой папки!
sudo systemctl restart price-check  # на сервере (может висеть на stop ~минуту)
```

## Подводные камни
- ⚠️ `rsync ./` в deploy.sh БЕЗ cd в папку проекта стирает код на сервере — не запускать скрипт из /root!
- ⚠️ При рестарте systemd сервис долго останавливается (PTB job_queue) — таймаут 60с норма, проверяй `is-active` потом
- ⚠️ Потеряно 3 юзера и их товары (было 23, стало 17) — БД затёрлась локальной пустой при rsync. Восстановлены юзеры (approved), но товары Caotina (298507406) и Jameson MD (414291150) утеряны навсегда (нет URL в логах)

## TODO
- [ ] Восстановить товары для 298507406 (Caotina) и 414291150 (Jameson MD) — перекинуть вручную
- [ ] Расширить `_DEEPLINK_HOSTS` если появятся другие сокращатели (rozetka, eva, atb)
- [ ] Опционально: резидентный прокси для Cloudflare Managed Challenge (maudau.com.ua)
