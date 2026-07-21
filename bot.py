"""Telegram-бот для управления мониторингом цен.

Команды:
  /start, /help  — справка
  /add <ссылка> — добавить товар
  (или просто прислать ссылку)
  /list          — список (кликабельный, с кнопками: открыть / история / удалить)
  /remove <id>   — убрать товар
  /history <id>  — история цен
  /shops         — список поддерживаемых и неподдерживаемых магазинов
"""
from __future__ import annotations

import logging
import os
import re
from datetime import time as dtime
from zoneinfo import ZoneInfo

KYIV = ZoneInfo("Europe/Kyiv")
CHECK_TIMES = [(8, 0), (12, 0), (17, 0), (23, 0)]  # по Киеву
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
    "• /list — мої товари (кнопки: відкрити / історія / видалити)\n"
    "• /shops — які магазини бот уміє читати\n"
    "• /clear — очистити чат від повідомлень\n"
    "• /history &lt;id&gt; — історія цін\n"
    "• /help — ця довідка"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP, parse_mode="HTML")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP, parse_mode="HTML")


CLEANUP_DELAY_SECONDS = 120  # через сколько секунд удалять служебные сообщения добавления


async def _delete_messages_job(context: ContextTypes.DEFAULT_TYPE):
    """Удаляет группу сообщений (ссылка юзера + служебные сообщения бота)."""
    data = context.job.data
    chat_id = data["chat_id"]
    for mid in data["msg_ids"]:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:  # noqa: BLE001
            # уже удалено / старше 48ч / нет прав — пропускаем
            continue


