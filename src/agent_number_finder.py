"""Agent phone-number finder with multi-account + multi-proxy rotation.

For every agent in DB whose `phone_number IS NULL`:
  1. Pick an active (account, proxy) pair.
  2. Open the agent's `agent_url` on guru.batdongsan.com.vn.
  3. Log in once with that account (cookies cached per-account in
     `.pw-data-<n>/` so future runs reuse them).
  4. Click the red phone-reveal button → read the unmasked number.
  5. UPDATE scrapy SET phone_number = ? WHERE agent_id = ?
     — fills every listing for that agent in one shot.
  6. Sleep AGENT_FETCH_DELAY_*_SEC (default 5 min) so the per-account
     reveal quota burns slowly.
  7. If Cloudflare blocks OR the account is rate-limited → mark this
     (account, proxy) cool for COOLDOWN_SEC and switch to the next pair.

Inputs:
  data/accounts.csv     — username,password  (one row per batdongsan account)
  data/proxies.csv      — server,username,password  (one row per proxy)

Run standalone:
    python src/agent_number_finder.py
"""
import asyncio
import csv
import logging
import os
import random
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dotenv import load_dotenv
load_dotenv()

from store import make_store  # noqa: E402

try:
    from patchright.async_api import async_playwright, TimeoutError as PWTimeout
    _BACKEND = "patchright"
except ImportError:
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
    _BACKEND = "playwright"


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("number-finder")
log.info("Backend: %s", _BACKEND)


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

AGENT_FETCH_DELAY_MIN_SEC = int(os.getenv("AGENT_FETCH_DELAY_MIN_SEC", "270"))  # 4m30s
AGENT_FETCH_DELAY_MAX_SEC = int(os.getenv("AGENT_FETCH_DELAY_MAX_SEC", "330"))  # 5m30s
COOLDOWN_SEC = int(os.getenv("ACCOUNT_COOLDOWN_SEC", "1800"))  # 30 min penalty
HEADLESS = os.getenv("FINDER_HEADLESS", os.getenv("HEADLESS", "false")).lower() == "true"
ACCOUNTS_CSV = Path(os.getenv("ACCOUNTS_CSV", "data/accounts.csv"))
PROXIES_CSV  = Path(os.getenv("PROXIES_CSV",  "data/proxies.csv"))

LOGIN_URL = "https://batdongsan.com.vn/sellernet/trang-dang-nhap"
HOME_URL  = "https://batdongsan.com.vn/"


_PROFILE_PHONE_SEL = (
    'button.styles_agent-contact-phone__xgD_q, '
    'button[class*="agent-contact-phone"], '
    '.js__phone-event, .js__phone, .re__btn-phone-icon, '
    '.js__card-phone-btn, .js__btn-tracking[data-phone-index]'
)
_INTERSTITIAL_TITLES = ("chờ một chút", "just a moment")


# ────────────────────────────────────────────  CSV loaders

def load_accounts() -> list[dict]:
    if not ACCOUNTS_CSV.exists():
        log.error("Accounts file not found: %s", ACCOUNTS_CSV)
        return []
    out = []
    with ACCOUNTS_CSV.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            u = (row.get("username") or "").strip()
            p = (row.get("password") or "").strip()
            if u and p:
                out.append({"username": u, "password": p,
                            "cool_until": 0.0})
    log.info("Loaded %d accounts.", len(out))
    return out


def load_proxies() -> list[dict]:
    if not PROXIES_CSV.exists():
        log.warning("Proxies file not found: %s — running without proxy.", PROXIES_CSV)
        return [{"server": None, "username": None, "password": None,
                 "cool_until": 0.0}]
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
    if not out:
        out.append({"server": None, "username": None, "password": None,
                    "cool_until": 0.0})
    log.info("Loaded %d proxies.", len(out))
    return out


def pick_pair(accounts: list[dict], proxies: list[dict]) -> tuple[dict, dict] | None:
    now = time.time()
    a_cands = [a for a in accounts if a["cool_until"] <= now]
    p_cands = [p for p in proxies  if p["cool_until"] <= now]
    if not a_cands or not p_cands:
        return None
    return random.choice(a_cands), random.choice(p_cands)


# ────────────────────────────────────────────  DB
# All four helpers used to live here as raw pymysql calls. They now delegate
# to the backend-agnostic store (src/store.py → PostgresStore / Database).


# ────────────────────────────────────────────  Browser helpers

