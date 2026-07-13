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
ADMIN_ID = os.getenv("ADMIN_ID")  # кому прилетают запросы на апрув новых юзеров

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
    if not await _ensure_access(update, context):
        return
    if not context.args:
        await update.message.reply_text("Вкажіть посилання: /add https://...")
        return
    url = context.args[0]
    if not URL_RE.match(url):
        await update.message.reply_text("Це не схоже на посилання (потрібен http/https).")
        return
    await _add_url(update, context, url, update.effective_chat.id)


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ensure_access(update, context):
        return
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


# Слова-мусор: магазины и служебная шелуха, мешающая сравнивать названия товаров.
_JUNK_WORDS = {
    "okwine", "silpo", "rozetka", "zakaz", "metro", "antoshka", "citrus", "maudau",
    "foxmart", "foxfrot", "fua", "prom", "obzhora", "com", "ua", "ru", "html", "www",
    "uk", "shop", "store", "buy", "market", "официальный", "сайт", "varus", "alcomag",
    "купити", "купить", "заказать", "онлайн", "супермаркет", "интернет", "магазин",
    "irish", "gin", "джин", "курити", "сільпо", "silpo", "awesome", "charcoal",
    "bliskavka", "makkvin", "пастеризоване", "темне", "thbg", "принтера", "пластик",
    "набір", "set", "дегустаційний", "best", "price", "new", "original", "extra",
}


def normalize_key(title: str, url: str = "") -> str:
    """Ключ группировки похожих товаров: бренд+модель без мусора/чисел/магазина.
    Все части домена магазина и служебные слова выкидываются где бы ни стояли."""
    # все части хоста (metro, zakaz, alcomag, ...) -> в стоп-слова
    host_parts = set()
    try:
        netloc = urlparse(url).netloc.lower().split("@")[-1].split(":")[0]
        if netloc.startswith("www."):
            netloc = netloc[4:]
        for p in netloc.split("."):
            if p:
                host_parts.add(p)
    except Exception:  # noqa: BLE001
        pass
    t = (title or "").lower()
    # любые разделители строк (вкл. unicode \u2028/\u2029) -> пробел
    t = t.replace("\t", " ")
    t = re.sub(r"[\s\u2028\u2029\u00a0]+", " ", t)
    t = re.sub(r"\([^)]*\)", " ", t)         # (5060434130228), (SM-...)
    t = re.sub(r"«[^»]*»", " ", t)
    t = re.sub(r"\d+", " ", t)               # все цифры: объёмы, %, модель
    # оставляем только латиницу/цифры/пробелы (кириллица = мусор/магазины)
    t = re.sub(r"[^a-z\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    stop = set(_JUNK_WORDS) | host_parts
    toks = [w for w in t.split() if w and w not in stop and len(w) > 2]
    return " ".join(sorted(toks)).strip()


def _item_view(it, best: bool = False):
    """Возвращает (html_text, markup) для одного товара: название-ссылка + кнопки."""
    price = f"{it['last_price']:.2f} {it['currency']}" if it["last_price"] is not None else "—"
    name = (it["title"] or it["url"])[:70]
    store = _store_name(it["url"])
    if store and store not in name.lower():
        name = f"{name} — {store.capitalize()}"
    trophy = "✅ " if best else ""
    text = f'<a href="{it["url"]}">{name}</a>\n#{it["id"]} · <b>{trophy}{price}</b>'
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("📜 Історія", callback_data=f"hist:{it['id']}"),
        InlineKeyboardButton("🗑 Видалити", callback_data=f"del:{it['id']}"),
    ]])
    return text, markup


# ── Контроль доступа (апрувал новых юзеров) ────────────────────────────────