async def _add_url(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, chat_id):
    conn = context.bot_data["conn"]
    # магазин, который не парсится без резидентного прокси (Cloudflare и т.п.)
    if monitor.is_proxy_required(url):
        try:
            from urllib.parse import urlparse
            host = urlparse(url).netloc.lower()
            if host.startswith("www."):
                host = host[4:]
        except Exception:  # noqa: BLE001
            host = url
        await update.message.reply_text(
            f"❌ Магазин <b>{host}</b> наразі не підтримується ботом. "
            f"Оберіть, будь ласка, інший магазин."
        )
        return
    existing = conn.execute(
        "SELECT id FROM items WHERE url = ? AND active = 1", (url,)
    ).fetchone()
    if existing:
        dup_msg = await update.message.reply_text(
            f"✅ Це посилання вже відстежується: #{existing['id']}"
        )
        # удаляем сообщение юзера со ссылкой + ответ бота через 2 хвилини
        context.job_queue.run_once(
            _delete_messages_job,
            CLEANUP_DELAY_SECONDS,
            data={"chat_id": chat_id,
                  "msg_ids": [update.message.message_id, dup_msg.message_id]},
            name=f"cleanup_dup_{existing['id']}",
        )
        return
    # --- лимит активных товаров на юзера ---
    if not (ADMIN_ID and str(chat_id) == str(ADMIN_ID)):
        limit = db.get_link_limit(conn, chat_id)
        count = db.count_active(conn, chat_id)
        if limit and count >= limit:
            # уже есть открытый запрос — не спамим повторно
            if db.get_open_limit_request(conn, chat_id):
                await update.message.reply_text(
                    f"⏳ Ваш запит на +10 посилань уже надіслано. Очікуйте рішення власника."
                )
                return
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("➕ Запитати +10", callback_data=f"req_limit:{chat_id}"),
            ]])
            await update.message.reply_text(
                f"🚫 Ліміт посилань: <b>{count}/{limit}</b> на людину.\n"
                f"Щоб отримати ще 10 — надішліть запит власнику бота.",
                parse_mode="HTML", reply_markup=kb,
            )
            return
    item_id = db.add_item(conn, url, str(chat_id))
    checking_msg = await update.message.reply_text(f"🔄 Перевіряю посилання #{item_id}…")
    item = db.get_item(conn, item_id)
    res = await monitor.check_item(conn, item, bot=context.bot)
    # собираем ID сообщений для авто-удаления через пару минут
    to_delete = [update.message.message_id, checking_msg.message_id]
    if res.get("ok"):
        if res.get("direction") == "new":
            added_msg = await update.message.reply_text(
                f"✅ Додано #{item_id}\n📦 {item['title'] or url}\n💰 {res['new']:.2f} {res['currency']}"
            )
            to_delete.append(added_msg.message_id)
        else:
            added_msg = await update.message.reply_text(
                f"✅ Оновлено #{item_id}: {res['new']:.2f} {res['currency']}"
            )
            to_delete.append(added_msg.message_id)
    else:
        domain = monitor.shop_domain(url)
        # магазин не в чёрном списке, но цену не взяли — неизвестный магазин
        conn.execute(
            "UPDATE items SET shop_status = 'unknown', active = 0 WHERE id = ?", (item_id,)
        )
        conn.commit()
        added_msg = await update.message.reply_text(
            f"⚠️ Посилання #{item_id} додано, але ціну не вдалося визначити "
            f"({res.get('error')}). Перевірю пізніше."
        )
        to_delete.append(added_msg.message_id)
        # админу — только при ПЕРВОМ появлении этого магазина
        if ADMIN_ID and db.mark_unknown_shop(conn, domain, str(chat_id), url):
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Взяв у роботу", callback_data=f"shop_taken:{domain}"),
            ]])
            await context.bot.send_message(
                chat_id=int(ADMIN_ID),
                text=f"🔧 <b>Новий невідомий магазин</b> потребує дописання парсера:\n"
                     f"🌐 Домен: <code>{domain}</code>\n"
                     f"👤 Юзер: <code>{chat_id}</code>\n"
                     f"🔗 Приклад: {url}",
                parse_mode="HTML", reply_markup=kb,
            )
    # сообщение "Взяв на моніторинг" (из monitor.check_item)
    if res.get("monitor_msg_id"):
        to_delete.append(res["monitor_msg_id"])
    # удаляем всё через 2 хвилини, щоб не засмічувати чат
    context.job_queue.run_once(
        _delete_messages_job,
        CLEANUP_DELAY_SECONDS,
        data={"chat_id": chat_id, "msg_ids": to_delete},
        name=f"cleanup_{item_id}",
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


async def cb_shop_taken(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Админ нажал «Взяв у роботу» у уведомлении про неизвестный магазин."""
    q = update.callback_query
    domain = q.data.split(":", 1)[1]
    conn = context.bot_data["conn"]
    db.set_unknown_shop_taken(conn, domain)
    # реактивируем все товары этого магазина (когда допишем парсер — заработают)
    conn.execute(
        "UPDATE items SET active = 1 WHERE shop_status = 'unknown' AND url LIKE ?",
        (f"%{domain}%",),
    )
    conn.commit()
    await q.answer("✅ Прийнято")
    await q.edit_message_text(
        f"✅ Магазин <code>{domain}</code> взято у роботу. Товари реактивовано "
        f"(після дописання парсера ціни оновляться).",
        parse_mode="HTML",
    )


# --- Лимиты: запрос юзера + аппрув админом ---

async def cb_req_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Юзер нажал «Запитати +10» — шлём админу уведомление с кнопками."""
    q = update.callback_query
    chat_id = q.data.split(":", 1)[1]
    conn = context.bot_data["conn"]
    if db.get_open_limit_request(conn, chat_id):
        await q.answer("Запит уже надіслано ✅")
        return
    user = db.get_user(conn, chat_id)
    uname = (user or {}).get("username") or "—"
    fname = q.from_user.full_name if q.from_user else "—"
    label = f"@{uname}" if uname != "—" else (fname or str(chat_id))
    count = db.count_active(conn, chat_id)
    limit = db.get_link_limit(conn, chat_id)
    db.mark_limit_request(conn, chat_id, "open")
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Підтвердити +10", callback_data=f"limit_ok:{chat_id}"),
        InlineKeyboardButton("❌ Відхилити", callback_data=f"limit_deny:{chat_id}"),
    ]])
    if ADMIN_ID:
        await context.bot.send_message(
            chat_id=int(ADMIN_ID),
            text=f"🔔 <b>Запит на +10 посилань</b>\n"
                 f"👤 Юзер: {label} (ID: <code>{chat_id}</code>)\n"
                 f"📊 Зараз на моніторингу: <b>{count}</b> з ліміту {limit}\n"
                 f"📈 Після +10 буде: <b>{count}</b> з {limit + 10}",
            parse_mode="HTML", reply_markup=kb,
        )
    await q.answer("Запит надіслано ✅")
    try:
        await q.edit_message_text(
            f"⏳ Запит на +10 посилань надіслано власнику. Очікуйте підтвердження."
        )
    except Exception:  # noqa: BLE001
        pass


