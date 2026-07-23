"""Проверка цен по всем активным товарам + отправка алертов в Telegram.

fetch(): сначала простой requests; если сайт отдаёт 403 / Cloudflare-челлендж —
автоматически fallback на headless-браузер (Playwright), если он установлен.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
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
            or "__cf_chl" in h
            or "enable javascript and cookies to continue" in h
            # украиноязычная / локализованная страница проверки Cloudflare
            or "триває перевірка безпеки" in h
            or "сервіс безпеки" in h
            or "перевірка пройшла успішно" in h
            or "перевірка безпеки" in h and "не бот" in h)


_JS_CHALLENGE_COOKIE_RE = re.compile(
    r'(?:const\s+defaultHash\s*=\s*"|"challenge_passed"\s*\+\s*)([a-f0-9]+)'
)


def _js_challenge_cookie(html: str) -> str | None:
    """Извлекает хеш из JS-challenge и возвращает cookie-строку.

    Если страница — примитивный JS-challenge (primeauto.com.ua, biom.ua),
    возвращает "challenge_passed=<хеш>", иначе None.
    Такой челлендж можно обойти без браузера: извлечь хеш, установить куку,
    перезапросить.
    """
    if not html:
        return None
    m = _JS_CHALLENGE_COOKIE_RE.search(html)
    if m:
        return f"challenge_passed={m.group(1)}"
    return None


def _is_js_challenge(html: str) -> bool:
    """Кастомный JS-челлендж магазина (не Cloudflare).

    Сайт отдаёт крошечную HTML-заглушку со скриптом, который крутит
    цикл, ставит cookie (напр. challenge_passed) и делает reload.
    Признаки: короткий body + скрипт с document.cookie и location.reload().
    """
    if not html:
        return False
    h = html.lower()
    cookie_set = "document.cookie" in h
    reloads = "location.reload" in h or "location.href" in h
    known_marker = "challenge_passed" in h
    # AWS WAF JavaScript challenge (makeup.com.ua и др.)
    aws_waf = "awswafintegration" in h or "awsWafCookieDomainList" in h
    return (known_marker or aws_waf
            or (cookie_set and reloads and len(html) < 5000))


def _looks_empty_spa(html: str) -> bool:
    """True, если это пустой SPA-скелет без товара (контент грузится JS).

    Некоторые магазины (fora.ua и др.) отдают на requests валидный HTTP 200,
    но HTML — это короткий каркас React/Vue без цены; реальные данные
    подгружаются XHR-запросами уже в браузере. Такой ответ нельзя парсить —
    надо отдать URL в Playwright, который выполнит JS и дождётся цены.

    Признак: в HTML нет НИ одного источника цены (ld+json с price / og:price /
    itemprop=price / число рядом с грн/₴/UAH) И присутствуют JS-бандлы SPA.
    """
    if not html:
        return False
    h = html.lower()
    has_price_signal = (
        '"price"' in h
        or "og:price" in h
        or 'itemprop="price"' in h
        or "product:price" in h
        or re.search(r"\d[\d\s.,]*\s*(грн|₴|uah)", h) is not None
    )
    if has_price_signal:
        return False
    # признаки SPA-каркаса: react/vue-бандлы или пустой root-контейнер
    spa_marker = (
        "/js/react" in h
        or "webpackchunk" in h
        or 'id="root"' in h
        or 'id="app"' in h
        or "data-react" in h
    )
    return spa_marker


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


async def _fetch_requests(url, extra_cookies=None):
    try:
        r = await asyncio.to_thread(
            requests.get, url, headers=HEADERS, timeout=TIMEOUT,
            proxies=_proxies(), cookies=extra_cookies,
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

        # Для большинства сайтов — --headless=new (лучше проходит Amazon и др.).
        # Для AWS WAF (makeup.com.ua) — старый headless=True (--headless=new
        # детектится WAF и отдаёт заглушку).
        use_old_headless = _is_headless_old(url)
        # Определяем локаль и таймзону по TLD сайта (для Amazon и др.)
        tld_locale = {
            "pl": "pl-PL",
            "de": "de-DE",
            "fr": "fr-FR",
            "it": "it-IT",
            "es": "es-ES",
            "uk": "uk-UA",
            "ua": "uk-UA",
            "md": "ro-MD",
        }
        tld_tz = {
            "pl": "Europe/Warsaw",
            "de": "Europe/Berlin",
            "fr": "Europe/Paris",
            "it": "Europe/Rome",
            "es": "Europe/Madrid",
            "uk": "Europe/Kyiv",
            "ua": "Europe/Kyiv",
            "md": "Europe/Chisinau",
        }
        locale_str = tld_locale.get(tld, "uk-UA")
        timezone_str = tld_tz.get(tld, "Europe/Kyiv")
        # Для Amazon используем современный Chrome 130, иначе 124
        is_amazon = "amazon." in urlparse(url).netloc.lower()
        ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36" if is_amazon
              else HEADERS["User-Agent"])
        launch_kwargs = {
            "headless": False,  # не используем логическое headless — вместо этого args
            "args": [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        }
        if not use_old_headless:
            launch_kwargs["args"].append("--headless=new")
        else:
            launch_kwargs["headless"] = True

        if PROXY_URL:
            launch_kwargs["proxy"] = {"server": PROXY_URL}
        async with async_playwright() as p:
            browser = await p.chromium.launch(**launch_kwargs)
            ctx = await browser.new_context(
                user_agent=ua,
                locale=locale_str,
                timezone_id=timezone_str,
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
                    "const clean = !/зачекайте|just a moment|verify you are human"
                    "|javascript is disabled|awswafintegration/i.test((t + h).toLowerCase()); "
                    "if (!clean) return false; "
                    "const hasNode = document.querySelectorAll('[class*=price], h1, [itemprop=name]').length > 0; "
                    "const hasPriceText = /\\d[\\d\\s.,]*\\s*(грн|₴|uah|zł|pln|€|$)/i.test(t); "
                    "return hasNode || hasPriceText; }",
                    timeout=30000,
                )
            except Exception:  # noqa: BLE001
                try:
                    # запасной варіант: просто чекаємо, поки WAF/SPA доробить
                    await page.wait_for_timeout(15000)
                except Exception:  # noqa: BLE001
                    pass
            html = await page.content()

            # JS-challenge (primeauto, biom та ін.): після location.reload
            # сторінка має завантажитися знову. Якщо перший прохід дав
            # challenge-заглушку — робимо другий goto (з cookie).
            if html and _is_js_challenge(html):
                logger.info(
                    "_fetch_playwright %s: challenge-заглушка, retry goto",
                    url,
                )
                try:
                    await page.goto(url, wait_until="domcontentloaded",
                                    timeout=30000)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    await page.wait_for_timeout(3000)
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


# Магазины, которые НЕ проходят даже `--headless=new` — нужен старый
# headless=True (без аргумента --headless). Пока только AWS WAF (makeup.com.ua).
_HEADLESS_OLD_HOSTS = (
    "makeup.com.ua",
)


def _is_headless_old(url: str) -> bool:
    """True, если магазин надо запускать со старым headless=True."""
    try:
        from urllib.parse import urlparse
        host = urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        return any(host == h or host.endswith("." + h) for h in _HEADLESS_OLD_HOSTS)
    except Exception:
        return False


# Магазины, где цена грузится через JS/AJAX уже ПОСЛЕ загрузки DOM
# (старые jQuery-сайты, часть SPA без React-маркеров). Обычный requests
# получает HTML без цены — такие URLs всегда парсим через headless-браузер.
# НЕ путать с _PROXY_REQUIRED_HOSTS (там Cloudflare-челлендж, цену не взять
# даже в браузере без прокси). Сюда пишем магазины, где Playwright цену БЕРЁТ.
_PLAYWRIGHT_ALWAYS = (
    "styx.odessa.ua",
    "amazon.pl",
)


def is_playwright_forced(url: str) -> bool:
    """True, если магазин из URL надо всегда парсить через браузер."""
    try:
        from urllib.parse import urlparse
        host = urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        return any(host == h or host.endswith("." + h) for h in _PLAYWRIGHT_ALWAYS)
    except Exception:  # noqa: BLE001
        return False


# Магазины, которые НЕ парсятся ботом без резидентного прокси (Cloudflare
# Managed Challenge и т.п. — реального контента в ответе нет, только заглушка
# «зачекайте»). Добавлять сюда ТОЛЬКО проверенные случаи, где fetch() реально
# возвращает 403/челлендж и цену не вытащить. НЕ писать сюда рабочие магазины!
_PROXY_REQUIRED_HOSTS = (
    "ya.ua",
    "deka.ua",
    "4f.ua",
    "hm.com",
    "leroymerlin.ua",
    "allegro.pl",
)


def is_proxy_required(url: str) -> bool:
    """True, если магазин из URL требует резидентный прокси (не парсится)."""
    try:
        from urllib.parse import urlparse
        host = urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        return any(host == h or host.endswith("." + h) for h in _PROXY_REQUIRED_HOSTS)
    except Exception:  # noqa: BLE001
        return False


def shop_domain(url: str) -> str:
    """Возвращает SLD магазина из URL: https://silpo.ua/x -> silpo.ua."""
    try:
        from urllib.parse import urlparse
        host = urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:  # noqa: BLE001
        return url


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