async def _ensure_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True, если юзеру можно работать. Иначе шлёт ему сообщение и запрос админу."""
    conn = context.bot_data["conn"]
    chat_id = update.effective_chat.id
    # админ всегда проходит
    if ADMIN_ID and str(chat_id) == str(ADMIN_ID):
        db.upsert_pending(conn, chat_id, update.effective_user.username)
        db.set_user_status(conn, chat_id, "approved")
        return True
    user = db.get_user(conn, chat_id)
    if user and user["status"] == "approved":
        return True
    # новый или pending/denied — регистрируем и просим апрув у админа
    is_new = user is None
    if is_new:
        db.upsert_pending(conn, chat_id, update.effective_user.username)
    if user and user["status"] == "denied":
        await update.effective_message.reply_text("⛔ Доступ заборонено. Зверніться до власника бота.")
        return False
    # pending
    uname = update.effective_user.username or update.effective_user.full_name or "?"
    await update.effective_message.reply_text(
        "⏳ Ваш запит на доступ відправлено власнику. Очікуйте підтвердження ✅"
    )
    if ADMIN_ID:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Дозволити", callback_data=f"approve:{chat_id}"),
            InlineKeyboardButton("❌ Відхилити", callback_data=f"deny:{chat_id}"),
        ]])
        await context.bot.send_message(
            chat_id=int(ADMIN_ID),
            text=f"🔔 <b>Новий користувач</b> запитує доступ:\n"
                 f"ID: <code>{chat_id}</code>\n"
                 f"@{uname}",
            parse_mode="HTML", reply_markup=kb,
        )
    return False


async def cb_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat_id = q.data.split(":", 1)[1]
    conn = context.bot_data["conn"]
    db.set_user_status(conn, chat_id, "approved")
    await q.answer("✅ Дозволено")
    await q.edit_message_text(f"✅ Користувач <code>{chat_id}</code> отримав доступ.", parse_mode="HTML")
    try:
        await context.bot.send_message(chat_id=int(chat_id),
                                      text="✅ Доступ відкрито! Можете користуватись ботом.")
    except Exception:  # noqa: BLE001
        pass


async def cb_deny(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat_id = q.data.split(":", 1)[1]
    conn = context.bot_data["conn"]
    db.set_user_status(conn, chat_id, "denied")
    await q.answer("❌ Відхилено")
    await q.edit_message_text(f"❌ Користувач <code>{chat_id}</code> заблокований.", parse_mode="HTML")
    try:
        await context.bot.send_message(chat_id=int(chat_id),
                                      text="⛔ Доступ заборонено власником бота.")
    except Exception:  # noqa: BLE001
        pass


async def _send_list(target, context: ContextTypes.DEFAULT_TYPE, items):
    """Шлёт заголовок + отдельное сообщение на каждый товар (кнопки под названием).
    Товары группируются по normalize_key: в каждой группе 🏆 у самой низкой цены.
    Сортировка: по названию группы, затем по возрастанию цены."""
    # группируем
    groups: dict[str, list] = {}
    for it in items:
        groups.setdefault(normalize_key(it["title"], it["url"]) or f"url:{it['url']}", []).append(it)
    # помечаем лучшую цену в каждой группе (где >1 товара)
    best_ids: set[int] = set()
    for g in groups.values():
        if len(g) > 1:
            cheapest = min(g, key=lambda x: (x["last_price"] if x["last_price"] is not None else 1e9))
            best_ids.add(cheapest["id"])
    # плоский список, отсортированный по (ключ группы, цена)
    def _sort_key(it):
        price = it["last_price"] if it["last_price"] is not None else 1e9
        return (normalize_key(it["title"], it["url"]), price)
    ordered = sorted(items, key=_sort_key)
    await target.reply_text(f"📋 <b>Ваші товари ({len(items)}):</b>", parse_mode="HTML")
    for it in ordered:
        text, markup = _item_view(it, best=it["id"] in best_ids)
        await target.reply_text(
            text, parse_mode="HTML", reply_markup=markup,
            disable_web_page_preview=True,
        )


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ensure_access(update, context):
        return
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
    if not await _ensure_access(update, context):
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
    if not await _ensure_access(update, context):
        return
    conn = context.bot_data["conn"]
    rows = db.history(conn, item_id, limit=10)
    if not rows:
        await update.message.reply_text("Історії ще немає.")
        return
    it = db.get_item(conn, item_id)
    name = (it["title"] or it["url"])[:70] if it else f"#{item_id}"
    lines = [f"🕓 Історія <a href=\"{it['url']}\">{name}</a>:"]
    for r in reversed(rows):
        lines.append(f"  {r['checked_at'][:16].replace('T', ' ')} — {r['price']:.2f} {r['currency']}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ensure_access(update, context):
        return
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
    ℹ️ Telegram не дає видаляти повідомлення старші 48 годин.
    """
    if not await _ensure_access(update, context):
        return
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
    if not await _ensure_access(update, context):
        return
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
    if not await _ensure_access(update, context):
        return
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
    if not await _ensure_access(update, context):
        return
    conn = context.bot_data["conn"]
    rows = db.history(conn, item_id, limit=10)
    await q.answer()
    if not rows:
        await context.bot.send_message(chat_id=q.message.chat_id, text="Історії ще немає.")
        return
    it = db.get_item(conn, item_id)
    name = (it["title"] or it["url"])[:70] if it else f"#{item_id}"
    lines = [f"🕓 Історія <a href=\"{it['url']}\">{name}</a>:"]
    for r in reversed(rows):
        lines.append(f"  {r['checked_at'][:16].replace('T', ' ')} — {r['price']:.2f} {r['currency']}")
    await context.bot.send_message(chat_id=q.message.chat_id, text="\n".join(lines), parse_mode="HTML")


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
    from telegram.ext import JobQueue
    # PTB >=22.7 не создаёт job_queue автоматически — инициализируем явно
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).job_queue(JobQueue()).build()
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
    app.add_handler(CallbackQueryHandler(cb_approve, pattern="^approve:"))
    app.add_handler(CallbackQueryHandler(cb_deny, pattern="^deny:"))
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
