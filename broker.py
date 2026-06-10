"""
Broker interactions: login, clock, notifications, market status.
"""

import time
import logging
from datetime import datetime, time as dt_time, timedelta

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException, StaleElementReferenceException

from config import (
    BROKER_URL,
    USERNAME,
    PASSWORD,
    DEFAULT_TIMEOUT,
    LOGIN_TIMEOUT,
    LOGIN_MAX_ATTEMPTS,
    NOTIFY_TIMEOUT,
    CLOCK_POLL_INTERVAL,
    CLOCK_DEADLINE_SECONDS,
)

logger = logging.getLogger(__name__)


# ── Helpers ─────────────────────────────────────────────

def _wait(driver, xpath, condition, timeout=DEFAULT_TIMEOUT):
    """Wait for an element located by XPATH to satisfy a condition."""
    return WebDriverWait(driver, timeout).until(condition((By.XPATH, xpath)))


def _click(driver, xpath, timeout=DEFAULT_TIMEOUT):
    """Wait for element to be clickable and click it."""
    el = _wait(driver, xpath, EC.element_to_be_clickable, timeout)
    el.click()
    return el


def _type(driver, xpath, text, timeout=DEFAULT_TIMEOUT):
    """Wait for element to be visible and type text."""
    el = _wait(driver, xpath, EC.visibility_of_element_located, timeout)
    el.send_keys(text)
    return el


# ── Login ───────────────────────────────────────────────

def login(driver) -> bool:
    """Login with retry mechanism. Returns True on success."""
    if not USERNAME or not PASSWORD:
        logger.error("Credentials not set. Define MOFID_USER and MOFID_PASSWORD env vars.")
        return False

    for attempt in range(1, LOGIN_MAX_ATTEMPTS + 1):
        logger.info("Login attempt %d/%d", attempt, LOGIN_MAX_ATTEMPTS)
        driver.get(BROKER_URL)

        _type(driver, '//*[@id="user-name"]', USERNAME, timeout=LOGIN_TIMEOUT)
        _type(driver, '//*[@id="password"]', PASSWORD, timeout=LOGIN_TIMEOUT)
        _click(driver, '//*[@id="primary_form"]/div[3]/button', timeout=LOGIN_TIMEOUT)

        try:
            _wait(
                driver,
                '//*[@id="root"]/main/div[2]/div[1]/ul[1]/li[2]',
                EC.element_to_be_clickable,
                timeout=LOGIN_TIMEOUT,
            )
            logger.info("Login successful.")
            return True
        except TimeoutException:
            logger.warning("Login attempt %d failed, retrying...", attempt)
            time.sleep(1.0)

    logger.error("Login failed after %d attempts.", LOGIN_MAX_ATTEMPTS)
    return False


# ── Modals ──────────────────────────────────────────────

def dismiss_announcement(driver):
    """Close announcement modal if present."""
    try:
        el = _wait(
            driver,
            '//*[@id="cancel-action-confirm-btn"]',
            EC.element_to_be_clickable,
            timeout=5,
        )
        el.click()
        logger.info("Announcement closed.")
    except TimeoutException:
        logger.debug("No announcement modal.")


# ── Broker Clock ────────────────────────────────────────

def get_broker_time(driver, clock_element=None):
    """Read broker clock text (HH:MM:SS) and return a time object."""
    if clock_element is None:
        clock_element = WebDriverWait(driver, 5).until(
            EC.visibility_of_element_located((By.ID, "easy-clock-id"))
        )
    time_str = clock_element.text.strip()
    parts = time_str.split(":")
    if len(parts) == 3:
        return dt_time(int(parts[0]), int(parts[1]), int(parts[2]))
    raise ValueError(f"Unexpected broker time format: {time_str}")


