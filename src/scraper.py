"""
Property scraper.

- Uses Playwright (Chromium) to render JS-heavy pages and pass most Cloudflare
  passive checks. If Cloudflare shows an interactive challenge, run with
  HEADLESS=false in .env and solve once — the cookie is reused for the session.
- Idempotent: each listing has a stable listing_id parsed from the URL, and
  upsert_property uses INSERT ... ON DUPLICATE KEY UPDATE so re-runs only add
  new properties and refresh last_seen_at on existing ones.
"""
import asyncio
import csv
import hashlib
import json
import logging
import os
import random
import re
import sys
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

from dotenv import load_dotenv

# Prefer Patchright (a Playwright fork patched against Cloudflare / Datadome
# bot detection) so headless runs survive the interstitial. Fall back to
# vanilla Playwright if it isn't installed — which still works in headed mode.
try:
    from patchright.async_api import TimeoutError as PlaywrightTimeout
    from patchright.async_api import async_playwright
    _BROWSER_BACKEND = "patchright"
except ImportError:
    from playwright.async_api import TimeoutError as PlaywrightTimeout
    from playwright.async_api import async_playwright
    _BROWSER_BACKEND = "playwright"

# Image upload runs in src/ftp_upload.py — the scraper itself no longer
# uploads. agent_phone_cache.py was also removed; the phone-finder writes
# phones directly to the DB. Both names are kept as no-op stubs for any
# legacy reveal/fetcher code that still references them.
def _agent_cached_phone(_agent_id):
    return None


# Cache: agent profile URL → resolved numeric agent_id. Populated by
# resolve_agent_id_from_profile() so the same profile isn't visited twice
# in a single run. Cleared automatically when the Python process exits.
_AGENT_ID_CACHE: "dict[str, str | None]" = {}


def _agent_url_key(url: "str | None") -> "str | None":
    """Normalize an agent URL into a cache key: drop query + trailing slash."""
    if not url:
        return None
    return url.split("?", 1)[0].rstrip("/")


async def resolve_agent_id_from_profile(ctx, agent_url: str) -> "str | None":
    """Open the agent profile in a fresh tab and pull `agentId=NNN` out of
    the rendered HTML. Cached per run by URL. Returns None on Cloudflare /
    timeout / no match — caller decides what to do."""
    key = _agent_url_key(agent_url)
    if not key:
        return None
    if key in _AGENT_ID_CACHE:
        return _AGENT_ID_CACHE[key]
    log.info("Resolving agent_id via profile: %s", key)
    page = await ctx.new_page()
    try:
        try:
            await page.goto(agent_url, wait_until="domcontentloaded",
                            timeout=30000, referer=BASE_URL)
        except PlaywrightTimeout:
            log.warning("  agent profile timeout: %s", key)
            _AGENT_ID_CACHE[key] = None
            return None
        if await is_cloudflare_interstitial(page):
            log.warning("  Cloudflare on agent profile: %s", key)
            _AGENT_ID_CACHE[key] = None
            return None
        # agentId is injected by JS during hydration — DOMContentLoaded fires
        # too early. Wait for either a tracking-label that mentions agentId,
        # or a 2s timeout (then read whatever's there).
        try:
            await page.wait_for_selector('[tracking-label*="agentId="]',
                                         timeout=4000)
        except PlaywrightTimeout:
            pass
        html = await page.content()
    except Exception as e:
        log.warning("  agent profile fetch raised: %s", str(e)[:120])
        _AGENT_ID_CACHE[key] = None
        return None
    finally:
        try:
            await page.close()
        except Exception:
            pass

    # Profile pages embed agentId in tracking-label, JSON-LD blobs, and
    # script-tag config payloads. A single regex over the full HTML catches
    # whichever variant the current template uses.
    m = re.search(r"agentId[\"'=:\s]+(\d{3,})", html)
    aid = m.group(1) if m else None
    _AGENT_ID_CACHE[key] = aid
    if aid:
        log.info("  resolved agent_id=%s for %s", aid, key)
    else:
        log.warning("  no agentId found in profile HTML: %s", key)
    return aid


RESOLVE_MISSING_AGENT_ID = os.getenv("RESOLVE_MISSING_AGENT_ID", "true").lower() == "true"


from store import make_store  # backend dispatch lives in src/store.py

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("scraper")
log.info("Browser backend: %s", _BROWSER_BACKEND)

BASE_URL = "https://batdongsan.com.vn"

BASE_CATEGORIES = [
    "/ban-can-ho-chung-cu",
    "/ban-nha-rieng",
    "/ban-nha-mat-pho",
    "/ban-nha-biet-thu-lien-ke",
    "/ban-dat",
    "/ban-dat-nen-du-an",
    "/cho-thue-can-ho-chung-cu",
    "/cho-thue-nha-rieng",
    "/cho-thue-nha-mat-pho",
    "/cho-thue-van-phong",
]

# Suffix appended to each base category to filter listings to one city.
# These slugs were discovered by typing the city name into the source site's
# homepage search bar (#SuggestionSearch) and capturing the URL its
# submit button produces — the pattern is "<city>-<provincecode>" (e.g.
# vung-tau-vt = Vũng Tàu city in Bà Rịa-Vũng Tàu province). Three of the
# top-10 cities are province-equivalents (HCM, Hanoi, Da Nang) and use
# the bare province slug; Hue uses its parent province slug because the
# city is the province (centrally-governed since 2025).
#
# The earlier "<province>/tp-<city>" sub-path pattern returned almost no
# results in practice (≤10 rows/city in production) — these slugs return
# 9–30 listings on page 1 of /ban-can-ho-chung-cu and filter to the city
# proper, not the whole province.
#
# If any URL 404s on the first run, fix the suffix here. The scraper logs
# each target URL ("Listing page N: <url>"), so bad slugs are obvious.
CITIES = [
    ("Ho Chi Minh City", "-tp-hcm"),
    ("Hanoi",            "-ha-noi"),
    ("Da Nang",          "-da-nang"),
    ("Nha Trang",        "-nha-trang-kh"),
    ("Hoi An",           "-hoi-an-qna"),
    ("Phu Quoc",         "-phu-quoc-kg"),
    ("Ha Long Bay",      "-ha-long-qni"),
    ("Da Lat",           "-da-lat-ldd"),
    ("Hue",              "-thua-thien-hue"),
    ("Vung Tau",         "-vung-tau-vt"),
]

# Final category list = base × city. 10 × 10 = 100 distinct scrape targets,
# each one scoped to a single property type in a single city.
CATEGORIES = [base + suffix for base in BASE_CATEGORIES for _, suffix in CITIES]


def _base_category(category: str) -> str:
    """Strip the city suffix back to one of BASE_CATEGORIES, so lookups
    keyed on the bare property-type path still work."""
    for base in BASE_CATEGORIES:
        if category.startswith(base):
            return base
    return category


# URL category → human label used for the `property_type` client column.
PROPERTY_TYPE_MAP = {
    "/ban-can-ho-chung-cu":       "Căn hộ chung cư",
    "/ban-nha-rieng":             "Nhà riêng",
    "/ban-nha-mat-pho":           "Nhà mặt phố",
    "/ban-nha-biet-thu-lien-ke":  "Biệt thự, nhà liền kề",
    "/ban-dat":                   "Đất",
    "/ban-dat-nen-du-an":         "Đất nền dự án",
    "/cho-thue-can-ho-chung-cu":  "Căn hộ chung cư",
    "/cho-thue-nha-rieng":        "Nhà riêng",
    "/cho-thue-nha-mat-pho":      "Nhà mặt phố",
    "/cho-thue-van-phong":        "Văn phòng",
}

HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"
# 0 means "no cap" — keep paginating until a page yields no new URLs.
MAX_PAGES_PER_CATEGORY = int(os.getenv("MAX_PAGES_PER_CATEGORY", "0"))
# Manual-assist: if Cloudflare's auto-solve fails, bring the browser window
# forward, ring the terminal bell, and wait for the human to click the
# checkbox themselves. Only useful when HEADLESS=false. Set to false for
# unattended overnight runs (cron) so it doesn't hang waiting for clicks.
MANUAL_ASSIST = os.getenv(
    "MANUAL_ASSIST", "true" if not HEADLESS else "false"
).lower() == "true"
MANUAL_ASSIST_TIMEOUT_SEC = int(os.getenv("MANUAL_ASSIST_TIMEOUT_SEC", "120"))
# Manual-assist needs a visible window to click. In headless mode it would
# just hang. Auto-disable so we don't waste 2 minutes per challenge.
if HEADLESS and MANUAL_ASSIST:
    logging.getLogger("scraper").warning(
        "MANUAL_ASSIST=true but HEADLESS=true — no window to click. "
        "Forcing MANUAL_ASSIST=false."
    )
    MANUAL_ASSIST = False
DELAY_MIN = int(os.getenv("REQUEST_DELAY_MIN_MS", "800"))
DELAY_MAX = int(os.getenv("REQUEST_DELAY_MAX_MS", "2200"))
# Persistent user-data dir so the Cloudflare clearance cookie survives reruns —
# solve the challenge once with HEADLESS=false and subsequent runs reuse it.
USER_DATA_DIR = os.getenv("USER_DATA_DIR", ".pw-data")
# How many detail tabs to open in parallel per listing page. The worker
# scrapes all BATCH_SIZE tabs, closes them, then moves to the next listing
# page. 10 is a safe default; bump to 15/20 only if Cloudflare stays quiet.
BATCH_SIZE = max(1, int(os.getenv("BATCH_SIZE", "10")))
# Restart the chromium process after this many listing pages. Chromium
# accumulates memory across BrowserContexts (cache, GPU state, shared
# workers) and on small VPS (4–8 GB) eventually OOMs. Restarting drops
# all of it. 0 = never restart (default 30).
MAX_PAGES_PER_BROWSER = max(0, int(os.getenv("MAX_PAGES_PER_BROWSER", "30")))
# Drop listings older than this many days at save time. 0 disables the filter.
# Run is intended as a daily cron, so 7 keeps the working set to the last week.
MAX_LISTING_AGE_DAYS = max(0, int(os.getenv("MAX_LISTING_AGE_DAYS", "7")))

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

# Per-proxy fingerprint variation. `_launch_ctx` picks one entry from each pool
# deterministically from the proxy ID, so each proxy keeps a stable UA +
# viewport (cf_clearance is minted against the {IP, UA, screen} tuple — once
# the cookie is set, that combination has to stay constant or CF re-challenges).
_UA_POOL = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
)
_VIEWPORT_POOL = (
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1600, "height": 900},
    {"width": 1920, "height": 1080},
)

PRICE_UNITS = {"tỷ": 1_000_000_000, "triệu": 1_000_000, "nghìn": 1_000}


