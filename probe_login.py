"""One-shot debug: load home page, dump any element that mentions 'login'
or could be a login trigger. Helps figure out the right selector."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str((Path(__file__).resolve().parent / "src").as_posix()))
from dotenv import load_dotenv
load_dotenv()

try:
    from patchright.async_api import async_playwright
except ImportError:
    from playwright.async_api import async_playwright

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

async def main():
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            ".pw-data-probe",
            headless=False,
            args=["--disable-blink-features=AutomationControlled",
                  "--disable-features=DnsOverHttps"],
            user_agent=USER_AGENT,
            locale="vi-VN",
            viewport={"width": 1366, "height": 768},
            timezone_id="Asia/Ho_Chi_Minh",
        )
        page = await ctx.new_page()
        print("Loading https://batdongsan.com.vn/ ...")
        await page.goto("https://batdongsan.com.vn/", wait_until="domcontentloaded", timeout=30000)
        print(f"Title: {await page.title()}")
        print(f"URL:   {page.url}")
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
            print("networkidle reached")
        except Exception as e:
            print(f"networkidle wait failed: {e}")

        # 1. Dump all anchor elements with text containing "log in" or "đăng nhập"
        report = await page.evaluate("""() => {
            const out = {
                title: document.title,
                avatar_candidates: [],
                login_candidates: [],
                buttons_with_id_starting_with_kct: [],
                anchors_with_login_text: [],
            };
            for (const el of document.querySelectorAll('[class*="avatar" i], .js__avatar, [class*="user-info" i]')) {
                out.avatar_candidates.push({
                    tag: el.tagName, id: el.id || null,
                    class: el.className,
                    visible: !!(el.offsetWidth && el.offsetHeight),
                });
            }
            for (const el of document.querySelectorAll('a, button')) {
                const id = el.id || '';
                const txt = (el.textContent || '').trim().toLowerCase();
                if (id.startsWith('kct')) {
                    out.buttons_with_id_starting_with_kct.push({
                        tag: el.tagName, id, text: el.textContent.trim().slice(0,80),
                        visible: !!(el.offsetWidth && el.offsetHeight),
                    });
                }
                if (/đăng nhập|log in|sign in/i.test(txt)) {
                    out.anchors_with_login_text.push({
                        tag: el.tagName, id, class: el.className,
                        text: el.textContent.trim().slice(0,80),
                        visible: !!(el.offsetWidth && el.offsetHeight),
                    });
                }
                if (/login|signin/i.test(id) || /login|signin/i.test(el.getAttribute('tracking-id') || '')) {
                    out.login_candidates.push({
                        tag: el.tagName, id, class: el.className,
                        text: el.textContent.trim().slice(0,80),
                        visible: !!(el.offsetWidth && el.offsetHeight),
                    });
                }
            }
            return out;
        }""")
        print()
        import json
        print(json.dumps(report, indent=2, ensure_ascii=False))

        # Screenshot for visual confirmation
        Path("logs").mkdir(exist_ok=True)
        shot = "logs/probe-home.png"
        await page.screenshot(path=shot, full_page=False)
        print(f"\nScreenshot saved to {shot}")

        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