def wait_until_broker_time(driver, target: dt_time):
    """
    Wait until the broker clock shows >= target time.
    Includes a safety deadline — raises TimeoutError if the target
    isn't reached within CLOCK_DEADLINE_SECONDS (default 1 hour).
    This prevents infinite loops if the market is closed or the
    clock element disappears.
    """
    clock_elem = WebDriverWait(driver, 10).until(
        EC.visibility_of_element_located((By.ID, "easy-clock-id"))
    )

    deadline = datetime.now() + timedelta(seconds=CLOCK_DEADLINE_SECONDS)
    logger.info(
        "Waiting for broker clock >= %s (deadline: %s)",
        target.strftime("%H:%M:%S"),
        deadline.strftime("%H:%M:%S"),
    )

    while True:
        if datetime.now() > deadline:
            raise TimeoutError(
                f"Broker time {target} not reached within "
                f"{CLOCK_DEADLINE_SECONDS}s deadline. "
                "Market may be closed or clock element is stale."
            )

        try:
            now = get_broker_time(driver, clock_element=clock_elem)
        except (TimeoutException, StaleElementReferenceException):
            # Clock element disappeared — re-find it
            logger.warning("Clock element lost, re-finding...")
            try:
                clock_elem = WebDriverWait(driver, 5).until(
                    EC.visibility_of_element_located((By.ID, "easy-clock-id"))
                )
            except TimeoutException:
                raise TimeoutError("Cannot find broker clock element. Page may have changed.")
            continue

        if now >= target and now <= dt_time(15, 30, 0):
            logger.info("Target time reached: %s", now.strftime("%H:%M:%S"))
            break

        time.sleep(CLOCK_POLL_INTERVAL)


# ── Notifications ───────────────────────────────────────

# ── Success keywords (Persian + English) ─────────────────

_SUCCESS_KEYWORDS = [
    "موفق",       # successful
    "ثبت شد",      # registered/submitted
    "پذیرفته",     # accepted
    "accept",
    "success",
    "submitted",
]

_FAILURE_KEYWORDS = [
    "خطا",         # error
    "reject",
    "ناموفق",      # failed
    "عدم پذیرش",   # not accepted
    "failed",
]


def is_order_accepted(broker_message: str) -> bool:
    """Determine if the broker message indicates success."""
    msg = broker_message.lower()
    if any(kw in msg for kw in _SUCCESS_KEYWORDS):
        return True
    # If we got no clear signal but also no failure keywords,
    # treat it as likely accepted (the broker often shows a brief
    # toast that disappears faster than our timeout)
    if "(no notification found)" in msg:
        return False
    if any(kw in msg for kw in _FAILURE_KEYWORDS):
        return False
    # Unknown message — could be a success toast we caught partially
    logger.info("Unclassified broker message: %r", broker_message)
    return False


def capture_broker_notification(driver, timeout=NOTIFY_TIMEOUT) -> str:
    """
    Try multiple strategies to capture the broker's response toast:
    1. Known CSS selectors (fast path)
    2. Any element containing success/failure keywords (broader search)
    Returns the matched text or '(no notification found)'.
    """
    # Strategy 1: known selectors
    notif_selectors = [
        (By.XPATH, "/html/body/app-root/app-notify/div/div"),
        (By.CLASS_NAME, "notify-message"),
        (By.XPATH, "//div[contains(@class, 'toast')]"),
        (By.XPATH, "//div[contains(@class, 'notification')]"),
        (By.XPATH, "//div[contains(@class, 'snackbar')]"),
        (By.XPATH, "//div[contains(@class, 'alert')]"),
        (By.XPATH, "//span[contains(@class, 'message')]"),
    ]
    for by, selector in notif_selectors:
        try:
            elem = WebDriverWait(driver, timeout).until(
                EC.visibility_of_element_located((by, selector))
            )
            text = elem.text.strip()
            if text:
                logger.debug("Notification via selector %s: %r", selector, text)
                return text
        except TimeoutException:
            continue

    # Strategy 2: scan the DOM for any visible element containing keywords
    try:
        import time as _time
        snapshot_time = _time.time()
        # Give the DOM a brief moment to render the toast
        _time.sleep(0.2)

        result = driver.execute_script("""
            const keywords = ['موفق', 'ثبت شد', 'پذیرفته', 'accept', 'success',
                              'خطا', 'reject', 'ناموفق', 'عدم پذیرش', 'failed'];
            const all = document.querySelectorAll('*');
            for (const el of all) {
                // Only leaf-ish elements with visible text
                if (el.children.length > 3) continue;
                const text = el.textContent.trim();
                if (!text || text.length > 200 || text.length < 2) continue;
                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden') continue;
                for (const kw of keywords) {
                    if (text.includes(kw)) {
                        return {text: text, tag: el.tagName, cls: el.className};
                    }
                }
            }
            return null;
        """)
        if result and result.get("text"):
            logger.debug("Notification via DOM scan: %r", result["text"])
            return result["text"]
    except Exception as e:
        logger.debug("DOM scan for notification failed: %s", e)

    return "(no notification found)"