async def _fetch_with_challenge_bypass(url: str) -> str | None:
    """Пробует обойти примитивный JS-challenge через requests + cookie.

    Некоторые магазины (primeauto.com.ua, biom.ua) отдают JS-заглушку,
    которая ставит cookie и reload'ит страницу. Хеш можно извлечь из
    скрипта, установить куку и перезапросить — без Playwright.
    """
    try:
        r, _ = await _fetch_requests(url)
        if r is None:
            return None
        if not _is_js_challenge(r.text):
            return None  # не челлендж — пусть обработчик решает
        c = _js_challenge_cookie(r.text)
        if not c:
            return None
        logger.info("_fetch_with_challenge_bypass %s: кука %s", url, c)
        # парсим "challenge_passed=abc" в {"challenge_passed": "abc"}
        parts = c.split("=", 1)
        cookie_dict = {parts[0]: parts[1]} if len(parts) == 2 else None
        if not cookie_dict:
            return None
        # повторный запрос с кукой
        r2, _ = await _fetch_requests(url, extra_cookies=cookie_dict)
        if r2 is None:
            return None
        if _is_js_challenge(r2.text) or _is_cloudflare(r2.text):
            return None
        return r2.text
    except Exception as exc:
        logger.warning("_fetch_with_challenge_bypass %s: %s", url, exc)
        return None


