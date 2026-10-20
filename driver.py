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


class BrowserGone(RuntimeError):
    """The Playwright session is unusable — the browser or the driver is gone.

    Raised instead of retrying: no page call can succeed after this.
    """


# Messages Playwright uses once the page/context/browser/connection has died.
# Lowercased substrings, matched against the whole cause chain.
_GONE_MARKERS = (
    "target page, context or browser has been closed",
    "browser has been closed",
    "playwright connection closed",
    "connection closed while reading from the driver",
    "event loop is closed",
    "target closed",
)


def is_browser_gone(exc: BaseException | None) -> bool:
    """True when `exc` (or anything in its cause chain) means the page is dead.

    This is the Ctrl+C case on Windows: the console event also reaches the
    Playwright driver process, which exits, so every in-flight call fails with
    TargetClosedError / "Playwright connection closed". Retrying such an error
    (a clock poll loop, an order burst) just floods the terminal — callers must
    stop instead. It also covers a crashed Chrome and a stopped Playwright.
    """
    seen = 0
    while exc is not None and seen < 10:
        if isinstance(exc, BrowserGone):
            return True
        text = str(exc).lower()
        if any(marker in text for marker in _GONE_MARKERS):
            return True
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return False


def require_live_browser(exc: BaseException) -> None:
    """Re-raise `exc` as BrowserGone when the session is dead; otherwise return.

    Call this first in a broad `except Exception` so a dead browser is never
    swallowed into a retry/fallback path.
    """
    if is_browser_gone(exc):
        raise BrowserGone(
            "the Playwright browser/driver is gone (Ctrl+C or a crashed browser)"
        ) from exc


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
        # After Ctrl+C the driver process is already gone: closing what is
        # left cannot work and is not an error worth a stack trace.
        if is_browser_gone(e):
            logger.info("Browser was already closed (driver exited after Ctrl+C).")
        else:
            logger.warning("Error closing context: %s", e)
    if browser is not None:
        try:
            browser.close()
        except Exception as e:
            if is_browser_gone(e):
                logger.debug("Browser already closed: %s", e)
            else:
                logger.warning("Error closing browser: %s", e)
    try:
        pw.stop()
        logger.info("Playwright stopped.")
    except Exception as e:
        if is_browser_gone(e):
            logger.debug("Playwright already stopped: %s", e)
        else:
            logger.warning("Error stopping Playwright: %s", e)


def cleanup_all():
    """Force-close all tracked sessions. Call this for immediate cleanup."""
    for session in list(_active_sessions):
        _cleanup_session(session)
    _active_sessions.clear()