async def cb_limit_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Админ подтвердил +10."""
    q = update.callback_query
    chat_id = q.data.split(":", 1)[1]
    conn = context.bot_data["conn"]
    db.bump_link_limit(conn, chat_id, 10)
    db.mark_limit_request(conn, chat_id, "approved")
    limit = db.get_link_limit(conn, chat_id)
    count = db.count_active(conn, chat_id)
    await q.answer("✅ Підтверджено +10")
    await q.edit_message_text(
        f"✅ <code>{chat_id}</code>: ліміт збільшено на 10. Тепер новий ліміт: <b>{limit}</b>.",
        parse_mode="HTML",
    )
    try:
        await context.bot.send_message(
            chat_id=int(chat_id),
            text=f"✅ Вам підтверджено +10 посилань! Новий ліміт: <b>{limit}</b> "
                 f"(зараз на моніторингу {count}).",
            parse_mode="HTML",
        )
    except Exception:  # noqa: BLE001
        pass


async def cb_limit_deny(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Админ отклонил."""
    q = update.callback_query
    chat_id = q.data.split(":", 1)[1]
    conn = context.bot_data["conn"]
    db.mark_limit_request(conn, chat_id, "denied")
    await q.answer("❌ Відхилено")
    await q.edit_message_text(
        f"❌ <code>{chat_id}</code>: запит відхилено.",
        parse_mode="HTML",
    )
    try:
        await context.bot.send_message(
            chat_id=int(chat_id),
            text="❌ Вам відмовлено у збільшенні ліміту. Звільніть місце, видаливши "
                 "частину посилань (команда /list → видалити) — і зможете додати нові.",
        )
    except Exception:  # noqa: BLE001
        pass