async def fetch(url: str) -> tuple[str | None, str | None]:
    """Возвращает (html, error). error=None при успехе."""
    # магазины, где цена грузится JS/AJAX после загрузки DOM —
    # requests не берёт, сразу идём в браузер. Но сначала пробуем
    # обойти примитивный JS-challenge через requests + cookie.
    if is_playwright_forced(url):
        html = await _fetch_with_challenge_bypass(url)
        if html:
            return html, None
        html, perr = await _fetch_playwright(url)
        if html and not _is_error_page(html):
            return html, None
        return None, f"не удалося завантажити (Playwright): {perr}"
    r, err = await _fetch_requests(url)
    if r is not None and r.status_code == 200:
        # проверка: если это JS-challenge — пробуем обойти
        if _is_js_challenge(r.text):
            html = await _fetch_with_challenge_bypass(url)
            if html:
                return html, None
            # не вышло — падаем в Playwright ниже
        elif not _is_cloudflare(r.text) and not _looks_empty_spa(r.text):
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

    # магазин реально отдал цену — отмечаем как проверенный (known_shops)
    try:
        was_new = db.touch_known_shop(conn, shop_domain(url))
        # если магазин был НЕИЗВЕСТНЫМ и теперь заработал — чистим очереди
        if was_new:
            domain = shop_domain(url)
            # убираем из unknown_shops
            conn.execute("DELETE FROM unknown_shops WHERE domain = ?", (domain,))
            # переводим все товары этого магазина с unknown → known
            conn.execute(
                "UPDATE items SET shop_status='known' WHERE url LIKE ? AND shop_status='unknown'",
                (f"%{domain}%",),
            )
            conn.commit()
            logger.info("shop %s — перенесён из unknown в known (авто)", domain)
    except Exception:  # noqa: BLE001
        pass

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
