"""Test Amazon.pl on Andrew server - Playwright various configs."""
import asyncio
import sys
sys.path.insert(0, '.')

async def test():
    from playwright.async_api import async_playwright
    
    configs = [
        # Config 1: old headless, no extra headers (current)
        {
            "name": "old headless, no extra headers",
            "headless": True,
            "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
            "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "extras": {},
        },
        # Config 2: --headless=new + extra CH-UA headers
        {
            "name": "--headless=new + Chrome 130",
            "headless": True,  # we'll use args instead
            "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled", "--headless=new"],
            "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            "extras": {
                "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
                "Sec-CH-UA": '"Chromium";v="130", "Google Chrome";v="130", "Not?A_Brand";v="99"',
                "Sec-CH-UA-Mobile": "?0",
                "Sec-CH-UA-Platform": '"Windows"',
            },  
        },
        # Config 3: --headless=new, no extra, modern Chrome
        {
            "name": "--headless=new, no extra",
            "headless": True,
            "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled", "--headless=new"],
            "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            "extras": {},
        },
    ]

    results = []
    for cfg in configs:
        print(f"\n=== Config: {cfg['name']} ===")
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=cfg["headless"], args=cfg["args"])
                ctx = await browser.new_context(
                    user_agent=cfg["ua"],
                    locale="pl-PL",
                    timezone_id="Europe/Warsaw",
                    viewport={"width": 1920, "height": 1080},
                    extra_http_headers=cfg["extras"] if cfg["extras"] else None,
                )
                await ctx.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                )
                page = await ctx.new_page()
                await page.goto(
                    "https://www.amazon.pl/dp/B07K47BG6K",
                    wait_until="domcontentloaded",
                    timeout=45000,
                )
                await page.wait_for_timeout(5000)
                html = await page.content()
                title = await page.title()
                print(f"  HTML len: {len(html)}")
                print(f"  Title: {title}")

                # Check what we got
                body_text = (await page.inner_text("body")).lower()
                if "api-services-support" in body_text:
                    results.append((cfg["name"], "BLOCKED (api-services)"))
                    print("  >>> BLOCKED by Amazon anti-bot")
                elif "zautomatyzowany" in body_text:
                    results.append((cfg["name"], "BLOCKED (polish)"))
                    print("  >>> BLOCKED (Polish anti-bot)")
                elif "121" in html[:50000]:
                    results.append((cfg["name"], "SUCCESS"))
                    print("  >>> PRICE FOUND in HTML!")
                    import parser
                    p, c, t = parser.extract(html, "https://www.amazon.pl/dp/B07K47BG6K")
                    print(f"  >>> Extracted: {p} {c} | {t[:60]}")
                elif "cookie" in html.lower() or "Zaakceptuj" in html:
                    print("  Cookie banner present, trying to click...")
                    try:
                        btns = await page.query_selector_all("button")
                        for btn in btns:
                            txt = (await btn.inner_text()).lower()
                            if "zaakceptuj" in txt or "accept" in txt or "zgadzam" in txt:
                                await btn.click()
                                print(f"  Clicked: {txt}")
                                await page.wait_for_timeout(5000)
                                break
                        html2 = await page.content()
                        if "121" in html2[:50000]:
                            results.append((cfg["name"], "SUCCESS after cookie click"))
                            print("  >>> PRICE after cookie!")
                            import parser
                            p, c, t = parser.extract(html2, "https://www.amazon.pl/dp/B07K47BG6K")
                            print(f"  >>> Extracted: {p} {c} | {t[:60]}")
                        else:
                            results.append((cfg["name"], f"COOKIE-CLICKED-NO-PRICE ({len(html2)})"))
                            print("  Still no price after cookie")
                    except Exception as e:
                        results.append((cfg["name"], f"COOKIE-ERROR: {e}"))
                        print(f"  Cookie error: {e}")
                else:
                    snippet = body_text[:200].replace("\n", " ")
                    results.append((cfg["name"], f"OTHER: {snippet[:80]}"))
                    print(f"  Other content: {snippet[:150]}")
                    
                await browser.close()
        except Exception as e:
            results.append((cfg["name"], f"EXCEPTION: {e}"))
            print(f"  EXCEPTION: {e}")

    print("\n\n========== RESULTS SUMMARY ==========")
    for name, result in results:
        print(f"  {name}: {result}")

asyncio.run(test())
