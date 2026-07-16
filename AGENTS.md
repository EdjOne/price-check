# Price Check — AGENTS.md

## Статус (на 2026-07-16)
- Версия: см. git log
- Сервер: Andrew (`andrew-server`), папка `/mnt/backup/price-check`, systemd `price-check` (User=andrew)
- База: `price_check.db` (SQLite), 4 юзера, 39 активных товаров (все EdjOne; 3 других approved-юзера пусты)
- Бот `active`. Расписание проверок — **фиксированное по Киеву**: 08:00, 12:00, 17:00, 23:00 (через `run_daily`, tz=Europe/Kyiv). Плюс `/check` вручную.
- IP сервера — украинский белый (НЕ Hetzner/датацентр). DNS-блоки магазинов — не из-за IP, а из-за несуществующих доменов в тестовых списках.

## Что сделано за сессию
- ✅ Переведён авто-апрув админа + расписание с `run_repeating` (каждые N ч) на **4 фиксированных времени по Киеву** (08/12/17/23) через `run_daily(time=..., tzinfo=ZoneInfo("Europe/Kyiv"))`. Commit ниже.
- ✅ `monitor.py` (правки из прошлой сессии, закоммичены): AWS WAF детект (`awswafintegration`), убраны `extra_http_headers` (Sec-CH-UA) и `--headless=new` в `_fetch_playwright` — иначе AWS WAF/Cloudflare детектят бота. ⚠️ это критично, не возвращать назад!
- ✅ Тест парсера: прогнали бота на ~33 реальных ссылках от имени тестового юзера 999999 (потом удалён). Результат: 22/33 цена взята, 11 — нет.

## Результат теста магазинов (2026-07-16)
Бот умеет брать цену ( ✅ ): silpo, zakaz, citrus, foxmart, stylus, repka, istore, amazon, bigl, skidka, kasta, prom, goldi, bookvoed, nashformat, book-ye, eva, budmarket, apteka911, goodok.
Баги парсера ( 🔴 цену не взял/криво ):
- `telemart` и `eldorado` — возвращают **0.0** (мусор вместо цены, надо чинить в parser.py)
- `varus`, `eko`, `karapuz`, `podorozhnyk` — цену не находит на живой странице (доработка parser.py)
- SPA/защищённые (rozetka, olx, ebay, temu, wildberries, zara, comfy, leroy, makeup, brocard и т.д.) — не берутся (JS-рендер/XHR). Нужен резидентный прокси (`PROXY_URL`).
- Часть FAIL была из-за кривых тестовых URL (категории вместо карточек) — не баг бота.

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

## TODO
- [ ] Починить parser.py: `telemart`/`eldorado` (0.0), `varus`/`eko`/`karapuz`/`podorozhnyk` (не находит цену)
- [ ] Резидентный прокси (`PROXY_URL`) для SPA/Cloudflare (rozetka, olx, ebay, temu, wildberries, zara, comfy, leroy, makeup...)
- [ ] Восстановить товары для 298507406 (Caotina) и 414291150 (Jameson MD) — утеряны, перекинуть вручную
- [ ] Расширить `_DEEPLINK_HOSTS` если появятся другие сокращатели
