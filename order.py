"""
Order actions: automated preparation (symbol/price/quantity), buy click,
verification in the orders list.
Playwright port.
"""

import logging
import time

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

import locators
from config import (
    DEFAULT_TIMEOUT,
    BUY_MAX_ATTEMPTS,
    BUY_RETRY_INTERVAL,
    VERIFY_ORDER,
    VERIFY_ORDER_TIMEOUT,
    PRICE_MODE,
    PRICE,
    QUANTITY,
)
from broker import capture_broker_notification, is_order_accepted, dismiss_announcement

logger = logging.getLogger(__name__)


def _first_visible(page: Page, selectors, timeout_s: float):
    """Return the first visible locator among `selectors`, else None."""
    deadline = time.time() + timeout_s
    for sel in selectors:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        try:
            loc = page.locator(f"xpath={sel}") if sel.startswith("//") else page.locator(sel)
            el = loc.first
            el.wait_for(state="visible", timeout=int(remaining * 1000))
            return el
        except PlaywrightTimeoutError:
            continue
    return None


def find_buy_button(page: Page, timeout=2):
    """Try the primary data-cy locator then fallbacks. Returns Locator or None."""
    selectors = [locators.ORDER_FORM_BUY] + locators.ORDER_FORM_BUY_FALLBACKS
    return _first_visible(page, selectors, timeout)


def _dismiss_layout_overlay(page: Page):
    """
    The symbol-search panel leaves `main-layout-overlay` active for a
    moment after selection (or until dismissed) and that backdrop
    intercepts ALL clicks — including the order-ticket opener. Close it
    deterministically: click the backdrop, then Escape, then toggle the
    search menu; give up after a few rounds so we fail loudly instead of
    timing out on every later click.
    """
    overlay = page.locator('[data-cy="main-layout-overlay"]')
    for attempt in range(6):
        if overlay.count() == 0:
            return
        cls = overlay.get_attribute("class") or ""
        if "active" not in cls:
            return
        try:
            if attempt == 0:
                overlay.click(timeout=1500)      # backdrop click closes panel
            elif attempt == 1:
                page.keyboard.press("Escape")
            else:
                page.locator(locators.SEARCH_MENU).first.click(timeout=1500)
        except Exception:
            pass
        page.wait_for_timeout(400)
    cls = overlay.get_attribute("class") or ""
    if "active" in cls:
        raise RuntimeError("Layout overlay still active — clicks would be blocked.")


# ── Automated order preparation ─────────────────────────
def _wait_search_results(page: Page, timeout_ms: int) -> bool:
    """True if ANY search result row is visible within the timeout."""
    try:
        page.locator('[data-cy^="search-item-name-"]').first.wait_for(
            state="visible", timeout=timeout_ms
        )
        return True
    except PlaywrightTimeoutError:
        return False


def _select_symbol(page: Page, symbol: str, timeout_s: int = DEFAULT_TIMEOUT):
    """
    Open the sidebar search, type the symbol, click the exact result.

    The Angular search occasionally doesn't react to a programmatic
    fill() (observed once live: no results for 8s, keyboard fallback then
    selected an unrelated recent symbol). So: verify results actually
    appeared, nudge with real key events when they didn't, and retry
    before resorting to Down+Enter — the ticket-header assertion in
    prepare_order guards the final outcome either way.
    """
    ms = timeout_s * 1000
    search_input = page.locator(locators.SEARCH_INPUT).first

    # Make sure the search panel is open
    if not search_input.is_visible():
        page.locator(locators.SEARCH_MENU).first.click(timeout=ms)
    search_input.wait_for(state="visible", timeout=ms)

    for attempt in range(1, 4):
        search_input.fill("")  # ensure a value change → input event fires
        page.wait_for_timeout(150)
        search_input.fill(symbol)

        if _wait_search_results(page, 4000):
            item = page.locator(locators.search_item(symbol)).first
            try:
                item.wait_for(state="visible", timeout=4000)
                item.click(timeout=3000)
                logger.info("Symbol selected via exact result (attempt %d).", attempt)
                return
            except PlaywrightTimeoutError:
                logger.warning(
                    "Results shown but exact item %r missing (attempt %d).",
                    symbol, attempt,
                )
        else:
            logger.warning("No search results after fill (attempt %d) — nudging.", attempt)

        # Nudge: real key events often kick the custom input's search
        search_input.click()
        page.keyboard.type(" ", delay=50)
        page.keyboard.press("Backspace")
        page.wait_for_timeout(1200)

    # Last resort: keyboard select first highlighted result
    logger.warning("Exact selection failed 3× — falling back to Down+Enter.")
    search_input.click()
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")