async def is_interstitial(page) -> bool:
    try:
        title = ((await page.title()) or "").lower().strip()
    except Exception:
        return False
    return any(s in title for s in _INTERSTITIAL_TITLES)


async def login_with_account(ctx, account: dict) -> str:
    """Hit the seller-net login page directly and fill the form. Returns:
        'ok'         logged in (cached cookie OR fresh login verified)
        'auth_fail'  credentials rejected (avatar never appeared)
        'cloudflare' Cloudflare blocked the page
        'transient'  page elements missing — retry next iteration
    """
    page = await ctx.new_page()
    try:
        try:
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
        except PWTimeout:
            log.warning("  login URL timeout")
            return "transient"
        if await is_interstitial(page):
            log.warning("  Cloudflare on login page")
            return "cloudflare"

        # The site shows a preloader ("Vui lòng chờ một chút...") while
        # the SPA mounts. Wait for it to disappear before doing anything
        # else — querying the form before this point returns nothing.
        try:
            await page.wait_for_selector(
                'text="Vui lòng chờ một chút"', state="hidden", timeout=20000,
            )
        except Exception:
            # Either it never appeared (good) or it never went away
            # (page stuck). Fall through; the form check below will tell us.
            pass

        # Also let lazy network requests settle. Bounded so a stuck request
        # doesn't keep us here forever.
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass

        # If the persistent context already had a session, batdongsan
        # redirects away from /trang-dang-nhap to the dashboard.
        if "trang-dang-nhap" not in (page.url or "").lower():
            log.info("  already logged in (redirected away from login URL)")
            return "ok"
        if await _has_avatar(page):
            log.info("  already logged in (avatar present)")
            return "ok"

        # Try several possible username input selectors — the modal's input
        # naming varies between layouts.
        user_input = None
        for sel in ('input[name="username"]',
                    'input[name="phone"]',
                    'input[name="email"]',
                    'input[autocomplete="username"]',
                    '.modal input[type="text"]',
                    '.modal input[type="tel"]',
                    'input[placeholder*="ố điện thoại" i]',  # "Số điện thoại"
                    'input[placeholder*="đăng nhập" i]'):
            try:
                el = page.locator(sel).first
                if await el.count() == 0:
                    continue
                if not await el.is_visible():
                    continue
                user_input = el
                log.info("  username input matched: %s", sel)
                break
            except Exception:
                continue

        if user_input is None:
            try:
                shot = Path("logs") / f"login-fail-{account['username']}-{int(time.time())}.png"
                shot.parent.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(shot), full_page=True)
                log.warning("  login modal opened but no username input — "
                            "screenshot saved to %s", shot)
            except Exception:
                log.warning("  login modal opened but no username input")
            return "transient"

        # Find the matching password input (most likely sibling).
        pw_input = None
        for sel in ('input[type="password"]',
                    'input[name="password"]',
                    'input[autocomplete="current-password"]'):
            try:
                el = page.locator(sel).first
                if await el.count() == 0:
                    continue
                if not await el.is_visible():
                    continue
                pw_input = el
                break
            except Exception:
                continue
        if pw_input is None:
            log.warning("  password input not found")
            return "transient"

        # Use the legacy `_human_type`: clicks the field, clears it, then
        # types char-by-char through page.keyboard with random delays.
        # This is what the old (working) login flow used.
        try:
            await _human_type(page, user_input, account["username"])
            await _human_type(page, pw_input, account["password"])
        except Exception as e:
            log.warning("  typing failed: %s", str(e)[:120])
            return "transient"

        # Sanity-check: did the inputs actually receive the text?
        try:
            u_val = await user_input.input_value()
            p_val = await pw_input.input_value()
        except Exception:
            u_val = p_val = ""
        if not u_val or not p_val:
            log.warning("  inputs still empty after typing (u=%r len(p)=%d) — "
                        "form likely uses non-standard binding", u_val, len(p_val or ""))
            try:
                shot = Path("logs") / f"login-emptyfields-{account['username']}-{int(time.time())}.png"
                shot.parent.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(shot), full_page=False)
                log.warning("  screenshot saved to %s", shot)
            except Exception:
                pass
            return "transient"
        log.info("  inputs filled (u_len=%d, p_len=%d)", len(u_val), len(p_val))
        # Snapshot the filled form so we can verify visually.
        try:
            pre = Path("logs") / f"login-prefilled-{account['username']}-{int(time.time())}.png"
            pre.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(pre), full_page=False)
        except Exception:
            pass
        # Submit via Enter on the password field — the legacy flow's
        # preferred path. AJAX forms react to this consistently.
        try:
            await page.keyboard.press("Enter")
        except Exception as e:
            log.warning("  Enter submit failed: %s", str(e)[:120])
            return "transient"
        await page.wait_for_timeout(2500)  # give the AJAX call time to complete

        # Verify by RELOADING the home page and checking for avatar there.
        # /trang-dang-nhap doesn't always auto-redirect after AJAX login,
        # so we navigate to BASE and look for the user element.
        for _ in range(8):
            try:
                await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=15000)
            except Exception:
                pass
            if await _has_avatar(page):
                log.info("  login succeeded (avatar visible on home)")
                return "ok"
            await page.wait_for_timeout(2000)

        # Failed — capture state for debugging.
        try:
            shot = Path("logs") / f"login-noavatar-{account['username']}-{int(time.time())}.png"
            shot.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(shot), full_page=True)
            log.warning("  no avatar after 8 reloads — credentials likely wrong "
                        "or CAPTCHA — screenshot saved to %s", shot)
        except Exception:
            log.warning("  no avatar after 8 reloads")
        return "auth_fail"
    finally:
        try: await page.close()
        except Exception: pass


