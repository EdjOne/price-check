"""Проверка цен по всем активным товарам + отправка алертов в Telegram.

fetch(): сначала простой requests; если сайт отдаёт 403 / Cloudflare-челлендж —
автоматически fallback на headless-браузер (Playwright), если он установлен.
"""
from __future__ import annotations

import asyncio
import logging
import os
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
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}

TIMEOUT = 25

# Резидентный прокси для обхода Cloudflare Managed Challenge (напр. maudau.com.ua).
# Берётся из .env (PROXY_URL). Пусто — без прокси.
PROXY_URL = os.getenv("PROXY_URL", "").strip() or None

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


def _is_js_challenge(html: str) -> bool:
    """Кастомный JS-челлендж магазина (не Cloudflare).

    Сайт отдаёт крошечную HTML-заглушку со скриптом, который крутит
    цикл, ставит cookie (напр. challenge_passed) и делает reload.
    Признаки: короткий body + скрипт с document.cookie и location.reload().
    Такие страницы надо отдавать в headless-браузер (Playwright), который
    выполнит JS и получит реальный контент.
    """
    if not html:
        return False
    h = html.lower()
    cookie_set = "document.cookie" in h
    reloads = "location.reload" in h or "location.href" in h
    # типичный маркер собственного челленджа biom.ua и ему подобных
    known_marker = "challenge_passed" in h
    # AWS WAF JavaScript challenge (makeup.com.ua и др.)
    aws_waf = "awswafintegration" in h or "awsWafCookieDomainList" in h
    return (known_marker or aws_waf
            or (cookie_set and reloads and len(html) < 5000))


# Маркеры страниц ошибок (404 / 403 / not found) — их нельзя трактовать как товар.
_ERROR_TITLE_HINTS = (
    "помилка 404", "страница не найдена", "сторінка не знайдена",
    "page not found", "not found", "404 not found", "error 404",
    "запрашиваемая страница", "does not exist", "не существует",
)


def _is_error_page(html: str, title: str | None = None) -> bool:
    """True, если страница — это 404/403/Not Found вместо товара."""
    if not html:
        return False
    t = (title or "").strip().lower()
    if t and any(h in t for h in _ERROR_TITLE_HINTS):
        return True
    # иногда title нормальный, но в теле торчит «Помилка 404»
    head = html[:4000].lower()
    return any(h in head for h in ("помилка 404", "error 404", "404 not found",
                                   "page not found", "страница не найдена",
                                   "сторінка не знайдена"))


async def _fetch_requests(url):
    try:
        r = await asyncio.to_thread(
            requests.get, url, headers=HEADERS, timeout=TIMEOUT, proxies=_proxies()
        )
        return r, None
    except Exception as exc:  # noqa: BLE001
        return None, exc


def _proxies():
    if not PROXY_URL:
        return None
    return {"http": PROXY_URL, "https": PROXY_URL}


async def _fetch_playwright(url):
    if not PLAYWRIGHT_AVAILABLE:
        return None, "Playwright не встановлено"
    try:
        from urllib.parse import urlparse
        tld = (urlparse(url).netloc.split(".")[-1] or "").lower()
        # для молдавских сайтов — локаль браузера молдавская
        md = tld == "md"
        launch_kwargs = {
            # headless=True (НЕ --headless=new!): AWS WAF и ряд других
            # JS-челленджей детектят --headless=new и блокируют браузер.
            # Старый headless=True + AutomationControlled off проходит чище.
            "headless": True,
            "args": [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        }
        if PROXY_URL:
            launch_kwargs["proxy"] = {"server": PROXY_URL}
        async with async_playwright() as p:
            browser = await p.chromium.launch(**launch_kwargs)
            ctx = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="ro-MD" if md else "uk-UA",
                timezone_id="Europe/Chisinau" if md else "Europe/Kyiv",
                viewport={"width": 1366, "height": 768},
                # NB: НЕ ставим extra_http_headers (Sec-CH-UA и т.п.) —
                # AWS WAF их детектит как бота и отдаёт JS-заглушку.
            )
            # прячем navigator.webdriver
            await ctx.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', "
                "{get: () => undefined});"
            )
            page = await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            # даём Cloudflare/AWS WAF время пройти JS-челлендж.
            # Сначала пробуем дождаться networkidle, затем — явно ждём,
            # пока страница перестанет быть заглушкой челленджа
            # (AWS WAF делает forceRefresh и подменяет контент).
            try:
                await page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:  # noqa: BLE001
                pass
            try:
                await page.wait_for_function(
                    "() => { const t = document.body.innerText || ''; "
                    "const h = document.documentElement.outerHTML || ''; "
                    "return !/зачекайте|just a moment|verify you are human"
                    "|javascript is disabled|awswafintegration/i.test((t + h).toLowerCase()) "
                    "&& document.querySelectorAll('[class*=price], h1, [itemprop=name]').length > 0; }",
                    timeout=30000,
                )
            except Exception:  # noqa: BLE001
                try:
                    # запасной варіант: просто чекаємо, поки WAF доробить
                    await page.wait_for_timeout(15000)
                except Exception:  # noqa: BLE001
                    pass
            html = await page.content()
            await browser.close()
            return html, None
    except Exception as exc:  # noqa: BLE001
        return None, f"Playwright: {exc}"


