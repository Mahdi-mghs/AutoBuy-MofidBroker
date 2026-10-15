"""
Real-browser smoke test — verifies the full automated flow EXCEPT the final
buy click. This script NEVER submits an order.

Steps: launch Chrome → login → dismiss modal → read broker clock →
auto-prepare the order (navigate + fill price/qty) → verify the buy button
exists → screenshot every stage → close.

Usage:
    python smoke_test.py            # uses SYMBOL / QUANTITY / PRICE_MODE from .env
    python smoke_test.py --symbol فولاد
"""

import argparse
import sys


def _utf8_streams():
    """Windows consoles default to cp1252 — Persian log text must not crash it."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

import locators
from config import SYMBOL, QUANTITY, PRICE_MODE, PRICE
from driver import initialize_driver, cleanup_all
from broker import login, dismiss_announcement, get_broker_time
from order import prepare_order, find_buy_button

from pathlib import Path

OUT = Path(__file__).parent / "artifacts"


def shot(page: Page, name: str) -> None:
    OUT.mkdir(exist_ok=True)
    page.screenshot(path=str(OUT / name), full_page=False)
    print(f"  [shot] {name}")


def main() -> int:
    _utf8_streams()
    parser = argparse.ArgumentParser(description="No-submit smoke test")
    parser.add_argument("--symbol", default=None)
    args = parser.parse_args()
    symbol = args.symbol or SYMBOL

    failures: list[str] = []
    page = None
    try:
        # ── 1. Launch + login ──
        print("[1/6] launching browser + login ...")
        page = initialize_driver()
        if not login(page):
            print("FAIL: login")
            return 1
        dismiss_announcement(page)
        shot(page, "smoke-01-after-login.png")

        # ── 2. Broker clock ──
        print("[2/6] reading broker clock ...")
        try:
            clock = get_broker_time(page)
            print(f"  broker clock: {clock.strftime('%H:%M:%S')}")
        except Exception as e:
            failures.append(f"broker clock unreadable: {e}")
            print(f"  FAIL: {e}")

        # ── 3. Auto-preparation ──
        print(f"[3/6] preparing order for {symbol!r} "
              f"(qty={QUANTITY}, mode={PRICE_MODE}, fixed_price={PRICE}) ...")
        try:
            summary = prepare_order(page, symbol=symbol)
            print(f"  prepared: {summary}")
            if symbol and symbol not in summary["symbol"]:
                failures.append(
                    f"symbol mismatch: ticket {summary['symbol']!r} != {symbol!r}"
                )
            if summary["quantity"] != QUANTITY:
                failures.append(
                    f"quantity mismatch: field {summary['quantity']} != {QUANTITY}"
                )
            if summary["price"] <= 0:
                failures.append("price field empty after preparation")
        except Exception as e:
            failures.append(f"prepare_order failed: {e}")
            print(f"  FAIL: {e}")
        shot(page, "smoke-02-after-prep.png")

        # ── 4. Buy button presence (NO CLICK) ──
        print("[4/6] locating buy button (will NOT click) ...")
        btn = find_buy_button(page, timeout=5)
        if btn is None:
            failures.append("buy button not found/visible")
            print("  FAIL: buy button not visible")
        else:
            print(f"  buy button visible: {btn.get_attribute('aria-label') or 'ارسال خرید'}")
        shot(page, "smoke-03-ready.png")

        # ── 5. Report field states ──
        print("[5/6] field states ...")
        try:
            price_val = page.locator(locators.PRICE_INPUT).first.input_value(timeout=2000)
            qty_val = page.locator(locators.QUANTITY_INPUT).first.input_value(timeout=2000)
            max_chip = page.locator(locators.MAX_PRICE_CHIP).first.inner_text(timeout=2000)
            print(f"  price={price_val}  max-chip={max_chip.strip()}  qty={qty_val}")
        except Exception as e:
            failures.append(f"could not read form fields: {e}")
            print(f"  FAIL: {e}")

        # ── 6. Done — never touched the buy button ──
        print("[6/6] closing browser (buy button was NOT clicked)")
    except Exception as e:
        failures.append(f"unexpected error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if page is not None:
            try:
                shot(page, "smoke-final.png")
            except Exception:
                pass
        cleanup_all()

    print()
    if failures:
        print(f"SMOKE TEST FAILED ({len(failures)} issue(s)):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("SMOKE TEST PASSED — full flow ready, no order submitted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
