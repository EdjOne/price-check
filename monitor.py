"""Проверка цен по всем активным товарам + отправка алертов в Telegram."""
from __future__ import annotations

import logging
import requests
from datetime import datetime, timezone

import db
import parser as price_parser

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

TIMEOUT = 25


def fetch(url: str) -> str | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        return r.text
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch failed %s: %s", url, exc)
        return None


def check_item(conn, item, bot=None) -> dict:
    """Проверяет один товар. Возвращает dict с результатом и шлёт алерт при изменении."""
    item_id = item["id"]
    url = item["url"]
    html = fetch(url)
    result = {"id": item_id, "url": url, "ok": False, "changed": False,
              "old": item["last_price"], "new": None, "currency": item["currency"],
              "error": None}

    if not html:
        result["error"] = "не удалось загрузить страницу"
        return result

    price, currency, title = price_parser.extract(html, url)
    if price is None:
        result["error"] = "цену не удалось определить"
        # не сбрасываем старую цену, просто пропускаем
        return result

    result["ok"] = True
    result["new"] = price
    result["currency"] = currency or result["currency"]

    # обновляем название, если раньше не было
    if title and not item["title"]:
        conn.execute("UPDATE items SET title = ? WHERE id = ?", (title, item_id))

    old = item["last_price"]
    if old is None or old != price:
        direction = "new" if old is None else ("down" if price < old else "up")
        db.update_price(conn, item_id, price, result["currency"])
        result["changed"] = True
        result["direction"] = direction

        if bot is not None and item["chat_id"]:
            _send_alert(bot, item, old, price, result["currency"], direction, title)
    else:
        # просто обновляем время проверки
        conn.execute("UPDATE items SET last_checked = ? WHERE id = ?",
                     (datetime.now(timezone.utc).isoformat(), item_id))
        conn.commit()

    return result


def _send_alert(bot, item, old, new, currency, direction, title):
    arrow = {"down": "🔻", "up": "🔺", "new": "🆕"}.get(direction, "")
    name = title or item["title"] or item["url"]
    if direction == "new":
        msg = f"{arrow} Взял на мониторинг:\n<b>{name}</b>\n💰 {new:.2f} {currency}\n🔗 {item['url']}"
    else:
        diff = new - old
        pct = (diff / old * 100) if old else 0
        msg = (
            f"{arrow} Цена изменилась!\n<b>{name}</b>\n"
            f"Было: {old:.2f} {currency}\n"
            f"Стало: {new:.2f} {currency} ({diff:+.2f}, {pct:+.1f}%)\n"
            f"🔗 {item['url']}"
        )
    try:
        bot.send_message(chat_id=item["chat_id"], text=msg, parse_mode="HTML")
    except Exception as exc:  # noqa: BLE001
        logger.warning("alert send failed %s: %s", item["chat_id"], exc)


def check_all(conn, bot=None):
    items = db.all_active(conn)
    results = []
    for item in items:
        try:
            results.append(check_item(conn, item, bot))
        except Exception as exc:  # noqa: BLE001
            logger.exception("check_item failed id=%s", item["id"])
            results.append({"id": item["id"], "ok": False, "error": str(exc)})
    return results