def parse_price(text: str | None) -> tuple[float | None, str | None]:
    if not text:
        return None, None
    raw = text.strip()
    cleaned = raw.lower().replace(",", ".")
    m = re.search(r"([\d.]+)\s*(tỷ|triệu|nghìn)", cleaned)
    if not m:
        return None, raw
    return float(m.group(1)) * PRICE_UNITS[m.group(2)], raw


def parse_area(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"([\d.,]+)\s*m", text.replace(",", "."))
    return float(m.group(1)) if m else None


def parse_int(text: str | None) -> int | None:
    if not text:
        return None
    m = re.search(r"\d+", text)
    return int(m.group()) if m else None


def parse_posted_date(text: str | None) -> "datetime | None":
    """Parse the source site's 'Ngày đăng' field. Common formats observed:
       '14/05/2026' or '14-05-2026', 'Hôm nay', 'Hôm qua', 'N ngày trước'.
       Returns midnight local time, or None if unparseable."""
    if not text:
        return None
    s = text.strip().lower()
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    if "hôm nay" in s or "hom nay" in s:
        return today
    if "hôm qua" in s or "hom qua" in s:
        return today - timedelta(days=1)
    m = re.search(r"(\d+)\s*ngày", s)          # "N ngày trước"
    if m:
        return today - timedelta(days=int(m.group(1)))
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(text.strip(), fmt)
        except ValueError:
            pass
    return None


def is_listing_too_old(posted_text: str | None) -> bool:
    """True iff posted_text parses to a date older than MAX_LISTING_AGE_DAYS.
    Unparseable / missing dates return False — we don't want to drop a
    listing just because the format is unfamiliar."""
    if MAX_LISTING_AGE_DAYS <= 0:
        return False
    d = parse_posted_date(posted_text)
    if d is None:
        return False
    cutoff = datetime.now() - timedelta(days=MAX_LISTING_AGE_DAYS)
    return d < cutoff


def extract_listing_id(url: str) -> str | None:
    m = re.search(r"-pr(\d+)", url)
    return m.group(1) if m else None


def extract_slug(url: str) -> str | None:
    """Slug = last URL segment before '-pr<id>'."""
    m = re.search(r"/([^/]+?)-pr\d+", url)
    return m.group(1) if m else None


def listing_type_from_category(category: str) -> str:
    if category.startswith("/ban-"):
        return "Bán"
    if category.startswith("/cho-thue-"):
        return "Cho thuê"
    return ""


def split_address(address: str | None) -> dict:
    """Vietnamese addresses are typically 'Street, Ward, District, Province' (last-to-first)."""
    if not address:
        return {"province": None, "district": None, "ward": None, "street": None}
    parts = [p.strip() for p in address.split(",") if p.strip()]
    return {
        "province": parts[-1] if len(parts) >= 1 else None,
        "district": parts[-2] if len(parts) >= 2 else None,
        "ward":     parts[-3] if len(parts) >= 3 else None,
        "street":   ", ".join(parts[:-3]) if len(parts) > 3 else (parts[0] if len(parts) <= 3 and parts else None),
    }


async def human_pause(page):
    await page.wait_for_timeout(random.randint(DELAY_MIN, DELAY_MAX))


async def human_mouse_move(page):
    """Move the cursor through 2-3 random points so Cloudflare's behavioral
    model sees pointer trajectory entropy. Cheap (~150ms total) and effective —
    zero mouse events on a page that "user clicked through" is a real signal.
    """
    try:
        for _ in range(random.randint(2, 3)):
            x = random.randint(50, 1200)
            y = random.randint(80, 600)
            await page.mouse.move(x, y, steps=random.randint(4, 8))
            await page.wait_for_timeout(random.randint(40, 120))
    except Exception:
        pass


async def human_scroll(page):
    """Real scroll + mouse moves so Cloudflare sees pointer and scroll signals."""
    try:
        await human_mouse_move(page)
        y = random.randint(400, 900)
        await page.evaluate(f"() => window.scrollBy({{top: {y}, behavior: 'auto'}})")
        await page.wait_for_timeout(random.randint(150, 400))
    except Exception:
        pass


_INTERSTITIAL_TITLES = ("chờ một chút", "just a moment")


async def _is_interstitial_now(page) -> bool:
    try:
        title = ((await page.title()) or "").lower().strip()
    except Exception:
        return False
    return any(s in title for s in _INTERSTITIAL_TITLES) or title == "batdongsan.com.vn"


# Shared across workers: only one worker is allowed to prompt the user for
# manual Cloudflare-checkbox click at a time, otherwise 4 windows would all
# pop forward and beep at once. Initialized lazily in main().
_manual_assist_lock: "asyncio.Lock | None" = None


async def is_cloudflare_interstitial(page, wait_seconds: int = 8) -> bool:
    """Detect Cloudflare's 'Just a moment…' challenge page.

    Flow:
      1. If the page isn't on the interstitial → return False fast.
      2. HEADLESS=true mode → return True immediately. Cloudflare's
         Turnstile doesn't auto-solve in headless chromium, so polling
         is pointless and wastes ~8s per failed batch. Fast-fail instead
         so the worker moves on to a fresh context.
      3. HEADLESS=false mode → poll for `wait_seconds` to let patchright
         auto-solve the JS challenge (which it can do in a real browser).
      4. If still on the interstitial AND MANUAL_ASSIST is on, ring the
         terminal bell, bring the browser to the front, and wait up to
         MANUAL_ASSIST_TIMEOUT_SEC for the human to click the checkbox.
      5. If the title changes during the manual window → return False.
      6. If timeout → return True (caller will discard this context).
    """
    if not await _is_interstitial_now(page):
        return False

    # Phase 1a — headless can't auto-solve Turnstile. Fail fast.
    if HEADLESS:
        return True

    # Phase 1b — auto-solve attempt (headed only).
    poll_ms = 500
    deadline_ms = wait_seconds * 1000
    elapsed = 0
    while elapsed < deadline_ms:
        await page.wait_for_timeout(poll_ms)
        elapsed += poll_ms
        if not await _is_interstitial_now(page):
            return False

    # Phase 2 — manual-assist (only when MANUAL_ASSIST is enabled).
    if not MANUAL_ASSIST:
        return True

    # Serialize across workers so only one window prompts at a time.
    lock = _manual_assist_lock
    if lock is not None:
        await lock.acquire()
    try:
        # Re-check after acquiring the lock — another worker may have just
        # resolved the same challenge (some Cloudflare cookies are domain-wide).
        if not await _is_interstitial_now(page):
            return False
        try:
            await page.bring_to_front()
        except Exception:
            pass
        try:
            sys.stdout.write("\a")
            sys.stdout.flush()
        except Exception:
            pass
        log.warning(
            ">>> MANUAL CLICK NEEDED <<<  click the Cloudflare checkbox in "
            "the browser window. Waiting up to %ds…",
            MANUAL_ASSIST_TIMEOUT_SEC,
        )

        manual_deadline = MANUAL_ASSIST_TIMEOUT_SEC * 1000
        elapsed = 0
        while elapsed < manual_deadline:
            await page.wait_for_timeout(poll_ms)
            elapsed += poll_ms
            if not await _is_interstitial_now(page):
                log.info("Manual challenge solved — resuming")
                return False
        log.error("Manual-assist timeout (%ds) — discarding this context and moving on",
                  MANUAL_ASSIST_TIMEOUT_SEC)
        return True
    finally:
        if lock is not None:
            lock.release()


async def text_of(page, selector: str) -> str | None:
    """Read innerText of the first matching element via page.evaluate.

    We deliberately avoid Playwright's Locator API here — its underlying CDP
    DOM probes (`DOM.describeNode`) crash with "CBOR: stack limit exceeded"
    on the source site's heavy detail-page DOMs, returning None for every field.
    page.evaluate returns a plain string, so it sidesteps CBOR entirely.
    Only standard CSS is supported (no Playwright `:has-text()` etc.).
    """
    try:
        return await page.evaluate(
            "sel => { const el = document.querySelector(sel); "
            "return el ? (el.innerText || '').trim() || null : null; }",
            selector,
        )
    except Exception:
        return None


async def extract_label_value_map(page) -> dict:
    """Return every short-info / specs label→value pair in ONE JS round-trip.

    Lets `scrape_detail` look up fields by Vietnamese label (e.g.
    "Mức giá", "Diện tích", "Pháp lý") without needing per-field
    `:has-text()` locator selectors that don't exist in plain CSS.
    """
    try:
        return await page.evaluate(
            """() => {
                const out = {};
                document.querySelectorAll('.re__pr-short-info-item').forEach(el => {
                    const t = (el.querySelector('.title')?.innerText || '').trim();
                    const v = (el.querySelector('.value')?.innerText || '').trim();
                    if (t) out[t] = v || null;
                });
                document.querySelectorAll('.re__pr-specs-content-item').forEach(el => {
                    const t = (el.querySelector('.re__pr-specs-content-item-title')?.innerText || '').trim();
                    const v = (el.querySelector('.re__pr-specs-content-item-value')?.innerText || '').trim();
                    if (t) out[t] = v || null;
                });
                return out;
            }"""
        )
    except Exception:
        return {}


def _spec(specs: dict, *labels: str) -> str | None:
    """Pick the first matching label (case-insensitive substring) from the dict."""
    for needle in labels:
        nl = needle.lower()
        for k, v in specs.items():
            if nl in k.lower():
                return v
    return None


async def fetch_listing_page_urls(page, category: str, page_num: int) -> list[str] | None:
    """Fetch one listing page. Returns the URLs found, [] if empty,
    or None on Cloudflare/timeout (signals 'stop this category')."""
    target = (
        urljoin(BASE_URL, f"{category}/p{page_num}")
        if page_num > 1
        else urljoin(BASE_URL, category)
    )
    log.info("Listing page %d: %s", page_num, target)
    try:
        await page.goto(target, wait_until="domcontentloaded", timeout=45000,
                        referer=BASE_URL)
        if await is_cloudflare_interstitial(page):
            log.warning("Cloudflare interstitial on %s — backing off 15s before next category", target)
            await page.wait_for_timeout(15000)
            return None
        # Wait for at least one listing anchor to render. Wrap in try/except
        # because chromium occasionally throws "CBOR: stack limit exceeded"
        # on very deep DOMs — in that case we still try eval_on_selector_all,
        # which uses a different CDP path and usually succeeds.
        try:
            await page.locator("a[href*='-pr']").first.wait_for(timeout=15000)
        except Exception as e:
            log.warning("locator.wait_for failed on %s (%s) — falling back to immediate eval", target, str(e)[:120])
    except PlaywrightTimeout:
        log.warning("Timeout on %s", target)
        return []
    await human_pause(page)
    anchors = await page.eval_on_selector_all(
        "a[href*='-pr']",
        "els => els.map(e => e.href)",
    )
    return sorted({a for a in anchors if re.search(r"-pr\d+", a) and "batdongsan.com.vn" in a})