async def shops_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает список магазинов: поддерживаемые + неподдерживаемые."""
    conn = context.bot_data["conn"]
    shops = db.list_known_shops(conn)
    unknown = db.list_unknown_shops(conn)
    blocked = list(monitor._PROXY_REQUIRED_HOSTS)

    parts = []
    if shops:
        parts.append("🛒 <b>Магазини, які бот уміє читати:</b>")
        for s in shops:
            parts.append(f"• {s['domain']}")
        parts.append(f"Всього: {len(shops)}")
    else:
        parts.append("🛒 Поки немає перевірених магазинів у базі.")

    if blocked:
        parts.append("\n⛔ <b>Не підтримується:</b>")
        for d in sorted(blocked):
            parts.append(f"• {d}")

    if unknown:
        parts.append("\n🔧 <b>Невідомі (треба дописати парсер):</b>")
        for u in unknown:
            taken = " ✅ взято" if u["taken"] else ""
            parts.append(f"• {u['domain']}{taken}")

    await update.message.reply_text("\n".join(parts), parse_mode="HTML")


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
    chat_id = str(update.effective_chat.id)
    # проверяем, что товар существует и принадлежит этому юзеру
    item = conn.execute(
        "SELECT id, chat_id FROM items WHERE id = ? AND active = 1", (item_id,)
    ).fetchone()
    if not item:
        await update.message.reply_text("❌ Немає такого id.")
        return
    if item["chat_id"] != chat_id and chat_id != ADMIN_ID:
        await update.message.reply_text("⛔ Це не ваш товар — видаляти можна лише свої.")
        return
    ok = db.remove_item(conn, item_id, chat_id=None if chat_id == ADMIN_ID else chat_id)
    await update.message.reply_text("✅ Видалено." if ok else "❌ Не вдалося видалити.")


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
    cid = str(update.effective_chat.id)
    it = db.get_item(conn, item_id)
    if not it:
        await update.message.reply_text("❌ Немає такого id.")
        return
    if it["chat_id"] != cid and cid != ADMIN_ID:
        await update.message.reply_text("⛔ Це не ваш товар — історію видно лише для своїх.")
        return
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
    # тільки для адміна
    if str(update.effective_chat.id) != ADMIN_ID:
        await update.message.reply_text("⛔ Команда тільки для адміна.")
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
    # защита: удалять можно только свои товары (админ — любые)
    cid = str(q.message.chat_id)
    if it["chat_id"] != cid and cid != ADMIN_ID:
        await q.answer("⛔ Це не ваш товар", show_alert=True)
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
    it = db.get_item(conn, item_id)
    if not it:
        await q.answer("Вже видалено")
        return
    cid = str(q.message.chat_id)
    if it["chat_id"] != cid and cid != ADMIN_ID:
        await q.answer("⛔ Це не ваш товар", show_alert=True)
        return
    db.remove_item(conn, item_id, chat_id=None if cid == ADMIN_ID else cid)
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
    cid = str(q.message.chat_id)
    it = db.get_item(conn, item_id)
    if not it:
        await q.answer("Немає такого товару")
        return
    if it["chat_id"] != cid and cid != ADMIN_ID:
        await q.answer("⛔ Це не ваш товар", show_alert=True)
        return
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
    # админ = безлимит (link_limit=0)
    db.ensure_admin_unlimited(app.bot_data["conn"], ADMIN_ID)
    await app.bot.set_my_commands([
        BotCommand("list", "📋 Мої товари"),
        BotCommand("shops", "🛒 Які магазини підтримуються"),
        BotCommand("clear", "🧹 Очистити чат"),
        BotCommand("history", "📜 Історія цін (/history <id>)"),
        BotCommand("help", "❓ Довідка"),
    ])
    logger.info("Bot commands menu registered")


def main():
    if not BOT_TOKEN:
        raise SystemExit("❌ Не задано BOT_TOKEN (середовище або .env)")
    conn = db.connect()
    # ensure unknown_shops + known_shops tables exist at startup
    db.ensure_unknown_shops_table(conn)
    db.ensure_known_shops_table(conn)
    # авто-апрув владельца бота при старте, чтобы статус не слетал после рестартов
    if ADMIN_ID:
        db.upsert_pending(conn, ADMIN_ID, None)
        db.set_user_status(conn, ADMIN_ID, "approved")
        logger.info("Admin %s auto-approved on startup", ADMIN_ID)
    from telegram.ext import JobQueue
    # PTB >=22.7 не создаёт job_queue автоматически — инициализируем явно
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).job_queue(JobQueue()).build()
    app.bot_data["conn"] = conn

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("check", check_cmd))
    app.add_handler(CommandHandler("shops", shops_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))

    app.add_handler(CallbackQueryHandler(cb_del, pattern="^del:"))
    app.add_handler(CallbackQueryHandler(cb_dodel, pattern="^dodel:"))
    app.add_handler(CallbackQueryHandler(cb_hist, pattern="^hist:"))
    app.add_handler(CallbackQueryHandler(cb_approve, pattern="^approve:"))
    app.add_handler(CallbackQueryHandler(cb_deny, pattern="^deny:"))
    app.add_handler(CallbackQueryHandler(cb_shop_taken, pattern="^shop_taken:"))
    app.add_handler(CallbackQueryHandler(cb_req_limit, pattern="^req_limit:"))
    app.add_handler(CallbackQueryHandler(cb_limit_approve, pattern="^limit_ok:"))
    app.add_handler(CallbackQueryHandler(cb_limit_deny, pattern="^limit_deny:"))
    app.add_handler(CallbackQueryHandler(cb_noop, pattern="^noop$"))

    app.add_error_handler(error_handler)

    for hh, mm in CHECK_TIMES:
        app.job_queue.run_daily(
            scheduled_check,
            time=dtime(hh, mm, tzinfo=KYIV),
            name=f"price_check_{hh:02d}{mm:02d}",
        )
    logger.info("Bot started. Scheduled checks (Kyiv): %s", CHECK_TIMES)
    app.run_polling()


if __name__ == "__main__":
    main()
