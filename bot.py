"""Telegram-бот для управления мониторингом цен.

Команды:
  /start, /help  — справка
  /add <ссылка> — добавить товар
  (или просто прислать ссылку)
  /list          — список (кликабельный, с кнопками: открыть / история / удалить)
  /remove <id>   — убрать товар
  /check         — принудительно проверить всё сейчас
  /history <id>  — история цен
"""
from __future__ import annotations

import logging
import os
import re
from urllib.parse import urlparse

from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes,
)

import db
import monitor

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("price_check.bot")

load_dotenv()

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
    "• /list — мої товари (кнопки: відкрити / історія / видалити)\n"
    "• /remove &lt;id&gt; — прибрати товар\n"
    "• /check — перевірити всі зараз\n"
    "• /clear — очистити чат від повідомлень\n"
    "• /history &lt;id&gt; — історія цін\n"
    "• /help — ця довідка"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP, parse_mode="HTML")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP, parse_mode="HTML")


async def _add_url(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, chat_id):
    conn = context.bot_data["conn"]
    existing = conn.execute(
        "SELECT id FROM items WHERE url = ? AND active = 1", (url,)
    ).fetchone()
    if existing:
        await update.message.reply_text(
            f"✅ Це посилання вже відстежується: #{existing['id']}"
        )
        return
    item_id = db.add_item(conn, url, str(chat_id))
    await update.message.reply_text(f"🔄 Перевіряю посилання #{item_id}…")
    item = db.get_item(conn, item_id)
    res = await monitor.check_item(conn, item, bot=context.bot)
    if res.get("ok"):
        if res.get("direction") == "new":
            await update.message.reply_text(
                f"✅ Додано #{item_id}\n📦 {item['title'] or url}\n💰 {res['new']:.2f} {res['currency']}"
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
    await _add_url(update, context, url, update.effective_chat.id)


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # склеиваем переносы строк — чтобы URL, разбитый при вставке, восстановился
    text = (update.message.text or "").replace("\n", "").replace("\r", "")
    m = URL_RE.search(text)
    if not m:
        await update.message.reply_text("Надішліть посилання на товар або /help.")
        return
    await _add_url(update, context, m.group(0), update.effective_chat.id)


_STORE_TLDS = {"ua", "com", "net", "org", "co", "io", "gov", "edu",
               "info", "biz", "ru", "de", "pl", "cz", "eu", "su", "by", "kz"}


def _store_name(url):
    """Извлекает название магазина (SLD) из URL: rozetka.com.ua -> rozetka."""
    try:
        netloc = urlparse(url).netloc.lower()
    except Exception:  # noqa: BLE001
        return ""
    netloc = netloc.split("@")[-1].split(":")[0]
    if netloc.startswith("www."):
        netloc = netloc[4:]
    parts = [p for p in netloc.split(".") if p]
    while len(parts) > 1 and parts[-1] in _STORE_TLDS:
        parts.pop()
    return parts[-1] if parts else ""


def _item_view(it):
    """Возвращает (html_text, markup) для одного товара: название-ссылка + кнопки."""
    price = f"{it['last_price']:.2f} {it['currency']}" if it["last_price"] is not None else "—"
    name = (it["title"] or it["url"])[:70]
    store = _store_name(it["url"])
    if store and store not in name.lower():
        name = f"{name} — {store}"
    text = f'<a href="{it["url"]}">{name}</a>\n#{it["id"]} · <b>{price}</b>'
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("📜 Історія", callback_data=f"hist:{it['id']}"),
        InlineKeyboardButton("🗑 Видалити", callback_data=f"del:{it['id']}"),
    ]])
    return text, markup


async def _send_list(target, context: ContextTypes.DEFAULT_TYPE, items):
    """Шлёт заголовок + отдельное сообщение на каждый товар (кнопки под названием).
    Сортировка по названию товара БЕЗ хвоста-магазина (чтобы одинаковые группировались)."""
    def _sort_key(it):
        title = (it["title"] or it["url"])
        # отрезаем добавленный в конце " — магазин" / " | магазин"
        base = re.sub(r"\s*[—|]\s*[^\s—|]+$", "", title).strip()
        return base.lower()
    items = sorted(items, key=_sort_key)
    await target.reply_text(f"📋 <b>Ваші товари ({len(items)}):</b>", parse_mode="HTML")
    for it in items:
        text, markup = _item_view(it)
        await target.reply_text(
            text, parse_mode="HTML", reply_markup=markup,
            disable_web_page_preview=True,
        )


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = context.bot_data["conn"]
    items = db.list_items(conn, chat_id=update.effective_chat.id)
    if not items:
        await update.message.reply_text("Список порожній. Надішліть посилання на товар 🛒")
        return
    await _send_list(update.message, context, items)


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
    results = await monitor.check_all(conn, bot=context.bot)
    changed = [r for r in results if r.get("changed")]
    await update.message.reply_text(
        f"Готово. Перевірено: {len(results)}, змін: {len(changed)}."
    )