async def _has_avatar(page) -> bool:
    """Avatar selectors used by the working legacy login flow."""
    sel = '.js__avatar, .re__avatar--xl, .re__avatar-text'
    try:
        return await page.evaluate(f"() => !!document.querySelector('{sel}')")
    except Exception:
        return False


async def _human_type(page, locator, text: str) -> None:
    """Type char-by-char via the GLOBAL keyboard with random 50-180ms delays.
    Mirrors the legacy `_human_type` that batdongsan's login form accepts.

    Locator.fill() or Locator.type() sometimes don't fire the keydown/input
    events the React controlled-input needs — typing through page.keyboard
    after focusing the element is what works.
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


async def _check_exit_ip(page) -> str | None:
    """Hit api.ipify.org through the current browser context. Lets us
    confirm the proxy is actually routing traffic."""
    try:
        resp = await page.evaluate(
            "() => fetch('https://api.ipify.org?format=json')"
            "      .then(r => r.json()).then(j => j.ip).catch(() => null)"
        )
        return resp
    except Exception:
        return None


async def reveal_on_profile(page) -> str | None:
    btn = page.locator(_PROFILE_PHONE_SEL).first
    try:
        await btn.wait_for(state="visible", timeout=15000)
    except Exception:
        log.warning("  no phone button on profile")
        return None
    try:
        await btn.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    await asyncio.sleep(random.uniform(1.5, 3.5))
    try:
        await btn.click(timeout=5000, delay=random.randint(40, 120))
    except Exception as e:
        log.warning("  click failed: %s", str(e)[:120])
        return None

    deadline_ms = 10000
    interval_ms = 400
    elapsed = 0
    while elapsed < deadline_ms:
        await page.wait_for_timeout(interval_ms)
        elapsed += interval_ms
        try:
            phone = await page.evaluate("""() => {
                for (const el of document.querySelectorAll(
                    'button[class*="agent-contact-phone"], .js__phone-event, .js__phone, '
                    '.re__btn-phone-icon, .js__card-phone-btn, [data-phone]')) {
                    const m = el.getAttribute('mobile');
                    if (m && !m.includes('*')) return m.trim();
                }
                for (const btn of document.querySelectorAll('button[class*="agent-contact-phone"]')) {
                    for (const p of btn.querySelectorAll('p')) {
                        const t = (p.textContent || '').trim();
                        if (t && /\\d{3,}/.test(t) && !t.includes('*')) return t;
                    }
                }
                for (const a of document.querySelectorAll('a[href^="tel:"]')) {
                    const t = (a.textContent || '').trim();
                    if (t && !t.includes('*')) return t;
                    const h = a.getAttribute('href') || '';
                    const digits = h.replace(/[^\\d+]/g, '');
                    if (digits.length >= 9) return digits;
                }
                return null;
            }""")
        except Exception:
            phone = None
        if phone:
            digits = re.sub(r"\D", "", phone)
            if len(digits) >= 9:
                return phone
    return None


# ────────────────────────────────────────────  Per-agent worker

async def process_one_agent(p, account: dict, proxy: dict, agent: tuple[str, str, str],
                             store) -> str:
    """Returns one of:
        'ok'           phone revealed + DB updated
        'no_phone'     reveal click registered, but number never unmasked
        'no_button'    profile loaded, no reveal element (corporate/missing)
        'cloudflare'   Cloudflare blocked → cool BOTH account and proxy
        'auth_fail'    credentials rejected (avatar never showed after submit)
                       → cool account only
        'transient'    page didn't render expected elements (no button, no
                       modal, no form, proxy timeout). Don't cool — just retry.
        'error'        unexpected exception
    """
    agent_id, agent_url, posted_by = agent
    if not agent_url:
        return "no_button"

    user_dir = f".pw-data-acct-{account['username']}"

    # The phone finder runs without a proxy — direct from this machine's IP.
    # Proxies add latency and weren't helping with the login flow. Accounts
    # are rotated via `accounts.csv`; batdongsan rate-limits per account,
    # not per IP, so the same exit IP is fine.
    ctx = await p.chromium.launch_persistent_context(
        user_dir,
        headless=HEADLESS,
        args=["--disable-blink-features=AutomationControlled",
              "--disable-features=DnsOverHttps"],
        user_agent=USER_AGENT,
        locale="vi-VN",
        viewport={"width": 1366, "height": 768},
        timezone_id="Asia/Ho_Chi_Minh",
    )
    try:
        outcome = await login_with_account(ctx, account)
        if outcome != "ok":
            return outcome  # propagate auth_fail / transient / cloudflare

        page = await ctx.new_page()
        try:
            try:
                await page.goto(agent_url, wait_until="domcontentloaded", timeout=45000)
            except PWTimeout:
                return "transient"
            if await is_interstitial(page):
                return "cloudflare"
            phone = await reveal_on_profile(page)
            if phone is None:
                return "no_phone"
            n = store.set_agent_phone(agent_id, phone)
            log.info("  ✓ phone=%s  (agent %s, posted_by=%r, %d listings updated)",
                     phone, agent_id, posted_by, n)
            return "ok"
        finally:
            try: await page.close()
            except Exception: pass
    finally:
        try: await ctx.close()
        except Exception: pass


# ────────────────────────────────────────────  Main loop

async def run_finder() -> None:
    accounts = load_accounts()
    if not accounts:
        log.error("No accounts — create data/accounts.csv (username,password) and re-run.")
        return

    store = make_store()
    try:
        async with async_playwright() as p:
            log.info("Phone finder loop started. Direct connection (no proxy).")
            while True:
                # Pick an account that isn't on cooldown.
                now = time.time()
                a_cands = [a for a in accounts if a["cool_until"] <= now]
                if not a_cands:
                    log.warning("All accounts are cooling down — sleeping 60s.")
                    await asyncio.sleep(60)
                    continue
                account = random.choice(a_cands)

                # Backfill: copy known phones onto any new listings the scraper
                # inserted, so we never re-reveal an agent we already know.
                backfilled = store.backfill_phones_from_known_agents()
                if backfilled:
                    log.info("Backfilled %d listings from already-known agent phones",
                             backfilled)

                agents = store.pending_agents(limit=1)
                if not agents:
                    log.info("No more agents need phones. Sleeping 5 min before recheck.")
                    await asyncio.sleep(300)
                    continue
                agent = agents[0]

                log.info("→ Agent %s  via account=%s",
                         agent[0], account["username"])
                try:
                    outcome = await process_one_agent(p, account, None, agent, store)
                except Exception as e:
                    log.exception("  unexpected error: %s", e)
                    outcome = "error"

                # Cooldown decisions.
                now = time.time()
                if outcome == "auth_fail":
                    account["cool_until"] = now + COOLDOWN_SEC
                    log.warning("  Auth failed (credentials rejected) — cooling "
                                "account for %ds", COOLDOWN_SEC)
                elif outcome == "cloudflare":
                    account["cool_until"] = now + COOLDOWN_SEC
                    log.warning("  Cloudflare blocked this account — cooling for %ds",
                                COOLDOWN_SEC)
                # 'transient' / 'ok' / 'no_phone' / 'no_button' / 'error'
                # → no cooldown, just sleep and try next.

                delay = random.randint(AGENT_FETCH_DELAY_MIN_SEC,
                                        AGENT_FETCH_DELAY_MAX_SEC)
                m, s = divmod(delay, 60)
                log.info("Sleeping %dm%02ds before next agent…", m, s)
                await asyncio.sleep(delay)
    finally:
        try: store.close()
        except Exception: pass


if __name__ == "__main__":
    asyncio.run(run_finder())
