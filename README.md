# 🛒 Price Check

🔍 **Price Check — твій особистий мисливець за знижками**

Набридло вручну гуглити «де дешевше?» перед кожною покупкою? Довірся боту — він зробить це за тебе. Автоматично, безкоштовно, без нервів.

**Як це працює:**
1️⃣ Кидаєш боту посилання на товар
2️⃣ Він 4 рази на день (вранці, вдень, увечері й уночі) сам перевіряє ціну
3️⃣ Ціна впала? Ти дізнаєшся першим ⚡

**97 магазинів під контролем — від техніки до продуктів:**
Сільпо, ATB, VARUS, Fozzy, Rozetka, Comfy, Citrus, Stylus, MOYO, Telemart, Epicentr, Intertop, ANSWEAR, Reserved, Mohito, COLIN'S, Mango, Jysk, Eva, Аптека 911, Brocard, OLX, Prom, Maudau, Kasta і ще десятки інших. Техніка, одяг, косметика, ліки, іграшки, їжа, зоотовари — все під прицілом.

Хочеш нову техніку, книжку, косметику чи ліки за адекватною ціною? Бот тримає руку на пульсі й не дасть переплатити.

✅ **Безкоштовно**
✅ **Без реклами**
✅ **Працює у фоні** — сам все перевірить

👉 Просто напиши боту в приватку, кинь посилання на товар — і почни ловити знижки вже сьогодні:
[@pricech_bot](https://t.me/pricech_bot)

---

## Для розробників

Стек: Python, `python-telegram-bot` 22+, SQLite, Playwright (Chromium), BeautifulSoup.

### Швидкий старт
```bash
git clone https://github.com/EdjOne/price-check.git
cd price-check
cp .env.example .env  # впиши BOT_TOKEN
pip install -r requirements.txt
python -m playwright install chromium
python bot.py
```

### Структура
- `bot.py` — Telegram-бот (хендлери, клавіатури, меню команд)
- `parser.py` — детект ціни з HTML (JSON-LD → meta → CSS → регексп)
- `monitor.py` — fetch (requests + Playwright fallback), перевірка, алерти
- `db.py` — SQLite (товари, історія, юзери, known shops)

### Команди
| Команда | Опис |
|---------|------|
| `/list` | Список товарів з кнопками |
| `/shops` | Які магазини підтримуються |
| `/history <id>` | Історія цін |
| `/clear` | Очистити чат |
| `/help` | Довідка |
