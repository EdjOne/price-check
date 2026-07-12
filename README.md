# Price Check

Універсальний моніторинг цін з керуванням через Telegram.
Знайшов товар на будь-якому сайті → кинув посилання боту → бот слідкує за ціною і шле сповіщення при зміні.

## Що вміє
- 🔗 Додає на моніторинг **будь-яке** посилання (маркетплейси, магазини, рандомні сайти)
- 🤖 Авто-визначення ціни зі сторінки (JSON-LD → meta-теги → CSS-селектори → регексп по валюті)
- 🛡 Обхід Cloudflare: якщо сайт блокує (403 / «Just a moment»), ціна береться через headless-браузер (Playwright)
- ⏰ Періодична перевірка (кожні N годин, налаштовується)
- 🔔 Сповіщення в Telegram при зміні/падінні ціни
- 📜 Історія цін по кожному товару
- 📋 **Клікабельний список** (`/list`): кнопки 🔗 відкрити / 📜 історія / 🗑 видалити (з підтвердженням), відсортовано за назвою товару

## Як користуватися
1. Створи бота у [@BotFather](https://t.me/BotFather) і отримай токен.
2. `cp .env.example .env` і впиши `BOT_TOKEN`.
3. Встанови залежності та запусти:
   ```bash
   pip install -r requirements.txt
   python -m playwright install chromium
   python bot.py
   ```
4. У Telegram боті:
   - просто надішли посилання на товар → бот візьме його на моніторинг
   - `/list` — твої товари (кнопки: відкрити / історія / видалити)
   - `/remove <id>` — прибрати
   - `/check` — перевірити всі зараз
   - `/history <id>` — історія цін

## Автозапуск (systemd)
Створи `/etc/systemd/system/price-check.service`:
```ini
[Unit]
Description=Price Check Telegram bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/root/price-check
ExecStart=/usr/local/lib/hermes-agent/venv/bin/python3 bot.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```
Запуск:
```bash
systemctl daemon-reload
systemctl enable --now price-check
```
⚠️ Один токен може полити лише **один** екземпляр — не запускай паралельно `python bot.py` і systemd.

## Налаштування (.env)
| Змінна | За замовчуванням | Опис |
|--------|------------------|------|
| `BOT_TOKEN` | — | токен від BotFather (обов'язково) |
| `CHECK_INTERVAL_HOURS` | `6` | інтервал авто-перевірки, годин |
| `DB_PATH` | `price_check.db` | файл бази SQLite |

## Структура
- `parser.py` — авто-детект ціни/назви з HTML
- `db.py` — SQLite (товари + історія цін)
- `monitor.py` — fetch (requests + Playwright-фолбек) + перевірка + алерти
- `bot.py` — Telegram-бот (python-telegram-bot 22), клікабельний список
- `price-check.service` — systemd-юніт

## Перевірити парсер окремо
```bash
python parser.py https://example.com/product
```