def prepare_order(
    page: Page,
    symbol: str,
    quantity: int | None = None,
    price_mode: str | None = None,
    price: int | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """
    Fully automated replacement for the old manual 'prepare the order'
    pause: navigate to the symbol, open the buy ticket, fill quantity
    and price according to the price mode.

    Returns a dict of what was actually set (symbol, quantity, price,
    price_mode) so callers can log/verify it. Raises RuntimeError when
    any step cannot be completed — never silently half-prepares.
    """
    symbol = symbol or ""
    quantity = QUANTITY if quantity is None else quantity
    price_mode = (price_mode or PRICE_MODE).lower()
    price = PRICE if price is None else price

    if not symbol:
        raise RuntimeError("No symbol configured. Set SYMBOL or pass --symbol.")
    if quantity <= 0:
        raise RuntimeError("No quantity configured. Set QUANTITY (shares > 0).")
    if price_mode not in ("ceiling", "reference", "fixed"):
        raise RuntimeError(f"Unknown PRICE_MODE {price_mode!r} (ceiling|reference|fixed).")
    if price_mode == "fixed" and price <= 0:
        raise RuntimeError("PRICE_MODE=fixed requires PRICE > 0.")

    ms = timeout * 1000

    # ── 1+2. Symbol navigation, open ticket, assert right symbol ──
    logger.info("Preparing order: symbol=%s qty=%d price_mode=%s", symbol, quantity, price_mode)
    opened_for = None
    for prep_attempt in range(1, 3):
        _select_symbol(page, symbol, timeout)
        page.wait_for_timeout(800)
        _dismiss_layout_overlay(page)
        page.locator(locators.ORDER_BUY_BUTTON).first.click(timeout=ms)
        header = page.locator(locators.ORDER_FORM_HEADER_SYMBOL).first
        header.wait_for(state="visible", timeout=ms)
        opened_for = header.inner_text(timeout=2000).strip()
        if symbol in opened_for:
            break
        if prep_attempt == 1:
            logger.warning(
                "Ticket opened for %r, expected %r — closing and retrying selection.",
                opened_for, symbol,
            )
            page.locator('[data-cy="order-form-header-close-btn"]').first.click(timeout=5000)
            page.wait_for_timeout(500)
    else:
        raise RuntimeError(
            f"Order ticket opened for {opened_for!r}, expected {symbol!r} — aborting prep."
        )
    logger.info("Order ticket open for %s.", opened_for)
    dismiss_announcement(page)  # modals can pop over the form

    # ── 3. Quantity ──
    # The ticket loads async defaults (observed: quantity 10,000, price =
    # last trade) that can land AFTER our fill and clobber it. Wait for the
    # form to settle, then fill and verify — retrying if the server value
    # overwrote us (this really happens; the smoke test caught it).
    qty_input = page.locator(locators.QUANTITY_INPUT).first
    qty_input.wait_for(state="visible", timeout=ms)
    page.wait_for_timeout(700)
    final_qty = _fill_verified(page, qty_input, quantity)

    # ── 4. Price per mode ──
    price_input = page.locator(locators.PRICE_INPUT).first
    price_input.wait_for(state="visible", timeout=ms)

    if price_mode == "fixed":
        final_price = _fill_verified(page, price_input, price)
    elif price_mode == "ceiling":
        chip = page.locator(locators.MAX_PRICE_CHIP).first
        chip.click(timeout=ms)
        page.wait_for_timeout(300)
        final_price = _read_number(price_input.input_value(timeout=2000))
        if final_price <= 0:
            # Chip didn't populate the field — type the chip's own value
            chip_text = chip.inner_text(timeout=2000)
            chip_value = _read_number(chip_text)
            if chip_value <= 0:
                raise RuntimeError("Ceiling price chip shows no value — cannot price the order.")
            final_price = _fill_verified(page, price_input, chip_value)
    else:  # reference — previous close from the symbol header
        closing = page.locator(locators.CLOSING_PRICE).first
        closing.wait_for(state="visible", timeout=ms)
        reference_price = _read_number(closing.inner_text(timeout=2000))
        if reference_price <= 0:
            raise RuntimeError("Could not read closing/reference price from symbol header.")
        final_price = _fill_verified(page, price_input, reference_price)

    page.wait_for_timeout(300)  # let Angular format/validate

    # ── 5. Verify the ticket is actually ready ──
    final_qty = _read_number(qty_input.input_value(timeout=2000))
    final_price = _read_number(price_input.input_value(timeout=2000))
    if final_qty != quantity:
        raise RuntimeError(f"Quantity field shows {final_qty}, expected {quantity}.")
    if final_price <= 0:
        raise RuntimeError("Price field is empty after preparation.")

    summary = {
        "symbol": opened_for,
        "quantity": final_qty,
        "price": final_price,
        "price_mode": price_mode,
    }
    logger.info("Order prepared: %s", summary)
    return summary


def _read_number(text: str) -> int:
    """Parse '4,540' / '10,000' / '4540' → int (0 if unparseable)."""
    digits = "".join(ch for ch in str(text) if ch.isdigit())
    return int(digits) if digits else 0


def _fill_verified(page: Page, field, value: int, attempts: int = 3) -> int:
    """
    Fill a numeric field and confirm the value stuck.

    The order ticket is an Angular reactive form fed by async server
    defaults: a fill can be silently replaced by a value that arrives
    afterwards. Re-fill until the field reads back what we asked for.
    Returns the verified value; raises if it never sticks.
    """
    actual = 0
    for attempt in range(1, attempts + 1):
        field.fill(str(value))
        page.wait_for_timeout(400)
        actual = _read_number(field.input_value(timeout=2000))
        if actual == value:
            return actual
        logger.warning(
            "Field shows %s after fill %s (attempt %d/%d) — refilling.",
            actual, value, attempt, attempts,
        )
    raise RuntimeError(f"Could not set field to {value} (it reads {actual}).")


# ── Buy click ───────────────────────────────────────────
def buy_click_robust(page: Page) -> tuple[bool, str]:
    """
    Click the buy button and capture the broker's response.
    Returns (success, message). Never raises on transient DOM changes —
    Playwright's Locator re-resolves the node on each action.
    """
    button = find_buy_button(page)
    if button is None:
        logger.error("Buy button not found.")
        return False, "Buy button not found"

    try:
        button.click(timeout=3000)
        logger.info("Buy button clicked, waiting for broker response...")
    except PlaywrightTimeoutError as e:
        logger.warning("Buy click failed (%s), re-finding...", e)
        button = find_buy_button(page)
        if button is None:
            return False, "Buy button gone after failed click"
        button.click(timeout=3000)
        logger.info("Buy button clicked (after re-find), waiting for broker response...")

    # Longer budget than NOTIFY_TIMEOUT — the toast can be slow
    broker_message = capture_broker_notification(page, timeout=2)
    logger.info("Broker response: %r", broker_message)
    success = is_order_accepted(broker_message)

    if broker_message == "(no notification found)" and not success:
        logger.warning("No notification captured — will rely on order list verification.")

    return success, broker_message


# ── Verification ────────────────────────────────────────
def verify_order_in_list(page: Page) -> bool:
    """Check the orders list for at least one order row. True if confirmed."""
    if not VERIFY_ORDER:
        logger.debug("Order verification disabled.")
        return True

    try:
        # Bring the orders tab into view if it's a separate tab
        tab = page.locator(locators.ORDERS_TAB).first
        if tab.count() and tab.is_visible():
            tab.click(timeout=3000)

        page.locator(locators.ORDERS_ROW).first.wait_for(
            state="attached", timeout=VERIFY_ORDER_TIMEOUT * 1000
        )
        logger.info("Order verified in orders list.")
        return True
    except PlaywrightTimeoutError:
        logger.warning("Could NOT verify order in orders list — check manually.")
        return False


def execute_buy(page: Page) -> bool:
    """
    Rapid-click the buy button up to BUY_MAX_ATTEMPTS times.
    Returns True if the order was accepted.
    """
    logger.info(
        "Starting buy attempts (max=%d, interval=%.2fs)",
        BUY_MAX_ATTEMPTS,
        BUY_RETRY_INTERVAL,
    )

    for attempt in range(1, BUY_MAX_ATTEMPTS + 1):
        success, msg = buy_click_robust(page)
        logger.info("Attempt %d/%d: %s", attempt, BUY_MAX_ATTEMPTS, msg)

        if success:
            logger.info("✅ Order accepted by broker (attempt %d).", attempt)
            if verify_order_in_list(page):
                logger.info("✅ Order verified in orders list.")
            else:
                logger.warning("⚠️ Order accepted but NOT verified in list — check manually.")
            return True

        if attempt < BUY_MAX_ATTEMPTS:
            time.sleep(BUY_RETRY_INTERVAL)

    logger.error("❌ Order not accepted after %d attempts.", BUY_MAX_ATTEMPTS)
    return False
