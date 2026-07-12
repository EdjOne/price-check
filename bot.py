"""Telegram-бот для управления мониторингом цен.

Команды:
  /start, /help  — справка
  /add <ссылка> — добавить товар на мониторинг
  (или просто прислать ссылку)
  /list          — список отслеживаемых товаров
  /remove <id>   — убрать товар
  /check         — принудительно проверить всё сейчас
  /history <id>  — история цен товара

Авто-проверка каждые CHECK_INTERVAL_HOURS часов (через JobQueue).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re

from dotenv import load_dotenv

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes,
)

import db
import monitor

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("price_check.bot")

load_dotenv()  # подгружает BOT_TOKEN, CHECK_INTERVAL_HOURS, DB_PATH из .env

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHECK_INTERVAL_HOURS = int(os.getenv("CHECK_INTERVAL_HOURS", "6"))
DB_PATH = os.getenv("DB_PATH", "price_check.db")

HELP = (
    "💰 <b>Price Check</b> — моніторинг цін\n\n"
    "Просто надішліть посилання на товар — і бот візьме його на моніторинг.\n"
    "Коли ціна зміниться, прийде сповіщення.\n\n"
    "Команди:\n"
    "• /add &lt;посилання&gt; — додати товар\n"
    "• /list — мої товари\n"
    "• /remove &lt;id&gt; — прибрати товар\n"
    "• /check — перевірити всі зараз\n"
    "• /history &lt;id&gt; — історія цін\n"
    "• /help — ця довідка"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP, parse_mode="HTML")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP, parse_mode="HTML")


async def _add_url(update: Update, url: str, chat_id):
    conn = context.bot_data["conn"]
    item_id = db.add_item(conn, url, str(chat_id))
    await update.message.reply_text(f"🔄 Перевіряю посилання #{item_id}…")
    item = db.get_item(conn, item_id)
    # синхронная первая проверка
    res = monitor.check_item(conn, item, bot=context.bot)
    if res.get("ok"):
        if res.get("direction") == "new":
            await update.message.reply_text(
                f"✅ Додано #{item_id}\n"
                f"📦 {item['title'] or url}\n"
                f"💰 {res['new']:.2f} {res['currency']}"
            )
        else:
            await update.message.reply_text(
                f"✅ Оновлено #{item_id}: {res['new']:.2f} {res['currency']}"
            )
    else:
        await update.message.reply_text(
            f"⚠️ Посилання #{item_id} додано, але ціну не вдалося визначити "
            f"({res.get('error')}). Перевірю пізніше."
        )


async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Вкажіть посилання: /add https://...")
        return
    url = context.args[0]
    if not URL_RE.match(url):
        await update.message.reply_text("Це не схоже на посилання (потрібен http/https).")
        return
    await _add_url(update, url, update.effective_chat.id)


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    m = URL_RE.search(text)
    if not m:
        await update.message.reply_text("Надішліть посилання на товар або /help.")
        return
    # если в сообщении несколько ссылок — берём первую
    await _add_url(update, m.group(0), update.effective_chat.id)


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = context.bot_data["conn"]
    items = db.list_items(conn, chat_id=update.effective_chat.id)
    if not items:
        await update.message.reply_text("Список порожній. Надішліть посилання на товар 🛒")
        return
    lines = ["📋 <b>Ваші товари:</b>"]
    for it in items:
        price = f"{it['last_price']:.2f} {it['currency']}" if it["last_price"] is not None else "—"
        name = (it["title"] or it["url"])[:60]
        lines.append(f"#{it['id']} · {price}\n   {name}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Вкажіть id: /remove 3")
        return
    try:
        item_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("id має бути числом.")
        return
    conn = context.bot_data["conn"]
    ok = db.remove_item(conn, item_id)
    await update.message.reply_text("✅ Видалено." if ok else "❌ Немає такого id.")


async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Вкажіть id: /history 3")
        return
    try:
        item_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("id має бути числом.")
        return
    conn = context.bot_data["conn"]
    rows = db.history(conn, item_id, limit=10)
    if not rows:
        await update.message.reply_text("Історії ще немає.")
        return
    lines = [f"🕓 Історія #{item_id}:"]
    for r in reversed(rows):
        lines.append(f"  {r['checked_at'][:16].replace('T', ' ')} — {r['price']:.2f} {r['currency']}")
    await update.message.reply_text("\n".join(lines))


async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = context.bot_data["conn"]
    await update.message.reply_text("🔄 Перевіряю всі товари…")
    results = monitor.check_all(conn, bot=context.bot)
    changed = [r for r in results if r.get("changed")]
    await update.message.reply_text(
        f"Готово. Перевірено: {len(results)}, змін: {len(changed)}."
    )


async def scheduled_check(context: ContextTypes.DEFAULT_TYPE):
    conn = context.bot_data["conn"]
    logger.info("scheduled check start")
    monitor.check_all(conn, bot=context.bot)
    logger.info("scheduled check done")


def main():
    if not BOT_TOKEN:
        raise SystemExit("❌ Не задано BOT_TOKEN (середовище або .env)")

    conn = db.connect(DB_PATH)
    app = Application.builder().token(BOT_TOKEN).build()
    app.bot_data["conn"] = conn

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("remove", remove_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("check", check_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))

    # авто-проверка каждые N часов
    app.job_queue.run_repeating(
        scheduled_check,
        interval=CHECK_INTERVAL_HOURS * 3600,
        first=30,
        name="price_check",
    )

    logger.info("Bot started. Check interval = %s h", CHECK_INTERVAL_HOURS)
    app.run_polling()


if __name__ == "__main__":
    main()
