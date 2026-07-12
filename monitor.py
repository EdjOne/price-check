"""Проверка цен по всем активным товарам + отправка алертов в Telegram.

fetch(): сначала простой requests; если сайт отдаёт 403 / Cloudflare-челлендж —
автоматически fallback на headless-браузер (Playwright), если он установлен.
"""
from __future__ import annotations

import asyncio
import logging
import requests
from datetime import datetime, timezone

import db
import parser as price_parser

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
              "image/webp,*/*;q=0.8",
    "Accept-Language": "uk-UA,uk;q=0.9,ru;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}

TIMEOUT = 25

PLAYWRIGHT_AVAILABLE = False
try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    pass


def _is_cloudflare(html: str) -> bool:
    if not html:
        return False
    h = html.lower()
    return ("just a moment" in h or "cf-chl" in h
            or "challenge-platform" in h or "__cf_chl" in h
            or "enable javascript and cookies to continue" in h)


async def _fetch_requests(url):
    try:
        r = await asyncio.to_thread(
            requests.get, url, headers=HEADERS, timeout=TIMEOUT
        )
        return r, None
    except Exception as exc:  # noqa: BLE001
        return None, exc


async def _fetch_playwright(url):
    if not PLAYWRIGHT_AVAILABLE:
        return None, "Playwright не встановлено"
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            ctx = await browser.new_context(
                user_agent=HEADERS["User-Agent"],
                locale="uk-UA",
                extra_http_headers={
                    "Accept-Language": "uk-UA,uk;q=0.9,ru;q=0.8,en;q=0.7"
                },
            )
            page = await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            # ждём, пока прогрузится реальный контент (не заглушка "зачекайте")
            try:
                await page.wait_for_function(
                    "() => { const t = document.body.innerText || ''; "
                    "return !/зачекайте|just a moment/i.test(t) && document.querySelectorAll('[class*=price], h1, [itemprop=name]').length > 0; }",
                    timeout=25000,
                )
            except Exception:  # noqa: BLE001
                try:
                    await page.wait_for_timeout(8000)
                except Exception:  # noqa: BLE001
                    pass
            html = await page.content()
            await browser.close()
            return html, None
    except Exception as exc:  # noqa: BLE001
        return None, f"Playwright: {exc}"


async def fetch(url: str) -> tuple[str | None, str | None]:
    """Возвращает (html, error). error=None при успехе."""
    r, err = await _fetch_requests(url)
    if r is not None and r.status_code == 200 and not _is_cloudflare(r.text):
        return r.text, None

    if r is not None and r.status_code != 200:
        logger.warning("fetch %s: HTTP %s, пробуем Playwright", url, r.status_code)
    elif r is not None:
        logger.warning("fetch %s: Cloudflare-челлендж, пробуем Playwright", url)

    html, perr = await _fetch_playwright(url)
    if html:
        return html, None
    if r is not None and r.status_code != 200:
        reason = "Cloudflare/защита сайта" if r.status_code == 403 else f"HTTP {r.status_code}"
        return None, f"не удалося завантажити: {reason}"
    return None, f"не удалося завантажити: {perr}"


async def check_item(conn, item, bot=None) -> dict:
    item_id = item["id"]
    url = item["url"]
    html, err = await fetch(url)
    result = {"id": item_id, "url": url, "ok": False, "changed": False,
              "old": item["last_price"], "new": None, "currency": item["currency"],
              "error": None}

    if not html:
        result["error"] = err or "не удалося завантажити сторінку"
        return result

    price, currency, title = price_parser.extract(html, url)
    if price is None:
        result["error"] = "ціну не вдалося визначити"
        return result

    result["ok"] = True
    result["new"] = price
    result["currency"] = currency or result["currency"]

    if title and not item["title"]:
        conn.execute("UPDATE items SET title = ? WHERE id = ?", (title, item_id))

    old = item["last_price"]
    if old is None or old != price:
        direction = "new" if old is None else ("down" if price < old else "up")
        db.update_price(conn, item_id, price, result["currency"])
        result["changed"] = True
        result["direction"] = direction
        if bot is not None and item["chat_id"]:
            await _send_alert(bot, item, old, price, result["currency"], direction, title)
    else:
        conn.execute("UPDATE items SET last_checked = ? WHERE id = ?",
                     (datetime.now(timezone.utc).isoformat(), item_id))
        conn.commit()
    return result


async def _send_alert(bot, item, old, new, currency, direction, title):
    arrow = {"down": "🔻", "up": "🔺", "new": "🆕"}.get(direction, "")
    name = title or item["title"] or item["url"]
    if direction == "new":
        msg = (f"{arrow} Взял на мониторинг:\n<b>{name}</b>\n"
               f"💰 {new:.2f} {currency}\n🔗 {item['url']}")
    else:
        diff = new - old
        pct = (diff / old * 100) if old else 0
        msg = (f"{arrow} Цена изменилась!\n<b>{name}</b>\n"
               f"Было: {old:.2f} {currency}\n"
               f"Стало: {new:.2f} {currency} ({diff:+.2f}, {pct:+.1f}%)\n"
               f"🔗 {item['url']}")
    try:
        await bot.send_message(chat_id=item["chat_id"], text=msg, parse_mode="HTML")
    except Exception as exc:  # noqa: BLE001
        logger.warning("alert send failed %s: %s", item["chat_id"], exc)


async def check_all(conn, bot=None):
    items = db.all_active(conn)
    results = []
    for item in items:
        try:
            results.append(await check_item(conn, item, bot))
        except Exception as exc:  # noqa: BLE001
            logger.exception("check_item failed id=%s", item["id"])
            results.append({"id": item["id"], "ok": False, "error": str(exc)})
    return results
