"""
Broker interactions: login, clock, notifications, market status.
Playwright port — all waiting is done through Locator auto-waiting.
"""

import logging
import time
from datetime import datetime, time as dt_time, timedelta

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

import locators
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

# Latest time of day a buy is considered valid (market closes 15:00 with
# closing auction; anything after is stale/after-hours)
MARKET_LATEST = dt_time(15, 30, 0)


# ── Login ───────────────────────────────────────────────
def login(page: Page) -> bool:
    """Login with retry mechanism. Returns True on success."""
    if not USERNAME or not PASSWORD:
        logger.error("Credentials not set. Define MOFID_USER and MOFID_PASS env vars.")
        return False

    for attempt in range(1, LOGIN_MAX_ATTEMPTS + 1):
        logger.info("Login attempt %d/%d", attempt, LOGIN_MAX_ATTEMPTS)
        page.goto(BROKER_URL, wait_until="domcontentloaded")

        try:
            page.locator(locators.LOGIN_USER_INPUT).fill(USERNAME, timeout=LOGIN_TIMEOUT * 1000)
            page.locator(locators.LOGIN_PASS_INPUT).fill(PASSWORD, timeout=LOGIN_TIMEOUT * 1000)
            page.locator(locators.LOGIN_SUBMIT_BUTTON).first.click(timeout=LOGIN_TIMEOUT * 1000)
            # Main menu appears only after a successful login
            page.locator(locators.LOGIN_SUCCESS_MARKER).first.wait_for(
                state="visible", timeout=LOGIN_TIMEOUT * 1000
            )
            logger.info("Login successful.")
            return True
        except PlaywrightTimeoutError:
            logger.warning("Login attempt %d failed, retrying...", attempt)
            time.sleep(1.0)

    logger.error("Login failed after %d attempts.", LOGIN_MAX_ATTEMPTS)
    return False


# ── Modals ──────────────────────────────────────────────
def dismiss_announcement(page: Page):
    """Close announcement modal if present. Fast no-op when absent."""
    try:
        el = page.locator(locators.ANNOUNCEMENT_CANCEL).first
        if el.count() == 0:
            logger.debug("No announcement modal.")
            return
        el.click(timeout=3000)
        logger.info("Announcement closed.")
    except PlaywrightTimeoutError:
        logger.debug("No announcement modal.")


# ── Broker Clock ────────────────────────────────────────
def get_broker_time(page: Page) -> dt_time:
    """Read broker clock text (HH:MM:SS) and return a time object."""
    text = page.locator(locators.CLOCK).first.inner_text(timeout=5000).strip()
    parts = text.split(":")
    if len(parts) == 3:
        return dt_time(int(parts[0]), int(parts[1]), int(parts[2]))
    raise ValueError(f"Unexpected broker time format: {text!r}")


def wait_until_broker_time(page: Page, target: dt_time, now_fn=None) -> dt_time:
    """
    Wait until the broker clock shows >= target time (and <= 15:30:00).

    `now_fn` defaults to reading the on-page clock; tests inject a fake
    clock. Raises TimeoutError if the target isn't reached within
    CLOCK_DEADLINE_SECONDS (default 1h) — guards against a closed market
    or a vanished clock element.
    """
    if now_fn is None:
        now_fn = lambda: get_broker_time(page)

    deadline = datetime.now() + timedelta(seconds=CLOCK_DEADLINE_SECONDS)
    logger.info(
        "Waiting for broker clock >= %s (deadline: %s)",
        target.strftime("%H:%M:%S"),
        deadline.strftime("%H:%M:%S"),
    )

    # Verify the clock is readable up front
    try:
        now_fn()
    except Exception as e:
        raise TimeoutError(f"Cannot find broker clock element: {e}") from e

    while True:
        if datetime.now() > deadline:
            raise TimeoutError(
                f"Broker time {target} not reached within "
                f"{CLOCK_DEADLINE_SECONDS}s deadline. "
                "Market may be closed or clock element is stale."
            )

        try:
            now = now_fn()
        except Exception as e:
            # Transient read failure (stale element, repaint) — retry
            logger.warning("Clock read failed (%s), retrying...", e)
            time.sleep(CLOCK_POLL_INTERVAL)
            continue

        if now >= target and now <= MARKET_LATEST:
            logger.info("Target time reached: %s", now.strftime("%H:%M:%S"))
            return now

        time.sleep(CLOCK_POLL_INTERVAL)


# ── Notifications ───────────────────────────────────────
# DOM keyword scan used when no toast selector matches (runs in-page)
_DOM_SCAN_JS = """
(kw) => {
    const all = document.querySelectorAll('*');
    for (const el of all) {
        if (el.children.length > 3) continue;
        const text = el.textContent.trim();
        if (!text || text.length > 200 || text.length < 2) continue;
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden') continue;
        for (const k of kw) {
            if (text.includes(k)) return {text: text, tag: el.tagName, cls: el.className};
        }
    }
    return null;
}
"""


def is_order_accepted(broker_message: str) -> bool:
    """
    Determine if the broker message indicates success.

    FAILURE keywords are checked first, deliberately: Persian negation is a
    prefix, so "ناموفق" (unsuccessful) *contains* "موفق" (successful). A
    success-first check reported rejected orders as accepted and the bot
    stopped retrying — the legacy implementation had exactly this bug.
    The bot only claims success on a clean success signal.
    """
    msg = broker_message.lower()
    if "(no notification found)" in msg:
        return False
    if any(kw in msg for kw in locators.FAILURE_KEYWORDS):
        return False
    if any(kw in msg for kw in locators.SUCCESS_KEYWORDS):
        return True
    logger.info("Unclassified broker message: %r", broker_message)
    return False


def capture_broker_notification(page: Page, timeout=NOTIFY_TIMEOUT) -> str:
    """
    Capture the broker's response toast:
    1. Poll known toast selectors for up to `timeout` seconds total.
    2. Fall back to an in-page keyword scan.
    Returns the matched text or '(no notification found)'.
    """
    selectors = locators.NOTIFICATION_SELECTORS
    end = time.time() + timeout

    # Strategy 1: known selectors (one shared deadline across all)
    while time.time() < end:
        for sel in selectors:
            try:
                if sel.startswith("/") or sel.startswith("//"):
                    loc = page.locator(f"xpath={sel}")
                else:
                    loc = page.locator(sel)
                el = loc.first
                if el.count() and el.is_visible():
                    text = el.inner_text(timeout=200).strip()
                    if text:
                        logger.debug("Notification via selector %s: %r", sel, text)
                        return text
            except Exception:
                continue
        time.sleep(0.1)

    # Strategy 2: in-page scan for any visible element with keywords
    try:
        time.sleep(0.2)  # let the toast render
        result = page.evaluate(_DOM_SCAN_JS, locators.SUCCESS_KEYWORDS + locators.FAILURE_KEYWORDS)
        if result and result.get("text"):
            logger.debug("Notification via DOM scan: %r", result["text"])
            return result["text"]
    except Exception as e:
        logger.debug("DOM scan for notification failed: %s", e)

    return "(no notification found)"