async def reveal_phone(page) -> None:
    """Best-effort: click the 'Hiện số' control so the real phone renders.

    On most listings the mobile number stays masked (e.g. '0933 626 ***')
    even after clicking — the full number is only shown to logged-in users.
    We still try; callers always fall back to whatever text is rendered.
    """
    candidates = [
        ".re__btn-phone-icon",
        ".re__link-phone",
        ".js__btn-tracking-show-phone",
        ".re__btn-see-phone",
        "button:has-text('Hiện số')",
        "a:has-text('Hiện số')",
    ]
    for sel in candidates:
        try:
            btn = page.locator(sel).first
            if await btn.count() == 0:
                continue
            await btn.click(timeout=2500)
            await page.wait_for_timeout(1200)
            return
        except Exception:
            continue


def clean_phone(text: str | None) -> str | None:
    """Strip the ' · Hiện số' decoration that follows the masked mobile."""
    if not text:
        return text
    return re.split(r"\s*[·•]\s*", text)[0].strip() or None


# Per-listing phone reveal clicks (SRP card + detail CTA + body span).
# Default OFF — phones come from the per-agent cache populated by
# `fetch_agent_phones.py`. Set to true ONLY for opportunistic reveals
# while testing or when an agent's profile is unreachable.
INLINE_PHONE_REVEAL = os.getenv("INLINE_PHONE_REVEAL", "false").lower() == "true"

BODY_PHONE_MAX_PER_RUN = int(os.getenv("BODY_PHONE_MAX_PER_RUN", "30"))
BODY_PHONE_DELAY_MIN_SEC = float(os.getenv("BODY_PHONE_DELAY_MIN_SEC", "2"))
BODY_PHONE_DELAY_MAX_SEC = float(os.getenv("BODY_PHONE_DELAY_MAX_SEC", "6"))
# How long to "read" the page before issuing the reveal click. Real users
# don't click reveal within a couple of seconds of landing on a page — the
# anti-bot model flags that. 6-15 s simulates a human glancing at the
# listing first.
BODY_PHONE_READ_MIN_SEC = float(os.getenv("BODY_PHONE_READ_MIN_SEC", "6"))
BODY_PHONE_READ_MAX_SEC = float(os.getenv("BODY_PHONE_READ_MAX_SEC", "15"))


async def _human_page_settle(page) -> None:
    """Pretend to read the page: small scroll, hover idle, pause.

    Fires before the very first click on a page. The anti-bot model
    expects a real user to take a few seconds to look at the listing
    before tapping the reveal button — clicking immediately after a
    `goto()` is a strong bot signal.
    """
    try:
        # Random small scroll — fires scroll/wheel events.
        await page.evaluate(
            "y => window.scrollBy({top: y, behavior: 'auto'})",
            random.randint(150, 500),
        )
    except Exception:
        pass
    await asyncio.sleep(random.uniform(BODY_PHONE_READ_MIN_SEC,
                                       BODY_PHONE_READ_MAX_SEC))
    try:
        await page.evaluate(
            "() => window.scrollTo({top: 0, behavior: 'auto'})"
        )
    except Exception:
        pass

# Process-wide counter + lock: serialize reveals across every tab/context
# so we never burn the per-account quota in a parallel burst. Initialised
# lazily on first call (asyncio primitives must be created inside a loop).
# Shared by BOTH reveal sources: the SRP card button (.js__card-phone-btn)
# and the detail body span (.re__detail-content .js__btn-tracking).
_body_phone_count = 0
_body_phone_lock: "asyncio.Lock | None" = None
_body_phone_quota_warned = False


async def _human_click_at(page, box: dict, *, double: bool = True) -> bool:
    """Move mouse from an off-target start coord to a random point inside
    the supplied bounding box over ~25 steps, hover briefly, then click.

    With `double=True` (default for phone reveals), a second click
    follows within 120-300ms — matching how a human typically reveals
    AND copies the phone in one fluid motion on the source site. Real
    `mousemove` events fire on each interpolation step so the gesture
    looks human.
    """
    if not box or box.get("w", 0) < 5:
        return False
    target_x = box["x"] + box["w"] * random.uniform(0.25, 0.75)
    target_y = box["y"] + box["h"] * random.uniform(0.25, 0.75)
    start_x = max(20.0, target_x - random.uniform(140, 360))
    start_y = max(20.0, target_y - random.uniform(60, 220))
    try:
        await page.mouse.move(start_x, start_y)
        await asyncio.sleep(random.uniform(0.08, 0.22))
        await page.mouse.move(target_x, target_y, steps=random.randint(18, 35))
        await asyncio.sleep(random.uniform(0.20, 0.55))
        # First click
        await page.mouse.down()
        await asyncio.sleep(random.uniform(0.05, 0.13))
        await page.mouse.up()
        if double:
            # Second click within the system double-click window so any
            # dblclick handler also fires.
            await asyncio.sleep(random.uniform(0.12, 0.30))
            await page.mouse.down()
            await asyncio.sleep(random.uniform(0.05, 0.13))
            await page.mouse.up()
        return True
    except Exception:
        return False


async def _quota_consume_and_warn() -> bool:
    """Return True if a reveal slot is available (and consume it).
    False if quota exhausted. Caller must hold `_body_phone_lock`."""
    global _body_phone_count, _body_phone_quota_warned
    if BODY_PHONE_MAX_PER_RUN and _body_phone_count >= BODY_PHONE_MAX_PER_RUN:
        if not _body_phone_quota_warned:
            log.warning(
                "Phone-reveal quota reached for this run (%d). "
                "Remaining cards/listings will skip phone extraction.",
                BODY_PHONE_MAX_PER_RUN,
            )
            _body_phone_quota_warned = True
        return False
    _body_phone_count += 1
    return True


async def _reveal_via_detail_main_button(page) -> str | None:
    """Click the detail-page main phone CTA to unmask. Visible button →
    clicks land cleanly. After reveal the element gains a `mobile=` attr.

    Target:
        <div class="re__btn re__btn-cyan-solid--md re__btn-phone-icon
                    phone js__phone phoneEvent js__phone-event"
             kyc-tracking-id="lead-phone-ldp"
             raw="..."
             mobile="..." (after click)>
            0934 346 *** · Current number
        </div>
    """
    global _body_phone_lock
    if _body_phone_lock is None:
        _body_phone_lock = asyncio.Lock()

    sel = '.js__phone-event, .js__phone, .re__btn-phone-icon'
    async with _body_phone_lock:
        if not await _quota_consume_and_warn():
            return None

        # Read-the-page idle before any mouse activity — humans don't
        # reveal phone within seconds of landing on a detail page.
        await _human_page_settle(page)

        try:
            box = await page.evaluate(f"""() => {{
                const el = document.querySelector('{sel}');
                if (!el) return null;
                el.scrollIntoView({{ block: 'center', behavior: 'instant' }});
                const r = el.getBoundingClientRect();
                return {{ x: r.x, y: r.y, w: r.width, h: r.height }};
            }}""")
        except Exception:
            return None
        if not box:
            return None

        await asyncio.sleep(random.uniform(BODY_PHONE_DELAY_MIN_SEC,
                                           BODY_PHONE_DELAY_MAX_SEC))
        if not await _human_click_at(page, box):
            return None
        await page.wait_for_timeout(1500)

        try:
            phone = await page.evaluate(f"""() => {{
                const el = document.querySelector('{sel}');
                if (!el) return null;
                const m = el.getAttribute('mobile');
                if (m && !m.includes('*')) return m.trim();
                const t = (el.innerText || '').trim();
                if (t && !t.includes('*')) return t.split(/[·•]/)[0].trim();
                return null;
            }}""")
        except Exception:
            return None
        if not phone:
            return None
        digits = re.sub(r"\D", "", phone)
        return phone if len(digits) >= 9 else None


async def extract_detail_phone(page) -> str | None:
    """Reveal and return the phone number from a detail page.

    Tries the visible main-CTA button first, falls back to the hidden
    in-description span via `extract_body_phone`. The CTA path is far
    more reliable; the body-span fallback is kept for listings that
    don't render the main button.
    """
    phone = await _reveal_via_detail_main_button(page)
    if phone:
        return phone
    return await extract_body_phone(page)


async def extract_card_phones(listing_page, prids: list[str]) -> dict[str, str]:
    """On a search-results page, click `.js__card-phone-btn[prid=...]`
    for each requested listing and return {prid: revealed_phone}.

    This is the SRP-card equivalent of extract_body_phone. The card
    button is visible (not gated by `hidden-mobile hidden-phone` CSS),
    so the click reliably reaches the handler. Reveals are still
    serialized + jittered + capped by the same BODY_PHONE_MAX_PER_RUN
    quota.
    """
    global _body_phone_lock
    if _body_phone_lock is None:
        _body_phone_lock = asyncio.Lock()

    results: dict[str, str] = {}
    if not prids:
        return results

    # Filter to prids that actually have a card button on this page.
    try:
        present = await listing_page.evaluate(
            """(prids) => {
                const want = new Set(prids);
                return Array.from(document.querySelectorAll(
                    '.js__card-phone-btn[prid]'
                ))
                .map(e => e.getAttribute('prid'))
                .filter(p => want.has(p));
            }""",
            prids,
        )
    except Exception:
        return results

    if not present:
        return results

    log.info("Card-phone: %d candidate cards on SRP", len(present))

    # One-time page-settle before the first reveal on this SRP.
    await _human_page_settle(listing_page)

    for prid in present:
        async with _body_phone_lock:
            if not await _quota_consume_and_warn():
                break

            # Scroll to the card and measure its button.
            try:
                box = await listing_page.evaluate(
                    """(p) => {
                        const el = document.querySelector(
                            '.js__card-phone-btn[prid="' + p + '"]'
                        );
                        if (!el) return null;
                        el.scrollIntoView({ block: 'center', behavior: 'instant' });
                        const r = el.getBoundingClientRect();
                        return { x: r.x, y: r.y, w: r.width, h: r.height };
                    }""",
                    prid,
                )
            except Exception:
                continue
            if not box:
                continue

            # Pre-click human-paced jitter.
            await asyncio.sleep(random.uniform(BODY_PHONE_DELAY_MIN_SEC,
                                               BODY_PHONE_DELAY_MAX_SEC))
            if not await _human_click_at(listing_page, box):
                continue

            # Server-mediated reveal takes a beat.
            await listing_page.wait_for_timeout(1500)

            try:
                phone = await listing_page.evaluate(
                    """(p) => {
                        const el = document.querySelector(
                            '.js__card-phone-btn[prid="' + p + '"]'
                        );
                        if (!el) return null;
                        const m = el.getAttribute('mobile');
                        if (m && !m.includes('*')) return m.trim();
                        const t = (el.innerText || '').trim();
                        // Strip the ' · Current number' suffix shown after reveal.
                        if (t && !t.includes('*')) {
                            return t.split(/[·•]/)[0].trim();
                        }
                        return null;
                    }""",
                    prid,
                )
            except Exception:
                continue
            if not phone:
                continue
            digits = re.sub(r"\D", "", phone)
            if len(digits) >= 9:
                results[prid] = phone
                log.info("Card-phone: prid=%s → %s", prid, phone)

    return results


