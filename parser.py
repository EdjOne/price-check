"""Авто-детект цены и названия товара из HTML любого сайта.

Стратегии (по порядку):
  1. JSON-LD (schema.org Product/Offer) — есть у большинства магазинов
  2. Meta-теги (og:price:amount, product:price:amount, ...)
  3. Селекторы (.price, [itemprop=price], [data-price], ...)
  4. Регексп по валютному символу в тексте страницы

Возвращает (price: float|None, currency: str|None, title: str|None).
"""
from __future__ import annotations

import json
import re
from bs4 import BeautifulSoup

# Символ/слово валюты -> ISO-код
CURRENCY_MAP = {
    "₴": "UAH", "грн": "UAH", "грив": "UAH", "uah": "UAH",
    "$": "USD", "usd": "USD", "дол": "USD", "бакс": "USD",
    "€": "EUR", "eur": "EUR", "євро": "EUR", "евро": "EUR",
    "₽": "RUB", "руб": "RUB", "ruble": "RUB", "rub": "RUB",
    "zł": "PLN", "pln": "PLN", "злот": "PLN",
}

# Маркери валюти: символи + слова (шукаємо число поряд)
CURRENCY_MARKERS = ["₴", "$", "€", "₽", "zł",
                    "грн", "грив", "руб", "дол", "євро", "евро",
                    "uah", "usd", "eur", "rub", "pln"]


def _to_float(raw: str) -> float | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    s = s.replace("\u00a0", " ").replace("\u202f", " ")
    # убираем всё кроме цифр, точки, запятой и пробела
    s = re.sub(r"[^\d\s.,]", "", s)
    # приводим к единому десятичному разделителю
    if "," in s and "." in s:
        s = s.replace(" ", "").replace(",", ".")  # 1 234.56 или 1,234.56
    elif "," in s:
        # запятая как разделитель целой/дробной (европейский формат)
        s = s.replace(" ", "").replace(",", ".")
    else:
        s = s.replace(" ", "")
    s = s.strip(" .")
    if not re.search(r"\d", s):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _currency_from_text(text: str) -> str | None:
    low = text.lower()
    for word, code in CURRENCY_MAP.items():
        if word.lower() in low:
            return code
    return None


def _amount_near_currency(text: str) -> tuple[float | None, str | None]:
    """Берём число рядом с маркером валюты (символ або слово)."""
    low = text.lower()
    for marker in CURRENCY_MARKERS:
        is_word = marker.isalpha()
        idx = low.find(marker) if is_word else text.find(marker)
        while idx != -1:
            start = max(0, idx - 18)
            end = min(len(text), idx + len(marker) + 18)
            window = text[start:end]
            m = re.search(r"\d[\d\s]*[.,]?\d{0,2}", window)
            if m:
                amt = _to_float(m.group(0))
                if amt:
                    return amt, _currency_from_text(window)
            idx = (low.find(marker, idx + len(marker)) if is_word
                   else text.find(marker, idx + len(marker)))
    return None, None


def _title(soup: BeautifulSoup) -> str | None:
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        return og["content"].strip()
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(strip=True)
    return None


def _from_json_ld(soup: BeautifulSoup) -> tuple[float | None, str | None]:
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text() or ""
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        blocks = data if isinstance(data, list) else [data]

        def walk(block):
            offers = block.get("offers")
            if isinstance(offers, list):
                offers = offers[0] if offers else None
            if isinstance(offers, dict):
                price = offers.get("price") or offers.get("lowPrice") or offers.get("highPrice")
                cur = offers.get("priceCurrency")
                if price is not None:
                    return _to_float(str(price)), (cur or None)
            if "price" in block and block["price"] is not None:
                return _to_float(str(block["price"])), block.get("priceCurrency")
            # вложенные блоки
            for k, v in block.items():
                if isinstance(v, dict):
                    r = walk(v)
                    if r[0] is not None:
                        return r
            return None, None

        for b in blocks:
            if isinstance(b, dict):
                price, cur = walk(b)
                if price is not None:
                    return price, cur
    return None, None


def _from_meta(soup: BeautifulSoup) -> tuple[float | None, str | None]:
    amount_props = [
        "product:price:amount", "og:price:amount",
        "twitter:data1", "price", "og:product:price:amount",
    ]
    cur_props = ["product:price:currency", "og:price:currency", "twitter:label1"]
    cur = None
    for cp in cur_props:
        tag = soup.find("meta", attrs={"property": cp}) or soup.find("meta", attrs={"name": cp})
        if tag and tag.get("content"):
            code = tag["content"].strip().upper()
            if len(code) <= 3:
                cur = code
    for ap in amount_props:
        tag = soup.find("meta", attrs={"property": ap}) or soup.find("meta", attrs={"name": ap})
        if tag and tag.get("content"):
            amt = _to_float(tag["content"])
            if amt is not None:
                return amt, cur or _currency_from_text(tag["content"])
    return None, None


def _from_selectors(soup: BeautifulSoup) -> tuple[float | None, str | None]:
    sels = [
        '[itemprop="price"]', '[data-price]', '.price', '#price',
        '[class*="price"]', '[class*="Price"]', '[id*="price"]',
    ]
    for sel in sels:
        el = soup.select_one(sel)
        if not el:
            continue
        text = el.get("content") or el.get("data-price") or el.get_text() or ""
        text = text.strip()
        if not text:
            continue
        amt, cur = _amount_near_currency(text)
        if amt is None:
            amt = _to_float(text)
            cur = _currency_from_text(text)
        if amt is not None:
            return amt, cur
    return None, None


def _from_text(soup: BeautifulSoup) -> tuple[float | None, str | None]:
    # видимый текст страницы
    text = soup.get_text(" ", strip=True)
    amt, cur = _amount_near_currency(text)
    if amt is not None:
        return amt, cur
    return None, None


def extract(html: str, url: str | None = None) -> tuple[float | None, str | None, str | None]:
    """Возвращает (price, currency, title)."""
    soup = BeautifulSoup(html, "lxml")
    title = _title(soup)

    price, cur = _from_json_ld(soup)
    if price is None:
        price, cur = _from_meta(soup)
    if price is None:
        price, cur = _from_selectors(soup)
    if price is None:
        price, cur = _from_text(soup)

    if cur is None and price is not None:
        cur = "UAH"  # дефолт для наших широт
    return price, cur, title


if __name__ == "__main__":
    import sys
    import requests

    u = sys.argv[1] if len(sys.argv) > 1 else None
    if not u:
        print("usage: python parser.py <url>")
        sys.exit(1)
    r = requests.get(u, headers={"User-Agent": "Mozilla/5.0 PriceCheck/1.0"}, timeout=20)
    p, c, t = extract(r.text, u)
    print(f"URL:    {u}")
    print(f"Title:  {t}")
    print(f"Price:  {p} {c}")
