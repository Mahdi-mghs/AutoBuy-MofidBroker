"""
Throwaway selector-discovery script (run once against the live broker).

Logs in, opens a symbol page and dumps every candidate input/button so the
real selectors can be baked into locators.py. NEVER clicks the buy button.
"""

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv(Path(__file__).parent / ".env")

URL = os.getenv("BROKER_URL", "https://d.easytrader.ir/")
USER = os.getenv("MOFID_USER", "")
PASS = os.getenv("MOFID_PASS", "")
SYMBOL = os.getenv("SYMBOL", "") or "فولاد"

OUT = Path(__file__).parent / "artifacts"
OUT.mkdir(exist_ok=True)

DUMP_JS = """
() => {
  const describe = (el) => ({
    tag: el.tagName.toLowerCase(),
    id: el.id || null,
    name: el.getAttribute('name'),
    formcontrolname: el.getAttribute('formcontrolname'),
    type: el.getAttribute('type'),
    placeholder: el.getAttribute('placeholder'),
    dataCy: el.getAttribute('data-cy'),
    cls: (el.className && el.className.toString) ? el.className.toString().slice(0, 120) : null,
    text: (el.innerText || '').trim().slice(0, 60),
    value: el.value !== undefined ? el.value : null,
  });
  const inputs = [...document.querySelectorAll('input')].filter(i => i.type !== 'hidden').map(describe);
  const buttons = [...document.querySelectorAll('button')].map(describe);
  const tabs = [...document.querySelectorAll('li, a[role="tab"], mat-tab')].map(describe).filter(x => x.text);
  return {inputs, buttons, tabs};
}
"""