async def extract_body_phone(page) -> str | None:
    """Reveal and return the phone number embedded in the description body.

    Approach: simulate a real user click on the masked-phone span.
      - Get the span's bounding box via page.evaluate (avoids the CBOR
        Locator crash that plagues the source site's heavy DOMs).
      - Move the mouse from an off-target start coord to a random point
        inside the span over ~25 steps so genuine `mousemove` events fire.
      - Brief hover pause (a real user reads the masked text first).
      - Separate `mouse.down → wait → mouse.up` instead of a one-shot click.

    Anti-quota controls:
      - Process-wide lock + counter so parallel tabs never burst reveals.
      - Hard cap BODY_PHONE_MAX_PER_RUN. Once reached, return None without
        clicking — the per-account quota stays intact for the next run.
      - Random jitter BODY_PHONE_DELAY_MIN_SEC..MAX_SEC before the click.

    Note: the per-account quota is enforced server-side by the source site. Even
    a perfectly-human click sequence still gets blocked once the quota is
    exhausted. The throttle exists to keep us under the ceiling.
    """
    global _body_phone_count, _body_phone_lock, _body_phone_quota_warned

    if _body_phone_lock is None:
        _body_phone_lock = asyncio.Lock()

    if BODY_PHONE_MAX_PER_RUN and _body_phone_count >= BODY_PHONE_MAX_PER_RUN:
        if not _body_phone_quota_warned:
            log.warning(
                "Body-phone reveal quota reached for this run (%d). "
                "Remaining listings will skip phone_number extraction.",
                BODY_PHONE_MAX_PER_RUN,
            )
            _body_phone_quota_warned = True
        return None

    async with _body_phone_lock:
        # Re-check inside the lock — another task may have just used the
        # last slot while we waited.
        if BODY_PHONE_MAX_PER_RUN and _body_phone_count >= BODY_PHONE_MAX_PER_RUN:
            return None

        # Un-hide the FIRST span so its bounding box resolves to real pixels,
        # then return its rect.
        try:
            box = await page.evaluate("""() => {
                const el = document.querySelector(
                    '.re__detail-content .js__btn-tracking[data-phone-index]'
                );
                if (!el) return null;
                el.style.setProperty('display', 'inline-block', 'important');
                el.style.setProperty('visibility', 'visible', 'important');
                el.style.setProperty('opacity', '1', 'important');
                el.scrollIntoView({ block: 'center', behavior: 'instant' });
                const r = el.getBoundingClientRect();
                return { x: r.x, y: r.y, w: r.width, h: r.height };
            }""")
        except Exception:
            return None
        if not box or box.get("w", 0) < 5:
            return None  # span not present → no quota consumed

        # Pre-click jitter — humans don't reveal at machine speed.
        await asyncio.sleep(random.uniform(BODY_PHONE_DELAY_MIN_SEC,
                                           BODY_PHONE_DELAY_MAX_SEC))

        # Random landing point inside the inner 50% of the span (avoid edges).
        target_x = box["x"] + box["w"] * random.uniform(0.25, 0.75)
        target_y = box["y"] + box["h"] * random.uniform(0.25, 0.75)
        # Start "elsewhere" so the trajectory is non-trivial — CDP will emit
        # mousemove events for every interpolated step.
        start_x = max(20.0, target_x - random.uniform(140, 360))
        start_y = max(20.0, target_y - random.uniform(60, 220))

        try:
            await page.mouse.move(start_x, start_y)
            await asyncio.sleep(random.uniform(0.08, 0.22))
            await page.mouse.move(target_x, target_y, steps=random.randint(18, 35))
            # Hover pause: read-before-click behavior.
            await asyncio.sleep(random.uniform(0.20, 0.55))
            await page.mouse.down()
            await asyncio.sleep(random.uniform(0.05, 0.13))
            await page.mouse.up()
        except Exception:
            return None

        _body_phone_count += 1  # quota consumed regardless of reveal outcome

        # Server-mediated reveal — give it a beat to land.
        await page.wait_for_timeout(1500)

        try:
            revealed = await page.eval_on_selector_all(
                '.re__detail-content .js__btn-tracking',
                """els => els.map(e => {
                    const m = e.getAttribute('mobile');
                    if (m && !m.includes('*')) return m.trim();
                    const t = (e.innerText || '').trim();
                    if (t && !t.includes('*')) return t;
                    return null;
                }).filter(Boolean)"""
            )
        except Exception:
            return None
        for p in revealed:
            digits = re.sub(r"\D", "", p)
            if len(digits) >= 9:
                return p.strip()
        return None


async def _wait_for_detail_ready(page) -> bool:
    """Wait up to ~4s for the detail page's title selector to render.
    Returns True if title is present, False on timeout.
    Uses page.evaluate (not Locator) to avoid the CBOR DOM-probe crash."""
    for _ in range(8):
        try:
            if await page.evaluate(
                "() => !!document.querySelector('h1, .re__pr-title')"
            ):
                return True
        except Exception:
            pass
        await page.wait_for_timeout(500)
    return False


