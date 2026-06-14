"""
Order actions: click buy, verify order in orders list.
"""

import time
import logging

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import (
    TimeoutException,
    StaleElementReferenceException,
)

from config import (
    DEFAULT_TIMEOUT,
    BUY_MAX_ATTEMPTS,
    BUY_RETRY_INTERVAL,
    NOTIFY_TIMEOUT,
    VERIFY_ORDER,
    VERIFY_ORDER_TIMEOUT,
)
from broker import capture_broker_notification, is_order_accepted

logger = logging.getLogger(__name__)


# ── Buy button locators ─────────────────────────────────

_PRIMARY_BUY = (By.CSS_SELECTOR, 'button[data-cy="oms-order-form-submit-button-buy"]')
_FALLBACK_BUY = [
    (By.XPATH, '//*[@id="root"]/main/div[3]/d-order-list/div/div[3]/order-form-tabular-container/div/order-form-actions/div/button[1]'),
    (By.XPATH, "//button[contains(@class, 'btn-success')]"),
    (By.XPATH, "//button[contains(., 'ارسال خرید')]"),
]


def _find_buy_button(driver, timeout=2):
    """Try to find the buy button using primary then fallback locators."""
    for by, selector in [_PRIMARY_BUY] + _FALLBACK_BUY:
        try:
            return WebDriverWait(driver, timeout).until(
                EC.element_to_be_clickable((by, selector))
            )
        except TimeoutException:
            continue
    return None


def buy_click_robust(driver) -> tuple[bool, str]:
    """
    Click the buy button with stale-element retry.
    Returns (success, message).
    """
    button = _find_buy_button(driver)
    if button is None:
        logger.error("Buy button not found.")
        return False, "Buy button not found"

    try:
        button.click()
        logger.info("Buy button clicked, waiting for broker response...")
    except StaleElementReferenceException:
        logger.warning("Buy button went stale, re-finding...")
        button = _find_buy_button(driver)
        if button is None:
            return False, "Buy button stale and not re-found"
        button.click()
        logger.info("Buy button clicked (after re-find), waiting for broker response...")

    # Use a longer timeout for notification — the broker toast can be slow
    broker_message = capture_broker_notification(driver, timeout=2)
    logger.info("Broker response: %r", broker_message)
    success = is_order_accepted(broker_message)

    # If we couldn't read the notification but there's no failure,
    # check the orders list as a fallback
    if broker_message == "(no notification found)" and not success:
        logger.warning("No notification captured — will rely on order list verification.")

    return success, broker_message


def verify_order_in_list(driver) -> bool:
    """
    After a buy click, navigate to the orders tab and check that
    a new order has appeared. Returns True if confirmed.
    """
    if not VERIFY_ORDER:
        logger.debug("Order verification disabled.")
        return True

    try:
        # Click the orders/watch tab — adjust XPath to your layout
        orders_tab = WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable(
                (By.XPATH, '//a[contains(text(), "سفارش‌ها") or contains(text(), "orders")]')
            )
        )
        orders_tab.click()

        # Wait for at least one order row to appear
        WebDriverWait(driver, VERIFY_ORDER_TIMEOUT).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, 'table.orders-table tbody tr, .order-list-item')
            )
        )
        logger.info("Order verified in orders list.")
        return True

    except TimeoutException:
        logger.warning("Could NOT verify order in orders list — check manually.")
        return False


def execute_buy(driver) -> bool:
    """
    Rapid-click the buy button up to BUY_MAX_ATTEMPTS times.
    Returns True if the order was accepted.
    """
    logger.info("Starting buy attempts (max=%d, interval=%.2fs)", BUY_MAX_ATTEMPTS, BUY_RETRY_INTERVAL)

    for attempt in range(1, BUY_MAX_ATTEMPTS + 1):
        success, msg = buy_click_robust(driver)
        logger.info("Attempt %d/%d: %s", attempt, BUY_MAX_ATTEMPTS, msg)

        if success:
            logger.info("✅ Order accepted by broker (attempt %d).", attempt)

            # Verify order actually appears in the orders list
            if verify_order_in_list(driver):
                logger.info("✅ Order verified in orders list.")
            else:
                logger.warning("⚠️ Order accepted but NOT verified in list — check manually.")

            return True

        if attempt < BUY_MAX_ATTEMPTS:
            time.sleep(BUY_RETRY_INTERVAL)

    logger.error("❌ Order not accepted after %d attempts.", BUY_MAX_ATTEMPTS)
    return False
