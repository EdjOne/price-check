# Price Check — AGENTS.md

## Статус (на 2026-07-20)
- Сервер: Andrew (`andrew-server`), папка `/mnt/backup/price-check`, systemd `price-check` (User=andrew)
- База: `price_check.db` (SQLite), 6 юзеров, 55 активных товаров (из 59 всего)
- Бот `active`. Расписание проверок — **фиксированное по Киеву**: 08:00, 12:00, 17:00, 23:00 (через `run_daily`, tz=Europe/Kyiv). Плюс `/check` вручную.
- IP сервера — украинский белый (НЕ Hetzner/датацентр).

## Таблицы магазинов
- `known_shops` (61 домен) — белый список проверенных магазинов. Автозаполняется при успешном `check_item` (`db.touch_known_shop`). НЕ привязан к товарам юзеров.
- `unknown_shops` (0) — магазины, где цену не взяли, но НЕ в чёрном списке (ждут доработки парсера).
- Чёрный список `PROXY_REQUIRED_HOSTS` в `monitor.py`: `ya.ua`, `deka.ua`, `4f.ua` (Cloudflare Managed Challenge / CDN-дроп IP — браузер без резидентного прокси не пробивает).
- `_PLAYWRIGHT_ALWAYS` в `monitor.py`: `styx.odessa.ua` (цена грузится JS/AJAX после DOM, requests не берёт — всегда через браузер).

## Что сделано за сессию (2026-07-20)
- ✅ Команда `/shops` — показывает юзеру: поддерживаемые (`known_shops`) + неподдерживаемые (`PROXY_REQUIRED_HOSTS`) + неизвестные (`unknown_shops`). Добавлена в HELP, docstring и меню команд Telegram (`set_my_commands`).
- ✅ Таблица `known_shops` — отдельный белый список проверенных магазинов (создаётся в `db.ensure_known_shops_table`, вызывается при старте).
- ✅ **fora.ua починен (SPA)**: requests отдаёт пустой React-скелет → `_looks_empty_spa()` форсит Playwright → `wait_for_function` ждёт паттерн `\d+грн/₴/UAH` → цена из ld+json `offers.price` (актуальная, не зачёркнутая). Товар #150 реактивирован.
- ✅ **styx.odessa.ua починен**: старый jQuery-сайт, цена грузится AJAX после DOM. Добавлен в `_PLAYWRIGHT_ALWAYS` → `fetch()` сразу в браузер. Товар #170 (2000 грн).
- ✅ **deka.ua → чёрный список**: Cloudflare Managed Challenge, Playwright не пробивает (таймаут, «Трохи зачекайте…»).
- ✅ **4f.ua → чёрный список**: CDN дропает IP сервера (HTTP:000, 0 байт, `ERR_HTTP2_PROTOCOL_ERROR`). Сайт с сервера недоступен.
- ✅ **letyshops-парсинг**: спарсили список магазинов с `letyshops.com/ua/shops`, прогнали живьём через `fetch`+`extract` только NEW-магазины (без дублей). Рабочие добавлены в `known_shops`: `apteka911.ua` (5.0), `citrus.ua` (2299.0), `modnakasta.ua` (199.0), `estro.ua` (3390.0), `stylus.ua`→`stls.store` (1199.0). Не добавлены (403/DNS/не магазин): `iherb.com`, `notino.ua`, `allegro.pl`, `sinsay.ua`, `knigarnia.ua`, `budinok-igrashok.ua`, `hotline.ua`.
- ✅ **itmag.ua починен**: `_is_cloudflare()` ложно срабатывал на маркер `challenge-platform` (есть в обычном JS сайта, не только в челлендже Cloudflare) → `fetch` уходил в Playwright и зависал (45с). Убрал `challenge-platform` как отдельный маркер (оставил точные `cf-chl`/`__cf_chl`/укр-фразы). Теперь itmag.ua парсится через requests за ~1с (цена в `<span class="product-price">`). Товар #187 (499 UAH).
- ✅ `_looks_empty_spa()` — детект React/Vue-скелета без цены → Playwright.

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