async def scheduled_check(context: ContextTypes.DEFAULT_TYPE):
    conn = context.bot_data["conn"]
    logger.info("scheduled check start")
    await monitor.check_all(conn, bot=context.bot)
    logger.info("scheduled check done")


async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Чистит чат: удаляет сообщения от текущего вниз (сколько сможет).

    Telegram позволяет боту удалять только сообщения не старше 48 часов.
    Идём по message_id вниз и молча пропускаем то, что удалить нельзя.
    """
    chat_id = update.effective_chat.id
    last_id = update.message.message_id
    deleted = 0
    # проходим окно из последних сообщений (id уменьшаем)
    for mid in range(last_id, max(0, last_id - 200), -1):
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
            deleted += 1
        except Exception:  # noqa: BLE001
            # сообщение чужое/старше 48ч/уже удалено/не существует — пропускаем
            continue
    note = await context.bot.send_message(
        chat_id=chat_id,
        text=(f"🧹 Очищено повідомлень: {deleted}.\n"
              "ℹ️ Telegram не дає видаляти повідомлення старші 48 годин."),
    )
    # удалить и это уведомление через 5 секунд
    context.job_queue.run_once(
        _delete_later, when=5,
        data={"chat_id": chat_id, "message_id": note.message_id},
        name=f"clr_note_{note.message_id}",
    )


async def _delete_later(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data
    try:
        await context.bot.delete_message(chat_id=d["chat_id"], message_id=d["message_id"])
    except Exception:  # noqa: BLE001
        pass


# --- Callback-обработчики (кнопки списка) ---
async def cb_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    item_id = int(q.data.split(":", 1)[1])
    conn = context.bot_data["conn"]
    it = db.get_item(conn, item_id)
    if not it:
        await q.answer("Вже видалено")
        return
    name = (it["title"] or it["url"])[:50]
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Так, видалити", callback_data=f"dodel:{item_id}"),
        InlineKeyboardButton("❌ Ні", callback_data="noop"),
    ]])
    await q.edit_message_text(
        f"🗑 Видалити <b>{name}</b> з моніторингу?", parse_mode="HTML", reply_markup=kb
    )
    await q.answer()


async def cb_dodel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    item_id = int(q.data.split(":", 1)[1])
    conn = context.bot_data["conn"]
    db.remove_item(conn, item_id)
    await q.answer("Видалено ✅")
    # просто убираем сообщение этого товара
    try:
        await q.message.delete()
    except Exception:  # noqa: BLE001
        await q.edit_message_text("🗑 Видалено.")


async def cb_hist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    item_id = int(q.data.split(":", 1)[1])
    conn = context.bot_data["conn"]
    rows = db.history(conn, item_id, limit=10)
    await q.answer()
    if not rows:
        await context.bot.send_message(chat_id=q.message.chat_id, text="Історії ще немає.")
        return
    lines = [f"🕓 Історія #{item_id}:"]
    for r in reversed(rows):
        lines.append(f"  {r['checked_at'][:16].replace('T', ' ')} — {r['price']:.2f} {r['currency']}")
    await context.bot.send_message(chat_id=q.message.chat_id, text="\n".join(lines))


async def cb_noop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.warning("Update %s caused error: %s", update, context.error)


async def post_init(app: Application):
    """Регистрируем меню команд — Telegram показывает подсказку при вводе '/'."""
    await app.bot.set_my_commands([
        BotCommand("add", "➕ Додати товар за посиланням"),
        BotCommand("list", "📋 Мої товари"),
        BotCommand("check", "🔄 Перевірити всі ціни зараз"),
        BotCommand("clear", "🧹 Очистити чат"),
        BotCommand("history", "📜 Історія цін (/history <id>)"),
        BotCommand("remove", "🗑 Прибрати товар (/remove <id>)"),
        BotCommand("help", "❓ Довідка"),
    ])
    logger.info("Bot commands menu registered")


def main():
    if not BOT_TOKEN:
        raise SystemExit("❌ Не задано BOT_TOKEN (середовище або .env)")
    conn = db.connect()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.bot_data["conn"] = conn

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("remove", remove_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("check", check_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))

    app.add_handler(CallbackQueryHandler(cb_del, pattern="^del:"))
    app.add_handler(CallbackQueryHandler(cb_dodel, pattern="^dodel:"))
    app.add_handler(CallbackQueryHandler(cb_hist, pattern="^hist:"))
    app.add_handler(CallbackQueryHandler(cb_noop, pattern="^noop$"))

    app.add_error_handler(error_handler)

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