async def _extract_detail_fields(
    page,
    url: str,
    category: str,
    pre_revealed_phone: str | None = None,
    srp_agent: dict | None = None,
) -> dict:
    """Pure extraction — assumes the detail page is already loaded.

    Shared by both `scrape_detail` (which does its own page.goto) and
    `scrape_detail_via_click` (which clicks a card on the listing page).
    """
    # Read-like behavior so we don't look like a goto-and-scrape bot.
    await human_scroll(page)

    # One JS round-trip pulls every label→value pair from both spec tables.
    specs = await extract_label_value_map(page)

    title = await text_of(page, "h1")
    price_text       = _spec(specs, "Mức giá", "Giá")
    area_text        = _spec(specs, "Diện tích")
    bedrooms_text    = _spec(specs, "Số phòng ngủ", "Phòng ngủ")
    bathrooms_text   = _spec(specs, "Số phòng tắm", "Số toilet", "Toilet")
    direction        = _spec(specs, "Hướng nhà")
    legal            = _spec(specs, "Pháp lý")
    furniture        = _spec(specs, "Nội thất")
    posted           = _spec(specs, "Ngày đăng")
    expires          = _spec(specs, "Ngày hết hạn")
    news_type        = _spec(specs, "Loại tin") or await text_of(
        page, ".re__pr-vip-label, .re__pr-priority"
    )

    # line-1 is the actual address; line-2 is just the post-reorg admin
    # rename annotation, which would otherwise corrupt the comma split.
    address = (
        await text_of(page, ".re__address-line-1")
        or await text_of(page, ".re__pr-short-description")
        or await text_of(page, ".js__pr-address")
    )
    description = (
        await text_of(page, ".re__detail-content")
        or await text_of(page, ".re__section-body")
    )
    contact_name = (
        await text_of(page, ".re__contact-name")
        or await text_of(page, ".js__agent-contact-name")
    )
    footer_disclaimer = (
        await text_of(page, ".re__disclaimer")
        or await text_of(page, ".re__pr-disclaimer")
        or await text_of(page, ".re__detail-disclaimer")
    )

    # Agent metadata — read from the existing `js__agent-contact-name`
    # link in the detail page. The href points to the agent's profile
    # on guru.batdongsan.com.vn, and `tracking-label` carries the
    # numeric agent_id (e.g. `agentId=1288513`). NO click — pure HTML.
    #
    # Fallback: when the fallback selector matches a /pa/ link without a
    # tracking-label, we lose the ID. Recover it from the URL slug instead
    # (formats seen: `…-<id>`, `/pa/<id>`, `?agentId=<id>`).
    agent_info = await page.evaluate("""() => {
        const idFromUrl = (href) => {
            if (!href) return null;
            let m = href.match(/\\/pa\\/[^?#]*?-(\\d+)(?:[/?#]|$)/);
            if (m) return m[1];
            m = href.match(/\\/pa\\/(\\d+)(?:[/?#]|$)/);
            if (m) return m[1];
            m = href.match(/[?&]agentId=(\\d+)/);
            if (m) return m[1];
            return null;
        };
        const candidates = [
            ...document.querySelectorAll('a.js__agent-contact-name'),
            ...document.querySelectorAll('[tracking-label*="agentId="]'),
            ...document.querySelectorAll('a[href*="guru.batdongsan.com.vn/pa/"]'),
        ];
        for (const el of candidates) {
            const tl = el.getAttribute('tracking-label') || '';
            let m = tl.match(/agentId=(\\d+)/);
            const href = el.href || el.getAttribute('data-bds-pa-link') || null;
            const id = (m && m[1]) || idFromUrl(href);
            if (id || href) return { id: id, url: href };
        }
        return { id: null, url: null };
    }""")
    # SRP-card data is more reliable than the detail-page selector — many
    # detail layouts hide the agent block until JS hydration. Prefer it.
    detail_id  = (agent_info or {}).get("id")  or None
    detail_url = (agent_info or {}).get("url") or None
    srp_id   = (srp_agent or {}).get("agent_id")
    srp_url  = (srp_agent or {}).get("agent_url")
    srp_name = (srp_agent or {}).get("agent_name")
    agent_id  = srp_id  or detail_id
    agent_url = srp_url or detail_url

    # Final fallbacks for agent_id — the phone-finder uses it as the WHERE
    # key when writing phones back, so a null here means the phone never
    # gets stitched onto this row.
    #   1) Scan the full detail-page HTML for any agentId reference. The
    #      number sometimes only shows up in inline JSON / tracking config.
    #   2) Open the agent profile in a new tab (cached by URL). Disable
    #      with RESOLVE_MISSING_AGENT_ID=false in .env.
    if not agent_id and agent_url:
        try:
            html = await page.content()
            m = re.search(r"agentId[\"'=:\s]+(\d{3,})", html)
            if m:
                agent_id = m.group(1)
                log.info("  recovered agent_id=%s from detail HTML", agent_id)
        except Exception:
            pass
    if not agent_id and agent_url and RESOLVE_MISSING_AGENT_ID:
        try:
            agent_id = await resolve_agent_id_from_profile(page.context, agent_url)
        except Exception as e:
            log.warning("  profile resolver raised: %s", str(e)[:120])

    # Phone resolution order:
    #   1. SRP-prerevealed phone (passed in from _crawl_listing_page).
    #   2. agent_phones cache hit — every listing by this agent shares
    #      the same number, so one cached entry covers all their rows.
    #   3. Inline reveal click (only if INLINE_PHONE_REVEAL=true) —
    #      disabled by default because it burns the per-account quota
    #      without dedup. Main path now is the per-agent fetcher.
    cached_agent_phone = _agent_cached_phone(agent_id) if agent_id else None
    body_phone = pre_revealed_phone or cached_agent_phone
    if not body_phone and INLINE_PHONE_REVEAL:
        body_phone = await extract_detail_phone(page)

    # Read the button text AFTER the reveal attempt. If reveal succeeded
    # the text is now the full number; otherwise it's still masked
    # ("0933 626 ***"). Stored as `hidden_number` for parity with older rows.
    contact_phone = clean_phone(
        await text_of(page, ".re__link-phone")
        or await text_of(page, ".re__btn-phone-icon")
        or await text_of(page, ".re__text-phone")
        or await text_of(page, ".js__contact-phone")
        or await text_of(page, "a[href^='tel:']")
    )

    thumbnail_paths = await page.eval_on_selector_all(
        ".re__media-thumb-item img",
        "els => els.map(e => e.src || e.getAttribute('data-src')).filter(Boolean)",
    )
    large_image_paths = await page.eval_on_selector_all(
        ".re__pr-gallery img, .re__media-main img, .swiper-slide img",
        "els => els.map(e => e.src || e.getAttribute('data-src')).filter(Boolean)",
    )

    # Lat/lng comes from the Google Maps embed on the detail page. The map
    # iframe is cross-origin (google.com), so we can't reach its inner DOM
    # via document.querySelector — but its own src= URL carries the same
    # lat/lng as the "Open in Maps" anchor inside. We try, in order:
    #   1) anchors in the parent doc / open shadow roots (template variants
    #      that render the link outside the iframe)
    #   2) the maps iframe src — covers ll= and the legacy q= / center=
    #   3) Playwright page.frames — for cross-origin frames whose `src`
    #      attribute on the host page is empty (rare; happens when the
    #      embed is loaded by JS after the initial HTML).
    lat = lng = None
    coord_re = re.compile(r"[?&](?:ll|q|center)=([\-\d.]+),([\-\d.]+)")

    try:
        href = await page.evaluate("""
            () => {
                const walk = (root) => {
                    if (root.querySelector) {
                        const a = root.querySelector('a[href*="maps.google"]');
                        if (a && a.href) return a.href;
                    }
                    const all = root.querySelectorAll ? root.querySelectorAll('*') : [];
                    for (const el of all) {
                        if (el.shadowRoot) {
                            const v = walk(el.shadowRoot);
                            if (v) return v;
                        }
                    }
                    return null;
                };
                return walk(document);
            }
        """)
        if href:
            m = coord_re.search(href)
            if m:
                lat, lng = float(m.group(1)), float(m.group(2))
    except Exception:
        pass

    if lat is None:
        try:
            # The maps iframe on the source site is lazy-loaded — until the user
            # scrolls it into view, `src=""` is empty and the real URL lives
            # in `data-src`. Read both so we don't depend on scroll order.
            iframe_srcs = await page.eval_on_selector_all(
                "iframe",
                "els => els.map(e => e.src || e.getAttribute('data-src') || '').filter(s => s.includes('maps'))",
            )
            for src in iframe_srcs:
                m = coord_re.search(src)
                if m:
                    lat, lng = float(m.group(1)), float(m.group(2))
                    break
        except Exception:
            pass

    if lat is None:
        try:
            for frame in page.frames:
                furl = frame.url or ""
                if "maps.google" not in furl:
                    continue
                m = coord_re.search(furl)
                if m:
                    lat, lng = float(m.group(1)), float(m.group(2))
                    break
                try:
                    href = await frame.locator(
                        'a[href*="maps.google"]'
                    ).first.get_attribute("href", timeout=2000)
                    if href:
                        m = coord_re.search(href)
                        if m:
                            lat, lng = float(m.group(1)), float(m.group(2))
                            break
                except Exception:
                    continue
        except Exception:
            pass

    price_vnd, price_raw = parse_price(price_text)
    addr_parts = split_address(address)

    return {
        # --- client-facing columns ---
        "url_source":         url,
        "listing_code":       extract_listing_id(url),
        "title":              title,
        "slug":                extract_slug(url),
        "description":        description,
        "footer_disclaimer":  footer_disclaimer,
        "price":              price_vnd,
        "acreage":            parse_area(area_text),
        "bedrooms":           parse_int(bedrooms_text),
        "bathrooms":          parse_int(bathrooms_text),
        "property_type":      PROPERTY_TYPE_MAP.get(_base_category(category)) or category.lstrip("/"),
        "legal_type":         legal,
        "news_type":          news_type,
        "interior":           furniture,
        "date_posted":        posted,
        "posted_by":          contact_name or srp_name,
        "hidden_number":      contact_phone,
        "phone_number":       body_phone,
        "agent_id":           agent_id,
        "agent_url":          agent_url,
        "location_city":      addr_parts["province"],   # top admin level (Hà Nội / TP HCM / …)
        "location_province":  addr_parts["district"],   # district within the city
        "listing_type":       listing_type_from_category(category),
        "thumbnail_paths":    thumbnail_paths,
        "large_image_paths":  large_image_paths,
        # --- auxiliary (kept for completeness / debugging) ---
        "price_raw":          price_raw,
        "direction":          direction,
        "address":            address,
        "ward":               addr_parts["ward"],
        "street":             addr_parts["street"],
        "latitude":           lat,
        "longitude":          lng,
        "expires_at_raw":     expires,
        "category":           category,
    }


async def scrape_detail(page, url: str, category: str) -> dict | None:
    """Direct-goto variant — opens the URL and extracts. Used as fallback;
    the main loop now prefers `scrape_detail_via_click` for human-like
    Referer + click signals."""
    await human_pause(page)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000,
                        referer=urljoin(BASE_URL, category))
        if await is_cloudflare_interstitial(page):
            log.warning("Cloudflare interstitial on %s — context will be discarded", url)
            return None
        await _wait_for_detail_ready(page)
    except PlaywrightTimeout:
        log.warning("Timeout on %s", url)
        return None
    return await _extract_detail_fields(page, url, category)


async def scrape_detail_via_click(listing_page, url: str, category: str) -> dict | None:
    """Try clicking the card; fall back to page.goto if the click fails.

    Click navigation sets a real Referer + same-origin Sec-Fetch-Site, which
    Cloudflare's behavioral model prefers. But cookie banners, overlays, or
    detached DOM nodes can make the click time out. In that case we fall
    back to direct goto — the cf_clearance cookie set on the listing-page
    load is still valid in the same BrowserContext, so this doesn't trigger
    a new Cloudflare challenge.

    Returns the extracted data dict, or None if both paths failed.
    """
    lid = extract_listing_id(url)
    if not lid:
        return None

    # Try the click first.
    clicked = False
    locator = None
    for sel in [f'a[href="{url}"]', f'a[href*="-pr{lid}"]']:
        loc = listing_page.locator(sel).first
        try:
            if await loc.count() > 0:
                locator = loc
                break
        except Exception:
            continue

    if locator is not None:
        try:
            await locator.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        # Some pages drop a cookie/consent banner that intercepts pointer
        # events — dismiss before clicking.
        await _dismiss_overlays(listing_page)
        await human_pause(listing_page)
        # First attempt: normal click with a short timeout.
        try:
            async with listing_page.expect_navigation(wait_until="domcontentloaded", timeout=30000):
                await locator.click(timeout=3000)
            clicked = True
        except Exception:
            # Second attempt: force=True bypasses actionability checks
            # (overlay obscuring, animations, etc.). Cheap retry.
            try:
                async with listing_page.expect_navigation(wait_until="domcontentloaded", timeout=30000):
                    await locator.click(timeout=3000, force=True)
                clicked = True
            except Exception as e:
                log.warning("  click failed (%s) — falling back to direct goto",
                            str(e).splitlines()[0][:120])

    if not clicked:
        # Direct goto fallback — still inside the same context, cf_clearance
        # is intact, so this typically loads with no re-challenge.
        try:
            await listing_page.goto(url, wait_until="domcontentloaded", timeout=30000,
                                    referer=listing_page.url)
        except Exception as e:
            log.warning("  goto fallback failed for %s: %s", url, str(e).splitlines()[0][:120])
            return None

    if f"-pr{lid}" not in listing_page.url:
        log.warning("  navigated to %s (expected pr%s) — skipping", listing_page.url, lid)
        return None

    if await is_cloudflare_interstitial(listing_page):
        log.warning("Cloudflare interstitial on detail %s", url)
        return None

    await _wait_for_detail_ready(listing_page)
    return await _extract_detail_fields(listing_page, url, category)


async def _scrape_one_tab(
    ctx,
    url: str,
    category: str,
    tag: str,
    pre_revealed_phone: str | None = None,
    srp_agent: dict | None = None,
) -> dict | None:
    """Open a fresh tab in the same BrowserContext (shares cf_clearance),
    scrape the detail page, close the tab. Returns the data dict, or
    None on Cloudflare interstitial / load failure.

    `pre_revealed_phone` is forwarded to `_extract_detail_fields` so we
    don't burn an extra reveal slot on a listing whose phone was already
    pulled from the SRP card button.

    `srp_agent` is the {agent_id, agent_url, agent_name} dict captured
    from the SRP card (more reliable than the detail-page selector) —
    used as the primary source for agent fields when present.
    """
    page = await ctx.new_page()
    try:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000,
                            referer=urljoin(BASE_URL, category))
        except PlaywrightTimeout:
            log.warning("%s   timeout: %s", tag, url)
            return None
        if await is_cloudflare_interstitial(page):
            log.warning("%s   cloudflare on detail: %s", tag, url)
            return None
        if not await _wait_for_detail_ready(page):
            log.warning("%s   detail not ready: %s", tag, url)
            return None
        return await _extract_detail_fields(
            page, url, category,
            pre_revealed_phone=pre_revealed_phone,
            srp_agent=srp_agent,
        )
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def _disable_target_attribute(page):
    """Strip target='_blank' from every <a> so clicks navigate same-tab."""
    try:
        await page.evaluate(
            "() => document.querySelectorAll('a[target]').forEach(a => a.removeAttribute('target'))"
        )
    except Exception:
        pass