# Домены-сокращатели (deeplink из приложений/рекламы), которые сами по себе
# не содержат товар — нужно выполнить JS-редирект через headless-браузер,
# чтобы получить реальный URL товара.
_DEEPLINK_HOSTS = ("link.silpo.ua",)


def _is_deeplink(url: str) -> bool:
    try:
        from urllib.parse import urlparse
        host = urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        return any(host == h or host.endswith("." + h) for h in _DEEPLINK_HOSTS)
    except Exception:
        return False


async def _resolve_deeplink(url: str) -> str | None:
    """Выполняет JS-редирект короткой ссылки и возвращает реальный URL товара.

    Возвращает None, если резолв не удался (Playwright недоступен / таймаут).
    """
    if not PLAYWRIGHT_AVAILABLE:
        return None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage",
                      "--disable-blink-features=AutomationControlled", "--headless=new"],
            )
            ctx = await browser.new_context(
                user_agent=HEADERS["User-Agent"],
                locale="uk-UA", timezone_id="Europe/Kyiv",
                viewport={"width": 1366, "height": 768},
            )
            await ctx.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            page = await ctx.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception:  # noqa: BLE001
                pass
            try:
                await page.wait_for_timeout(5000)
            except Exception:  # noqa: BLE001
                pass
            resolved = page.url
            await browser.close()
            if resolved and resolved != url and not _is_deeplink(resolved):
                return resolved
            return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("resolve_deeplink %s failed: %s", url, exc)
        return None


async def fetch(url: str) -> tuple[str | None, str | None]:
    """Возвращает (html, error). error=None при успехе."""
    r, err = await _fetch_requests(url)
    if (r is not None and r.status_code == 200
            and not _is_cloudflare(r.text) and not _is_js_challenge(r.text)):
        # доп. защита: страница ошибки с кодом 200 (редко)
        if not _is_error_page(r.text):
            return r.text, None
        logger.warning("fetch %s: страница-ошибка (200) — не берём", url)
        return None, "сторінка не знайдена (404/помилка)"
    if r is not None and r.status_code != 200:
        # 404/5xx нельзя вылечить браузером — не тратим время на Playwright
        if r.status_code == 404:
            logger.warning("fetch %s: HTTP 404", url)
            return None, "сторінка не знайдена (HTTP 404)"
        if r.status_code == 403:
            logger.warning("fetch %s: HTTP 403 (возможно защита)", url)
        else:
            logger.warning("fetch %s: HTTP %s, пробуем Playwright", url, r.status_code)

    html, perr = await _fetch_playwright(url)
    if html:
        # Playwright мог тоже отдать 404-страницу — проверяем
        if _is_error_page(html):
            return None, "сторінка не знайдена (404/помилка)"
        return html, None
    if r is not None and r.status_code != 200:
        reason = "Cloudflare/защита сайта" if r.status_code == 403 else f"HTTP {r.status_code}"
        return None, f"не удалося завантажити: {reason}"
    return None, f"не удалося завантажити: {perr}"


async def check_item(conn, item, bot=None) -> dict:
    item_id = item["id"]
    url = item["url"]
    # короткие deeplink-ссылки (link.silpo.ua и т.п.) резолвим в реальный URL
    # один раз и кэшируем в resolved_url, чтобы не гонять Playwright каждый раз
    effective_url = url
    if _is_deeplink(url):
        cached = item["resolved_url"] if "resolved_url" in item.keys() else None
        if cached:
            effective_url = cached
        else:
            resolved = await _resolve_deeplink(url)
            if resolved:
                effective_url = resolved
                db.set_resolved_url(conn, item_id, resolved)
                logger.info("deeplink %s -> %s", url, resolved)
    html, err = await fetch(effective_url)
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
            result["monitor_msg_id"] = await _send_alert(
                bot, item, old, price, result["currency"], direction, title
            )
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
        sent = await bot.send_message(chat_id=item["chat_id"], text=msg, parse_mode="HTML")
        return sent.message_id
    except Exception as exc:  # noqa: BLE001
        logger.warning("alert send failed %s: %s", item["chat_id"], exc)
        return None


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