def main():
    log = print
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(OUT / "discovery-profile"),
            channel="chrome",
            headless=False,
            ignore_https_errors=True,
            viewport={"width": 1600, "height": 1000},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        # ── Login ──
        page.goto(URL, wait_until="domcontentloaded")
        page.screenshot(path=str(OUT / "01-login-page.png"))
        page.fill("#user-name", USER)
        page.fill("#password", PASS)
        page.click("#primary_form button")
        try:
            page.wait_for_selector(
                "#root > main > div:nth-child(2) > div:nth-child(1) > ul:nth-child(1) > li:nth-child(2)",
                timeout=20000,
            )
            log("LOGIN OK")
        except Exception as e:
            log(f"LOGIN FAILED: {e}")
            page.screenshot(path=str(OUT / "login-failed.png"), full_page=True)
            ctx.close()
            sys.exit(1)

        page.screenshot(path=str(OUT / "02-after-login.png"))

        # ── Dismiss announcement modal if present ──
        try:
            page.click("#cancel-action-confirm-btn", timeout=4000)
            log("announcement dismissed")
        except Exception:
            log("no announcement modal")

        # ── Dump post-login state ──
        state = page.evaluate(DUMP_JS)
        (OUT / "state-after-login.json").write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # ── Symbol search via data-cy menu icon ──
        try:
            page.click('li[data-cy="search-menu-icon"]', timeout=10000)
            log("search menu opened")
            # The search panel should now be visible — find its input
            SEARCH = 'input[data-cy="layout-search-input"]'
            try:
                page.wait_for_selector(SEARCH, timeout=8000)
                log("search input found (data-cy=layout-search-input)")
            except Exception:
                log("search input NOT found — dumping inputs")
                dump = page.evaluate(DUMP_JS)
                (OUT / "state-search-panel.json").write_text(
                    json.dumps(dump, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                raise
            page.fill(SEARCH, SYMBOL)
            page.wait_for_timeout(1500)  # let suggestions render
            page.screenshot(path=str(OUT / "03-symbol-suggestions.png"))
            # dump suggestion DOM
            suggestions = page.evaluate("""
                () => [...document.querySelectorAll('li, [class*="result"], [class*="option"], [role="option"]')]
                  .filter(el => (el.innerText||'').trim().length > 0 && (el.innerText||'').length < 200)
                  .slice(0, 15)
                  .map(el => ({tag: el.tagName.toLowerCase(), cls: (el.className.toString()||'').slice(0,100),
                               text: (el.innerText||'').trim().slice(0,80), id: el.id,
                               dataCy: el.getAttribute('data-cy')}))
            """)
            (OUT / "suggestions.json").write_text(
                json.dumps(suggestions, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            # click the exact-symbol search item via data-cy
            clicked = False
            for sel in [
                f'[data-cy="search-item-name-{SYMBOL}"]',
                f'[data-cy="search-panel-item-{SYMBOL}"]',
                "[data-cy^='search-item-name-'] >> nth=0",
            ]:
                try:
                    page.click(sel, timeout=3000)
                    clicked = True
                    log(f"suggestion clicked via {sel}")
                    break
                except Exception:
                    continue
            if not clicked:
                # fallback: press Down+Enter in the search box
                page.press(SEARCH, "ArrowDown")
                page.press(SEARCH, "Enter")
                log("suggestion selected via keyboard")
            page.wait_for_timeout(2500)
        except Exception as e:
            log(f"symbol navigation issue: {e}")

        # ── Open the order form (buy side) ──
        try:
            page.wait_for_selector('[data-cy="renderer-symbol-name"]', timeout=10000)
            log("symbol page loaded (renderer-symbol-name present)")
        except Exception:
            log("renderer-symbol-name NOT found")
        try:
            page.click('[data-cy="order-buy-btn"]', timeout=8000)
            log("order form opened via order-buy-btn")
            page.wait_for_timeout(1500)
        except Exception as e:
            log(f"order-buy-btn click failed: {e}")

        # ── Dump order form state (price/qty/quick buttons) ──
        try:
            page.wait_for_selector('button[data-cy="oms-order-form-submit-button-buy"]', timeout=8000)
            log("BUY BUTTON FOUND")
        except Exception:
            log("buy button NOT found after opening order form")
        form_state = page.evaluate(DUMP_JS)
        (OUT / "state-order-form.json").write_text(
            json.dumps(form_state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        form_extra = page.evaluate("""
            () => {
              const inForm = (el) => !!el.closest('order-form, [class*="order-form"], app-order-form, d-order-list');
              const inputs = [...document.querySelectorAll('input')]
                .filter(i => i.type !== 'hidden')
                .map(i => ({id: i.id, name: i.name, fcn: i.getAttribute('formcontrolname'),
                            placeholder: i.placeholder, aria: i.getAttribute('aria-label'),
                            type: i.type, value: i.value, dataCy: i.getAttribute('data-cy'),
                            inForm: inForm(i), parentText: (i.parentElement?.innerText||'').trim().slice(0,50),
                            cls: i.className.slice(0,80)}));
              const quick = [...document.querySelectorAll('button, a, span, label')]
                .filter(el => /سقف|مرجع|کف قیمت|قیمت مرجع/.test(el.innerText||''))
                .slice(0, 20)
                .map(el => ({tag: el.tagName.toLowerCase(), dataCy: el.getAttribute('data-cy'),
                             cls: (el.className.toString()||'').slice(0,90),
                             text: (el.innerText||'').trim().slice(0,40), inForm: inForm(el)}));
              const labels = [...document.querySelectorAll('label, [class*="label"]')]
                .filter(el => (el.innerText||'').trim().length < 40 && (el.innerText||'').trim().length > 0)
                .slice(0, 30)
                .map(el => ({tag: el.tagName.toLowerCase(), text: (el.innerText||'').trim(),
                             forAttr: el.getAttribute('for'), dataCy: el.getAttribute('data-cy')}));
              return {inputs, quick, labels};
            }
        """)
        (OUT / "order-form-detail.json").write_text(
            json.dumps(form_extra, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        page.screenshot(path=str(OUT / "05-order-form.png"), full_page=True)
        (OUT / "order-form.html").write_text(page.content(), encoding="utf-8")

        page.screenshot(path=str(OUT / "04-symbol-page.png"), full_page=True)
        (OUT / "symbol-page.html").write_text(page.content(), encoding="utf-8")

        # ── Dump order-form state ──
        state2 = page.evaluate(DUMP_JS)
        (OUT / "state-symbol-page.json").write_text(
            json.dumps(state2, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # ── Extra: quick-price buttons and form containers ──
        extra = page.evaluate("""
            () => {
              const match = (el, kws) => kws.some(k => (el.innerText||'').includes(k));
              const quick = [...document.querySelectorAll('button, a, span, div')]
                .filter(el => match(el, ['سقف قیمت','قیمت مرجع','کف قیمت','مرجع','سقف']))
                .slice(0, 20)
                .map(el => ({tag: el.tagName.toLowerCase(), cls: el.className.toString().slice(0,100),
                             text: (el.innerText||'').trim().slice(0,40)}));
              const forms = [...document.querySelectorAll('order-form, order-form-actions, [class*="order-form"]')]
                .slice(0, 10)
                .map(el => ({tag: el.tagName.toLowerCase(), cls: el.className.toString().slice(0,100)}));
              return {quick, forms};
            }
        """)
        (OUT / "quick-price.json").write_text(
            json.dumps(extra, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # ── Buy button presence check (no click!) — already done above ──

        ctx.close()
    log(f"artifacts written to {OUT}")


if __name__ == "__main__":
    main()
