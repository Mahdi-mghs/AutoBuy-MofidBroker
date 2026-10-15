"""
Browser setup (Playwright) with guaranteed cleanup.

Uses the installed Google Chrome via Playwright's channel support —
no chromedriver, no webdriver-manager, no version hunting.
"""

import atexit
import logging

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    sync_playwright,
)

from config import CHROME_USER_DATA_DIR, HEADLESS, INSTANCE_ID

logger = logging.getLogger(__name__)

# The site shows a <size-warning> overlay below the lg breakpoint (1024px)
# that intercepts ALL clicks — keep the viewport comfortably above it.
VIEWPORT = {"width": 1600, "height": 1000}

# Track active sessions so we can clean them up on exit.
# Each entry is a dict with a `done` flag so cleanup is IDEMPOTENT:
# both cleanup_all() (explicit) and atexit may request the same session.
_active_sessions: list[dict] = []


def initialize_driver() -> Page:
    """
    Launch Playwright + Chrome and return the main Page.

    - If CHROME_USER_DATA_DIR is set, uses a persistent context so each
      multi-instance run keeps its own login/profile.
    - Otherwise uses a throwaway profile (fresh, isolated session).
    - Registered for automatic cleanup on process exit.
    """
    pw = sync_playwright().start()

    ignore_https_errors = True  # mirrors old --ignore-certificate-errors

    if CHROME_USER_DATA_DIR:
        logger.info("Using Chrome user data dir: %s", CHROME_USER_DATA_DIR)
        context = pw.chromium.launch_persistent_context(
            CHROME_USER_DATA_DIR,
            channel="chrome",
            headless=HEADLESS,
            ignore_https_errors=ignore_https_errors,
            viewport=VIEWPORT,
        )
        browser = None
    else:
        browser = pw.chromium.launch(
            channel="chrome",
            headless=HEADLESS,
        )
        context = browser.new_context(
            ignore_https_errors=ignore_https_errors, viewport=VIEWPORT
        )

    # Prefer an existing tab (persistent context may restore tabs)
    pages = context.pages
    page = pages[0] if pages else context.new_page()
    if not HEADLESS:
        try:
            page.bring_to_front()
        except Exception:  # pragma: no cover - best effort
            pass

    session = {"pw": pw, "browser": browser, "context": context, "done": False}
    _active_sessions.append(session)
    atexit.register(_cleanup_session, session)

    instance_info = f" (instance: {INSTANCE_ID})" if INSTANCE_ID else ""
    logger.info("Browser initialized%s and cleanup registered.", instance_info)
    return page


def _cleanup_session(session: dict):
    """Safely close one Playwright session. Idempotent — safe to call twice
    (explicit cleanup_all() followed by the atexit hook)."""
    if session.get("done"):
        return
    session["done"] = True
    if session in _active_sessions:
        _active_sessions.remove(session)

    pw: Playwright = session["pw"]
    browser: Browser | None = session["browser"]
    context: BrowserContext = session["context"]

    try:
        context.close()
        logger.info("Browser context closed cleanly.")
    except Exception as e:
        logger.warning("Error closing context: %s", e)
    if browser is not None:
        try:
            browser.close()
        except Exception as e:
            logger.warning("Error closing browser: %s", e)
    try:
        pw.stop()
        logger.info("Playwright stopped.")
    except Exception as e:
        logger.warning("Error stopping Playwright: %s", e)


def cleanup_all():
    """Force-close all tracked sessions. Call this for immediate cleanup."""
    for session in list(_active_sessions):
        _cleanup_session(session)
    _active_sessions.clear()