async def _dismiss_overlays(page):
    """Best-effort: dismiss cookie banners / popups that block clicks.

    Walks a small list of common Vietnamese consent + popup selectors and
    clicks any that exist. Failures are silent — none of these is required.
    """
    candidates = [
        # Cookie / consent banners
        "button:has-text('Đồng ý')",
        "button:has-text('Chấp nhận')",
        "button:has-text('OK')",
        ".cookie-consent button",
        "#cookie-banner button",
        "[id*='cookie'] button",
        # Generic close buttons
        ".re__modal-close",
        ".modal-close",
        "button.close",
        "[aria-label='Close']",
        "[aria-label='close']",
    ]
    for sel in candidates:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                await loc.click(timeout=1500)
        except Exception:
            continue


class _Progress:
    """Shared state across worker tasks: which category-page each worker
    should pick next, and which categories have been exhausted.

    Persists to a JSON file (default data/progress.json) so a restart
    resumes near where it left off. On restart, each category does a
    quick re-walk of pages 1..FRESH_RECHECK_PAGES first to catch any
    new listings posted while the scraper was offline, then jumps to
    the saved page and continues forward.

    Failed pages are NOT saved — they get retried next run via the
    fresh-recheck or because next_page was never advanced past them.
    """
    def __init__(self, categories: list[str], max_pages: int,
                 progress_path: "Path | None" = None,
                 fresh_recheck: int = 3):
        self.next_page = {c: 1 for c in categories}
        self.fresh_queue: dict[str, deque] = {c: deque() for c in categories}
        self.exhausted: set[str] = set()
        self.max_pages = max_pages
        self.fresh_recheck = max(0, fresh_recheck)
        self.progress_path = progress_path
        self.lock = asyncio.Lock()

        # Load saved state (if any). Don't carry over `exhausted` from disk —
        # the fresh-recheck might find new listings that re-open the category.
        if progress_path and progress_path.exists():
            try:
                saved = json.loads(progress_path.read_text(encoding="utf-8"))
                for c in categories:
                    s = saved.get("categories", {}).get(c, {})
                    saved_next = int(s.get("next_page", 1))
                    if saved_next > 1:
                        fresh = list(
                            range(1, min(self.fresh_recheck, saved_next - 1) + 1)
                        )
                        self.fresh_queue[c] = deque(fresh)
                        self.next_page[c] = saved_next
                        if fresh:
                            log.info(
                                "Resume %s: re-check pages %s, then continue from p%d",
                                c, fresh, saved_next,
                            )
                        else:
                            log.info("Resume %s: continue from p%d", c, saved_next)
            except Exception as e:
                log.warning("Failed to load progress (starting fresh): %s", e)

    async def next_task(self) -> "tuple[str, int] | None":
        async with self.lock:
            # Round-robin across non-exhausted categories so workers spread
            # load and one slow category doesn't starve the rest.
            for cat in list(self.next_page.keys()):
                if cat in self.exhausted:
                    continue

                # Drain the fresh-recheck queue first (only populated on
                # restart-from-saved-state).
                if self.fresh_queue[cat]:
                    pn = self.fresh_queue[cat].popleft()
                    self.next_page = {
                        **{k: v for k, v in self.next_page.items() if k != cat},
                        cat: self.next_page[cat],
                    }
                    return cat, pn

                pn = self.next_page[cat]
                if self.max_pages and pn > self.max_pages:
                    self.exhausted.add(cat)
                    continue
                self.next_page[cat] = pn + 1
                # Move this category to the back so others get a turn next call.
                self.next_page = {
                    **{k: v for k, v in self.next_page.items() if k != cat},
                    cat: self.next_page[cat],
                }
                return cat, pn
            return None

    def mark_exhausted(self, category: str):
        self.exhausted.add(category)
        self._save()

    def report_progress(self):
        """Persist state. Call after each successful listing page."""
        self._save()

    def _save(self):
        if not self.progress_path:
            return
        try:
            data = {
                "categories": {
                    c: {
                        "next_page": self.next_page[c],
                        "exhausted": c in self.exhausted,
                    }
                    for c in self.next_page
                },
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            self.progress_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.progress_path.with_suffix(self.progress_path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(self.progress_path)  # atomic on POSIX
        except Exception as e:
            log.warning("Failed to save progress: %s", e)


# Counter shared across workers, protected by progress.lock for log accuracy.
_total_saved = 0


async def _crawl_listing_page(
    category: str,
    page_num: int,
    ctx,
    db,
    ftp,
) -> str:
    """Process a single listing page (typically 20 detail clicks).
    Returns one of: 'ok', 'no_new', 'cloudflare', 'failed'.
    """
    global _total_saved
    tag = ""
    listing_page = await ctx.new_page()
    target = (
        urljoin(BASE_URL, f"{category}/p{page_num}")
        if page_num > 1 else urljoin(BASE_URL, category)
    )

    # Mimic a real navigation chain: home → category for page 1, category → /pN
    # for later pages. CF behavioral models look at Referer continuity.
    referer = (
        urljoin(BASE_URL, category) if page_num > 1 else BASE_URL
    )

    async def _go_to_listing() -> str:
        """Navigate to the listing page target. Returns 'ok' / 'cloudflare' / 'failed'."""
        try:
            await listing_page.goto(target, wait_until="domcontentloaded", timeout=45000,
                                    referer=referer)
        except PlaywrightTimeout:
            return "failed"
        if await is_cloudflare_interstitial(listing_page):
            return "cloudflare"
        try:
            await listing_page.locator("a[href*='-pr']").first.wait_for(timeout=6000)
        except Exception:
            pass
        await _disable_target_attribute(listing_page)
        return "ok"

    try:
        log.info("%s Listing page %d: %s", tag, page_num, target)
        first = await _go_to_listing()
        if first == "failed":
            log.warning("%s Timeout on %s", tag, target)
            return "failed"
        if first == "cloudflare":
            log.warning("%s Cloudflare interstitial on %s — discarding batch", tag, target)
            return "cloudflare"

        await human_scroll(listing_page)

        # Pull listing URLs AND per-card agent info from the SRP in one pass.
        # Every card with a professional agent has a div with
        # `tracking-label="loc=Sale-Listing,agentId=NNN,prid=MMM,..."` and
        # `data-bds-pa-link="https://guru.batdongsan.com.vn/pa/<slug>?…"`.
        # Reading this here is more reliable than the detail-page extractor
        # (which misses many layouts).
        srp_data = await listing_page.evaluate("""() => {
            const urls = Array.from(new Set(
                Array.from(document.querySelectorAll('a[href*="-pr"]'))
                    .map(a => a.href)
                    .filter(h => /-pr\\d+/.test(h) && h.includes('batdongsan.com.vn'))
            ));
            const agents = {};
            for (const el of document.querySelectorAll('[tracking-label*="agentId="]')) {
                const tl = el.getAttribute('tracking-label') || '';
                const pridM = tl.match(/prid=(\\d+)/);
                const idM = tl.match(/agentId=(\\d+)/);
                if (!pridM || !idM) continue;
                const pa = (el.getAttribute('data-bds-pa-link') || '').split('?')[0];
                const nameEl = el.querySelector('.agent-name');
                const name = nameEl ? (nameEl.textContent || '').trim() : null;
                agents[pridM[1]] = {
                    agent_id: idM[1],
                    agent_url: pa || null,
                    agent_name: name || null,
                };
            }
            return { urls, agents };
        }""")
        urls = srp_data.get("urls") or []
        srp_agents: dict = srp_data.get("agents") or {}

        # Skip listings already in DB up front. has_listing is a single-row
        # SELECT — fast even called 20 times.
        new_urls = [
            u for u in urls
            if extract_listing_id(u) and not db.has_listing(extract_listing_id(u))
        ]
        log.info("%s   page %d: %d urls (%d already in DB)",
                 tag, page_num, len(urls), len(urls) - len(new_urls))

        # Phones now come from the per-agent cache populated by
        # `fetch_agent_phones.py`. Per-listing reveal clicks are off by
        # default (INLINE_PHONE_REVEAL=false) — they burn the per-account
        # quota for marginal gain. Flip the env var on to re-enable.
        card_phones: dict[str, str] = {}
        if INLINE_PHONE_REVEAL:
            new_prids = [extract_listing_id(u) for u in new_urls if extract_listing_id(u)]
            try:
                card_phones = await extract_card_phones(listing_page, new_prids)
            except Exception as e:
                log.warning("%s   extract_card_phones raised: %s", tag, str(e)[:120])
                card_phones = {}

        if not urls:
            return "no_new"
        if not new_urls:
            # Everything on this page is already saved — keep walking pagination
            # in case page N+1 has new ones. Return 'ok' so the worker tries
            # the next page; we DON'T mark exhausted until a page yields zero
            # listings at all.
            return "ok"

        # New-tab batch mode: open BATCH_SIZE detail tabs in parallel,
        # scrape them concurrently, close all, then move on. All tabs
        # share the same BrowserContext as the listing page so cf_clearance
        # is reused — no per-tab Cloudflare challenge.
        for wave_start in range(0, len(new_urls), BATCH_SIZE):
            wave = new_urls[wave_start:wave_start + BATCH_SIZE]
            log.info("%s   wave: opening %d tabs (%d–%d of %d)",
                     tag, len(wave),
                     wave_start + 1, wave_start + len(wave), len(new_urls))

            async def _staggered(url: str, idx: int):
                # Stagger tab opens by ~150ms each so 10 nav requests don't
                # all hit Cloudflare's edge in the same TCP burst.
                await asyncio.sleep(idx * 0.15)
                prid = extract_listing_id(url)
                pre_phone = card_phones.get(prid) if prid else None
                srp_agent = srp_agents.get(prid) if prid else None
                return await _scrape_one_tab(
                    ctx, url, category, tag,
                    pre_revealed_phone=pre_phone,
                    srp_agent=srp_agent,
                )

            tasks = [asyncio.create_task(_staggered(u, i))
                     for i, u in enumerate(wave)]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            cf_or_fail = 0
            for url, res in zip(wave, results):
                if isinstance(res, Exception):
                    log.error("%s   exception on %s: %s",
                              tag, url, str(res).splitlines()[0][:160])
                    cf_or_fail += 1
                    continue
                if res is None:
                    cf_or_fail += 1
                    continue
                if not res.get("listing_code"):
                    continue

                if is_listing_too_old(res.get("date_posted")):
                    log.info("%s   skip %s — posted %s (older than %d days)",
                             tag, res["listing_code"], res.get("date_posted"),
                             MAX_LISTING_AGE_DAYS)
                    continue

                # FTP is a no-op when SKIP_IMAGE_UPLOAD=true (URL-only mode).
                try:
                    new_large, new_thumb = ftp.upload_listing(
                        res["listing_code"],
                        res.get("large_image_paths") or [],
                        res.get("thumbnail_paths") or [],
                    )
                    res["large_image_paths"] = new_large
                    res["thumbnail_paths"] = new_thumb
                except Exception as e:
                    log.error("%s   FTP upload failed for %s: %s — saving original URLs",
                              tag, res["listing_code"], e)

                db.upsert_property(res)
                _total_saved += 1
                if _total_saved % 10 == 0:
                    log.info("Progress: %d listings saved so far", _total_saved)

            # If half (or more) of the wave got blocked, cf_clearance is
            # likely burned — abandon this context, worker will rotate.
            if cf_or_fail >= max(3, len(wave) // 2):
                log.warning("%s   %d/%d tabs failed/cloudflare — ending batch",
                            tag, cf_or_fail, len(wave))
                return "cloudflare"

            await human_pause(listing_page)
        return "ok"
    finally:
        try:
            await listing_page.close()
        except Exception:
            pass


REQUIRE_LOGIN = os.getenv("REQUIRE_LOGIN", "true").lower() == "true"
LOGIN_MANUAL_TIMEOUT_SEC = int(os.getenv("LOGIN_MANUAL_TIMEOUT_SEC", "300"))
LOGIN_URL = os.getenv("LOGIN_URL", "https://batdongsan.com.vn/sellernet/trang-dang-nhap")


async def _is_logged_in(page) -> bool:
    """Definitive logged-in check: header user-avatar is rendered.

    The source site injects `.js__avatar` (with class `re__avatar--xl` and an
    inner `.re__avatar-text` initial) into the top-right header only when
    the user session cookie is valid. The header user-state is fetched
    via JS after DOMContentLoaded, so the avatar appears 0.5–2 s after
    `goto` returns. We poll for up to 4 s instead of one-shot checking.
    """
    sel = '.js__avatar, .re__avatar--xl, .re__avatar-text'
    for _ in range(8):
        try:
            if await page.evaluate(f"() => !!document.querySelector('{sel}')"):
                return True
        except Exception:
            pass
        await page.wait_for_timeout(500)
    return False


async def _human_type(page, locator, text: str) -> None:
    """Type text into a field char-by-char with random 50–180ms delays.

    Avoids Locator.fill() which sets the field value in one shot and never
    fires keydown/keypress/input events — anti-bot scripts (incl. the one
    behind the source site's login) flag that.
    """
    await locator.click(timeout=5000)
    try:
        await locator.fill("")
    except Exception:
        try:
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Delete")
        except Exception:
            pass
    for ch in text:
        await page.keyboard.type(ch)
        await asyncio.sleep(random.uniform(0.05, 0.18))


async def _wait_for_login(page, timeout_sec: int) -> bool:
    """Poll the home page for the logged-in indicator.

    Reloads BASE_URL every 8s so cookies set by manual login in another
    tab (or by an AJAX submit on this page) propagate into the DOM check.
    """
    deadline_ms = timeout_sec * 1000
    elapsed = 0
    poll_ms = 2000
    reload_every = 8000
    next_reload = 0
    while elapsed < deadline_ms:
        if elapsed >= next_reload:
            try:
                await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=15000)
            except Exception:
                pass
            next_reload = elapsed + reload_every
        if await _is_logged_in(page):
            return True
        await page.wait_for_timeout(poll_ms)
        elapsed += poll_ms
    return False


async def _attempt_form_fill_and_submit(page, username: str, password: str) -> bool:
    """Fill the visible login form and submit it. Returns True if both
    fields were filled and a submit action was dispatched.

    Verification of success is the caller's job (poll the avatar via
    `_wait_for_login`). This helper just drives the form.

    The /sellernet/trang-dang-nhap page is React-rendered, so the inputs
    appear after `domcontentloaded`. We wait up to 15 s for the username
    input to attach before doing anything else.
    """
    try:
        await page.wait_for_selector(
            'input[name="username"]', state="visible", timeout=15000,
        )
    except Exception:
        return False

    user_loc = page.locator('input[name="username"]').first
    pw_loc = page.locator(
        'input[data-testid="password-input"], input[name="password"]'
    ).first

    user_ok = pw_ok = False
    try:
        if await user_loc.count() > 0:
            await _human_type(page, user_loc, username)
            user_ok = True
    except Exception as e:
        log.warning("Auto-login: typing username failed: %s", e)
    try:
        if await pw_loc.count() > 0:
            await _human_type(page, pw_loc, password)
            pw_ok = True
    except Exception as e:
        log.warning("Auto-login: typing password failed: %s", e)
    if not (user_ok and pw_ok):
        return False

    # Submit: prefer Enter on the password field (most reliable on AJAX
    # forms), fall back to an explicit submit-button click.
    submitted = False
    try:
        await page.keyboard.press("Enter")
        submitted = True
    except Exception:
        pass
    if not submitted:
        for sel in [
            'button[type="submit"]',
            'button:has-text("Đăng nhập"):not(:has-text("Facebook")):not(:has-text("Google")):not(:has-text("Zalo"))',
            'button:has-text("Log in")',
            'button:has-text("Sign in")',
            '.re__btn-login',
            'input[type="submit"]',
        ]:
            try:
                loc = page.locator(sel).first
                if await loc.count() == 0:
                    continue
                await loc.click(timeout=5000)
                submitted = True
                break
            except Exception:
                continue
    await page.wait_for_timeout(2500)
    return submitted


async def _open_login_modal(page) -> bool:
    """Open the header auth modal and switch it into login mode.

    The trigger element is the header sign-up button — `id="kct_username"`
    `tracking-id="sign-up-button-seller"` — which opens a modal that
    contains both register and login forms. We click it, then switch to
    the login tab/link inside. Returns True iff `input[name="username"]`
    is present in the DOM afterward.

    The home page React-renders the header after `domcontentloaded`, so
    we first wait up to 15 s for one of the trigger candidates to attach.
    """
    try:
        await page.wait_for_selector(
            '#kct_username, a[tracking-id="sign-up-button-seller"], '
            'a:has-text("Đăng ký"), button:has-text("Đăng ký")',
            timeout=15000,
        )
    except Exception:
        return False

    triggers = [
        '#kct_username',
        'a[tracking-id="sign-up-button-seller"]',
        'a:has-text("Đăng ký")',
        'button:has-text("Đăng ký")',
    ]
    opened = False
    for sel in triggers:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                await loc.click(timeout=3000)
                await page.wait_for_timeout(1500)  # modal animation
                opened = True
                break
        except Exception:
            continue
    if not opened:
        return False

    # Most source site modals open on the register tab. Switch to login.
    for sel in [
        '[class*="modal"] a:has-text("Đăng nhập")',
        '[class*="modal"] button:has-text("Đăng nhập")',
        '[class*="popup"] a:has-text("Đăng nhập")',
        '[class*="popup"] button:has-text("Đăng nhập")',
        'a:has-text("Đăng nhập")',
        'button:has-text("Đăng nhập")',
    ]:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                await loc.click(timeout=2500)
                await page.wait_for_timeout(900)
                break
        except Exception:
            continue

    try:
        return bool(await page.evaluate(
            "() => !!document.querySelector('input[name=\"username\"]')"
        ))
    except Exception:
        return False


async def auto_login(ctx) -> bool:
    """Ensure a logged-in session before crawling. Returns True iff verified.

    Flow:
      1. Load home. If avatar is present → already logged in, done.
      2. Navigate to LOGIN_URL. If redirected away → already logged in;
         verify and return.
      3. If on the login URL, fill the form and submit.
      4. If still not verified: try the header sign-up modal as a
         secondary path (clicks `#kct_username` → switches to login tab
         → fills the same form).
      5. If still not verified AND HEADLESS=false: bring the window
         forward, beep, wait up to LOGIN_MANUAL_TIMEOUT_SEC for a human.
      6. HEADLESS mode + failure → return False; main() aborts the run.
    """
    username = os.getenv("SOURCE_USERNAME", "").strip()
    password = os.getenv("SOURCE_PASSWORD", "")

    page = await ctx.new_page()
    try:
        try:
            await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
        except PlaywrightTimeout:
            log.warning("Auto-login: home page timeout")
        if await is_cloudflare_interstitial(page):
            log.warning("Auto-login: Cloudflare on home page — cannot proceed "
                        "until the challenge is solved")
            if HEADLESS:
                return False

        if await _is_logged_in(page):
            log.info("Auto-login: already logged in (persistent cookies still valid)")
            return True

        if not username or not password:
            log.warning("Auto-login: no SOURCE_USERNAME / SOURCE_PASSWORD in .env")
            if HEADLESS:
                return False
            log.warning(">>> PLEASE LOG IN MANUALLY IN THE BROWSER WINDOW <<<")
            try: sys.stdout.write("\a"); sys.stdout.flush()
            except Exception: pass
            try: await page.bring_to_front()
            except Exception: pass
            return await _wait_for_login(page, LOGIN_MANUAL_TIMEOUT_SEC)

        log.info("Auto-login: signing in as %s at %s", username, LOGIN_URL)

        # Navigate to the seller-net login page directly. This is a dedicated
        # full-page form (not an in-page modal), so a goto is the correct path.
        try:
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
        except PlaywrightTimeout:
            log.warning("Auto-login: %s timeout", LOGIN_URL)

        # If we land somewhere OTHER than /sellernet/trang-dang-nhap, the source site
        # bounced us away — the session in USER_DATA_DIR is already valid and
        # the avatar JS just took longer than _is_logged_in's poll window. In
        # that case skip the form-fill (there's no form on the dashboard) and
        # verify via _wait_for_login.
        landed = (page.url or "").lower()
        on_login_page = "trang-dang-nhap" in landed
        if not on_login_page:
            log.info("Auto-login: %s redirected to %s — session already valid",
                     LOGIN_URL, page.url)
            if await _wait_for_login(page, 15):
                log.info("Auto-login: success (existing session reused)")
                return True
            log.warning("Auto-login: redirected away from login but no "
                        "logged-in indicator yet — falling through")

        if on_login_page and await is_cloudflare_interstitial(page):
            log.warning("Auto-login: Cloudflare on %s", LOGIN_URL)

        if on_login_page:
            if not await _attempt_form_fill_and_submit(page, username, password):
                log.warning("Auto-login: form fields not found on %s", LOGIN_URL)

        # Verify post-submit (or post-redirect): poll for 15s.
        if await _wait_for_login(page, 15):
            log.info("Auto-login: success — session saved to %s", USER_DATA_DIR)
            return True

        # Modal fallback: when the direct URL approach didn't get us in,
        # try the header sign-up button which opens an in-page auth modal
        # containing both register and login forms.
        log.info("Auto-login: trying header modal fallback")
        try:
            await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
        except PlaywrightTimeout:
            log.warning("Auto-login: home page timeout (modal fallback)")
        if await _is_logged_in(page):
            log.info("Auto-login: success (verified before modal needed)")
            return True
        if await _open_login_modal(page):
            if await _attempt_form_fill_and_submit(page, username, password):
                if await _wait_for_login(page, 15):
                    log.info("Auto-login: success via header modal — "
                             "session saved to %s", USER_DATA_DIR)
                    return True
                log.warning("Auto-login: modal submit dispatched but "
                            "verification failed")
            else:
                log.warning("Auto-login: modal opened but form fields not visible")
        else:
            log.warning("Auto-login: header sign-up trigger not found")

        if HEADLESS:
            log.warning("Auto-login: automated attempt failed and HEADLESS=true "
                        "(no manual fallback)")
            return False

        log.warning(">>> AUTO-LOGIN FAILED — PLEASE COMPLETE LOGIN MANUALLY IN "
                    "THE BROWSER WINDOW <<<  waiting up to %ds",
                    LOGIN_MANUAL_TIMEOUT_SEC)
        try: sys.stdout.write("\a"); sys.stdout.flush()
        except Exception: pass
        try: await page.bring_to_front()
        except Exception: pass
        return await _wait_for_login(page, LOGIN_MANUAL_TIMEOUT_SEC)
    finally:
        try:
            await page.close()
        except Exception:
            pass


# ─── Proxy pool ──────────────────────────────────────────────────────────
#
# The phone-finder reads `data/proxies.csv` directly; the scraper used to read
# a single proxy from `.env` (PROXY_SERVER). That meant one bad row killed the
# whole run. The pool below picks a random proxy from the CSV per Chromium
# context, cools any proxy that produces a net-level failure for
# PROXY_COOLDOWN_SEC, and falls back to the .env single proxy (or direct
# connection) when the CSV is missing.

PROXY_ENABLED       = os.getenv("PROXY_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
PROXIES_CSV         = Path(os.getenv("PROXIES_CSV", "data/proxies.csv"))
PROXY_COOLDOWN_SEC  = int(os.getenv("PROXY_COOLDOWN_SEC", "600"))  # 10 min

# Chromium net errors that almost certainly mean "this proxy is bad" — used
# to decide whether to cool the active proxy on a page crash. We deliberately
# exclude ERR_TIMED_OUT here because source site sometimes hangs.
_PROXY_FAIL_PATTERNS = (
    "ERR_PROXY_CONNECTION_FAILED",
    "ERR_PROXY_AUTH",
    "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_HTTP_RESPONSE_CODE_FAILURE",
    "ERR_CONNECTION_RESET",
    "ERR_CONNECTION_REFUSED",
    "ERR_CONNECTION_CLOSED",
    "ERR_EMPTY_RESPONSE",
    "ERR_SSL_PROTOCOL_ERROR",
    "ERR_SOCKS_CONNECTION_FAILED",
)


def _load_proxy_pool() -> list[dict]:
    """Read `data/proxies.csv` (server,username,password). Falls back to the
    single .env PROXY_SERVER entry, then to []. Returns [] when PROXY_ENABLED
    is false, regardless of what's in proxies.csv / PROXY_SERVER."""
    if not PROXY_ENABLED:
        return []
    if PROXIES_CSV.exists():
        out = []
        with PROXIES_CSV.open(encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                s = (row.get("server") or "").strip()
                if not s:
                    continue
                out.append({
                    "server":   s,
                    "username": (row.get("username") or "").strip() or None,
                    "password": (row.get("password") or "").strip() or None,
                    "cool_until": 0.0,
                })
        if out:
            return out
    if os.getenv("PROXY_SERVER"):
        return [{
            "server":   os.getenv("PROXY_SERVER"),
            "username": os.getenv("PROXY_USER") or None,
            "password": os.getenv("PROXY_PASSWORD") or None,
            "cool_until": 0.0,
        }]
    return []


def _looks_like_proxy_failure(err_msg: str) -> bool:
    return any(p in err_msg for p in _PROXY_FAIL_PATTERNS)


async def run_scraper():
    """Data-only scrape: walks the 10 categories, saves listings to DB
    with agent_id + agent_url. NO login, NO phone reveal, NO image upload.

    Image upload runs in src/ftp_upload.py as a separate background loop.
    Phone reveal runs in src/agent_number_finder.py with multi-account rotation.
    """
    proxy_pool = _load_proxy_pool()
    if proxy_pool:
        log.info("Proxy pool: %d proxies", len(proxy_pool))
    elif not PROXY_ENABLED:
        log.info("PROXY_ENABLED=false — direct connection")
    else:
        log.info("No proxies configured — direct connection")

    chromium_args = [
        "--disable-blink-features=AutomationControlled",
        "--disable-features=DnsOverHttps",
        # Bright Data residential MITMs HTTPS with its own CA. Trust only
        # that specific cert (SPKI-pinned) rather than blanket-ignoring all
        # cert errors — keeps Chromium's "Secure Context" intact so target
        # site JS (e.g. Cloudflare's challenge) executes normally.
        # SPKI from data/brightdata_proxy_ca/New SSL certifcate.../*.crt
        "--ignore-certificate-errors-spki-list=6cvKh5o6I2VADnzNXsXZfrze45k+hTbnaZD+cbzqwRQ=",
    ]
    if not HEADLESS:
        chromium_args.append("--start-minimized")

    ctx_options = dict(
        locale="vi-VN",
        timezone_id="Asia/Ho_Chi_Minh",
        # Bright Data residential MITMs HTTPS with its own CA cert by default,
        # which Chromium rejects as ERR_CERT_AUTHORITY_INVALID. Trust it.
        # (Alternative: enable "Native SSL" in the Bright Data zone dashboard.)
        ignore_https_errors=True,
        # user_agent, viewport, proxy are filled in per-launch by _launch_ctx()
        # below so each proxy gets a stable but distinct fingerprint and its
        # own cf_clearance cookie store.
    )

    db = make_store()
    db.init_schema()

    category_order = list(CATEGORIES)
    random.shuffle(category_order)
    log.info("Category order this run: %s", ", ".join(category_order))

    progress = _Progress(
        categories=category_order,
        max_pages=MAX_PAGES_PER_CATEGORY,
        progress_path=Path(os.getenv("PROGRESS_PATH", "data/progress.json")),
        fresh_recheck=int(os.getenv("FRESH_RECHECK_PAGES", "3")),
    )

    # Empty stub uploader so _crawl_listing_page can still call .upload_listing().
    # Real FTP upload runs separately in src/ftp_upload.py.
    class _NoopUploader:
        def upload_listing(self, lid, large, thumb): return large, thumb
        def close(self): pass
    ftp = _NoopUploader()
    pages_done = 0

    ctx_holder = {"ctx": None, "active_proxy": None}

    def _pick_proxy() -> dict | None:
        if not proxy_pool:
            return None
        now = time.time()
        live = [pr for pr in proxy_pool if pr["cool_until"] <= now]
        if not live:
            # Every proxy is cooling — pick the one closest to ready anyway,
            # so we don't stall forever waiting on a fixed timer.
            live = sorted(proxy_pool, key=lambda pr: pr["cool_until"])[:1]
            log.warning("All proxies cooling — using %s anyway",
                        live[0]["server"])
        return random.choice(live)

    base_data_dir = os.getenv("SCRAPER_USER_DATA_DIR", ".pw-data-scraper")

    async with async_playwright() as p:
        async def _launch_ctx():
            chosen = _pick_proxy()
            kw = dict(ctx_options)
            if chosen:
                # cf_clearance is bound to the IP that solved the challenge —
                # if we reuse one user-data dir across proxies, every rotation
                # ships a cookie minted for a different IP and CF re-challenges.
                # Give each proxy its own dir + a stable UA/viewport keyed off
                # the same proxy_id so the fingerprint never drifts within
                # a single profile.
                #
                # Hash on server+username, not just server: Bright Data and
                # similar gateway-style providers point every row at the same
                # endpoint (brd.superproxy.io:22225) and differentiate sessions
                # via the username (-session-<id>). Without including the
                # username, all gateway rows would collapse to one profile dir.
                fp_key = f"{chosen['server']}|{chosen.get('username') or ''}"
                proxy_id = hashlib.md5(fp_key.encode()).hexdigest()[:8]
                user_data_dir = f"{base_data_dir}-{proxy_id}"
                seed = int(proxy_id, 16)
                kw["user_agent"] = _UA_POOL[seed % len(_UA_POOL)]
                kw["viewport"]   = _VIEWPORT_POOL[seed % len(_VIEWPORT_POOL)]
                kw["proxy"] = {k: chosen[k]
                               for k in ("server", "username", "password")
                               if chosen.get(k)}
                log.info("Browser context: proxy=%s dir=%s vp=%dx%d",
                         chosen["server"], user_data_dir,
                         kw["viewport"]["width"], kw["viewport"]["height"])
            else:
                user_data_dir   = base_data_dir
                kw["user_agent"] = USER_AGENT
                kw["viewport"]   = {"width": 1366, "height": 768}
                kw["proxy"]      = None
            ctx_holder["active_proxy"] = chosen
            return await p.chromium.launch_persistent_context(
                user_data_dir,
                headless=HEADLESS,
                args=chromium_args,
                **kw,
            )

        ctx_holder["ctx"] = await _launch_ctx()

        async def _rotate_ctx(reason: str):
            log.info("Rotating Chromium context (%s)", reason)
            try:
                await ctx_holder["ctx"].close()
            except Exception as e:
                log.warning("ctx.close() raised: %s", e)
            ctx_holder["ctx"] = await _launch_ctx()

        try:
            while True:
                task = await progress.next_task()
                if task is None:
                    break

                if MAX_PAGES_PER_BROWSER and pages_done and \
                        pages_done % MAX_PAGES_PER_BROWSER == 0:
                    await _rotate_ctx(f"every {MAX_PAGES_PER_BROWSER} pages")

                category, page_num = task
                try:
                    result = await _crawl_listing_page(
                        category, page_num, ctx_holder["ctx"], db, ftp,
                    )
                except Exception as e:
                    log.exception("crash on %s page %d: %s",
                                  category, page_num, e)
                    if _looks_like_proxy_failure(str(e)):
                        cur = ctx_holder.get("active_proxy")
                        if cur:
                            cur["cool_until"] = time.time() + PROXY_COOLDOWN_SEC
                            log.warning("Cooling proxy %s for %ds",
                                        cur["server"], PROXY_COOLDOWN_SEC)
                        await _rotate_ctx("proxy failure")
                    result = "failed"
                pages_done += 1
                if result == "no_new":
                    progress.mark_exhausted(category)
                    log.info("%s exhausted", category)
                elif result == "ok":
                    progress.report_progress()

            log.info("Scrape phase done. New listings inserted: %d", _total_saved)
        finally:
            try:
                await ctx_holder["ctx"].close()
            except Exception:
                pass
    db.close()


# Back-compat alias for anything still calling main().
main = run_scraper


if __name__ == "__main__":
    asyncio.run(run_scraper())
