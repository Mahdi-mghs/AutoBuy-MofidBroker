"""
Central registry of every selector the bot uses.

All selectors were verified against the live EasyTrader (d.easytrader.ir)
DOM during the Playwright rewrite (see docs/adr/002-automate-order-preparation.md).
Playwright locator strings: CSS by default, Playwright text engine where
the Persian label is the only stable handle.

IMPORTANT quirk: on this site `data-cy` attributes live on the custom
elements (<ui-button>, <input-widget>, <div>), NOT always on the native
element. e.g. the buy button is:
    <ui-button data-cy="oms-order-form-submit-button-buy">
        <button aria-label="ارسال خرید">ارسال خرید</button>
    </ui-button>
so the locators below target the wrapper and descend to the native node.
"""

# ── Login page ──────────────────────────────────────────
LOGIN_USER_INPUT = "#user-name"
LOGIN_PASS_INPUT = "#password"
LOGIN_SUBMIT_BUTTON = "#primary_form button"
# Post-login marker: the main menu (دیده‌بان watchlist) appears
LOGIN_SUCCESS_MARKER = 'li[data-cy="mw-menu-icon"]'

# ── Announcement modal (shown optionally after login) ───
ANNOUNCEMENT_CANCEL = "#cancel-action-confirm-btn"

# ── Broker clock ────────────────────────────────────────
CLOCK = "#easy-clock-id"

# ── Symbol search / navigation ──────────────────────────
# Sidebar search tab (legacy XPath li[2] is gone; data-cy is stable)
SEARCH_MENU = 'li[data-cy="search-menu-icon"]'
SEARCH_INPUT = 'input[data-cy="layout-search-input"]'  # placeholder: جستجوی نماد


def search_item(symbol: str) -> str:
    """Exact search result for a symbol, e.g. [data-cy=\"search-item-name-فولاد\"]."""
    return f'[data-cy="search-item-name-{symbol}"]'


# ── Order form ──────────────────────────────────────────
# Opens the buy-side order ticket for the currently selected symbol
ORDER_BUY_BUTTON = '[data-cy="order-buy-btn"]'
ORDER_SELL_BUTTON = '[data-cy="order-sell-btn"]'
# Header of the open order ticket — carries the symbol name (assert target)
ORDER_FORM_HEADER_SYMBOL = '[data-cy="order-form-header-symbol-name"]'
# Final submit (the button the bot rapid-clicks at target time)
ORDER_FORM_BUY = '[data-cy="oms-order-form-submit-button-buy"] button'
# Fallbacks for the buy button
ORDER_FORM_BUY_FALLBACKS = [
    'button[aria-label="ارسال خرید"]',
    "order-form-actions button >> nth=0",
    "//button[contains(., 'ارسال خرید')]",
]

# Price / quantity fields (comma-formatted text inputs, dir=ltr)
PRICE_INPUT = 'input[data-cy="order-form-input-price"]'   # also id="price"
QUANTITY_INPUT = 'input[data-cy="order-form-input-quantity"]'  # also id="quantity"

# Value chips next to the fields — clicking one fills the field:
#   max-price chip = سقف قیمت (ceiling), min-price = کف قیمت (floor)
MAX_PRICE_CHIP = '[data-cy="order-form-max-price"]'
MIN_PRICE_CHIP = '[data-cy="order-form-min-price"]'
MAX_QUANTITY_CHIP = '[data-cy="order-form-max-quantity"]'
MIN_QUANTITY_CHIP = '[data-cy="order-form-min-quantity"]'

# Previous close / closing price shown in the symbol header
# (used by PRICE_MODE=reference as the قیمت مرجع stand-in)
CLOSING_PRICE = '[data-cy="symbol-header-closing-price"]'

# ── Broker toast / notification ─────────────────────────
# Priority order: app-notify root first (the actual toast host)
NOTIFICATION_SELECTORS = [
    "/html/body/app-root/app-notify/div/div",
    "app-notify div",
    ".notify-message",
    "div.toast",
    "div.notification",
    "div.snackbar",
    "div.alert",
    "span.message",
]

# Keywords the DOM keyword-scan looks for (Persian + English)
SUCCESS_KEYWORDS = ["موفق", "ثبت شد", "پذیرفته", "accept", "success", "submitted"]
FAILURE_KEYWORDS = ["خطا", "reject", "ناموفق", "عدم پذیرش", "failed"]

# ── Orders tab / list ───────────────────────────────────
ORDERS_TAB = '[data-cy="symbol-detail-tab-orders"], a:has-text("سفارشات"), a:has-text("سفارش‌ها")'
ORDERS_LIST = 'd-order-list[data-cy="order-list-container"]'
ORDERS_ROW = 'd-order-list[data-cy="order-list-container"] .ag-row'
ORDERS_EMPTY = '[data-cy="empty-order-list-container"]'
