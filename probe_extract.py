"""Smoke test: pull a few listings from a city-scoped SRP and run the real
detail extractor on each. Prints the fields that recently changed
(lat/lng, agent_id, date_posted, location_city) so we can verify them
without writing to the DB.

Usage: python3 probe_extract.py
       python3 probe_extract.py --category /ban-can-ho-chung-cu-da-nang --n 5
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from urllib.parse import urljoin

sys.path.insert(0, str((Path(__file__).resolve().parent / "src").as_posix()))

from dotenv import load_dotenv
load_dotenv()

from scraper import (
    BASE_URL,
    USER_AGENT,
    _BROWSER_BACKEND,
    async_playwright,
    scrape_detail,
)

FIELDS_OF_INTEREST = (
    "listing_code",
    "title",
    "date_posted",
    "location_city",
    "location_province",
    "latitude",
    "longitude",
    "agent_id",
    "agent_url",
    "price_raw",
    "url_source",
)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--category", default="/ban-can-ho-chung-cu-da-nang",
                    help="Category path (with city suffix) to pull URLs from.")
    ap.add_argument("--n", type=int, default=3,
                    help="How many listings to extract.")
    ap.add_argument("--url", action="append",
                    help="Specific listing URL(s) to test. Repeatable. "
                         "Skips the SRP step.")
    args = ap.parse_args()

    headless = os.getenv("HEADLESS", "false").lower() == "true"
    print(f"backend={_BROWSER_BACKEND} headless={headless}")

    async with async_playwright() as p:
        # Reuse the scraper's persistent user-data dir so any cf_clearance
        # cookie from your last manual solve carries over — otherwise every
        # detail tab gets a fresh Cloudflare interstitial.
        user_data_dir = os.getenv("SCRAPER_USER_DATA_DIR", ".pw-data-scraper")
        print(f"user_data_dir={user_data_dir}")
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=DnsOverHttps",
            ],
            user_agent=USER_AGENT,
            viewport={"width": 1366, "height": 768},
            locale="vi-VN",
            timezone_id="Asia/Ho_Chi_Minh",
            ignore_https_errors=True,
        )

        try:
            urls = args.url or []
            if not urls:
                srp_url = urljoin(BASE_URL, args.category)
                print(f"\n[SRP] {srp_url}")
                page = await ctx.new_page()
                await page.goto(srp_url, wait_until="domcontentloaded",
                                timeout=60000, referer=BASE_URL)
                await page.wait_for_timeout(2000)
                anchors = await page.eval_on_selector_all(
                    "a[href*='-pr']",
                    "els => els.map(e => e.href)",
                )
                import re
                seen = []
                for a in anchors:
                    if re.search(r"-pr\d+", a) and "batdongsan.com.vn" in a and a not in seen:
                        seen.append(a)
                    if len(seen) >= args.n:
                        break
                urls = seen
                await page.close()
                print(f"[SRP] picked {len(urls)} URL(s)")

            if not urls:
                print("No URLs to test — Cloudflare may have blocked the SRP. "
                      "Try with HEADLESS=false to solve once, or pass --url.")
                return

            page = await ctx.new_page()
            for i, url in enumerate(urls, 1):
                print(f"\n--- [{i}/{len(urls)}] {url}")
                try:
                    res = await scrape_detail(page, url, args.category)
                except Exception as e:
                    print(f"  EXTRACT FAILED: {e}")
                    continue
                if not res:
                    print("  (no result — Cloudflare / timeout)")
                    continue
                trimmed = {k: res.get(k) for k in FIELDS_OF_INTEREST}
                print(json.dumps(trimmed, ensure_ascii=False, indent=2))

                missing = [k for k in ("latitude", "longitude", "agent_id",
                                       "date_posted") if not res.get(k)]
                if missing:
                    print(f"  ! missing: {missing}")

                if not res.get("latitude"):
                    # Try scrolling the maps section into view, then dump what
                    # we can see. The source site lazy-loads the maps iframe; if we
                    # extract before it materializes, lat/lng stays None.
                    print("  [debug] scrolling to bottom & sleeping 3s …")
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(3000)
                    debug = await page.evaluate("""() => {
                        const iframes = Array.from(document.querySelectorAll('iframe')).map(f => ({
                            src: f.src, dataSrc: f.getAttribute('data-src'),
                            cls: f.className, id: f.id
                        }));
                        const anchors = Array.from(document.querySelectorAll('a[href*="maps.google"]'))
                            .map(a => a.href);
                        return { iframes, anchors };
                    }""")
                    print(f"  [debug] iframes on page: {len(debug['iframes'])}")
                    for f in debug["iframes"][:8]:
                        print(f"    - src={f['src'][:120]} | dataSrc={(f['dataSrc'] or '')[:60]} | cls={f['cls'][:60]}")
                    print(f"  [debug] maps anchors: {debug['anchors']}")
                    print(f"  [debug] page.frames count: {len(page.frames)}")
                    for fr in page.frames:
                        print(f"    - frame.url={fr.url[:120]}")
        finally:
            await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
