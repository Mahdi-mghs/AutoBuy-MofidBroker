"""
Main entry point — CLI orchestration for AutoBuy-MofidBroker (Playwright).

Usage:
    python main.py --time 08:44:58
    python main.py --time 09:00:00 --verify-order
    python main.py --time 08:44:58 --no-verify
    python main.py --symbol فولاد --time 08:59:59
    python main.py --manual          # old behavior: prepare order by hand
"""

import argparse
import logging
import signal
import sys
import time
from datetime import time as dt_time

from playwright.sync_api import Page

import locators
from config import (
    LOG_LEVEL,
    LOG_FILE,
    USERNAME,
    PASSWORD,
    DEFAULT_BUY_TIME,
    INSTANCE_ID,
    SYMBOL,
    QUANTITY,
    PRICE_MODE,
    PRICE,
    API_MODE,
    API_BASE_URL,
    VERIFY_ORDER,
    BROKER_URL,
    TOKEN_CACHE,
    TOKEN_CACHE_PATH,
    TOKEN_CACHE_MIN_VALIDITY,
)
from driver import BrowserGone, initialize_driver, cleanup_all, is_browser_gone, require_live_browser
from broker import login, dismiss_announcement, wait_until_broker_time, get_broker_time
from order import execute_buy, prepare_order, find_buy_button
from easy_api import (
    ApiAuthError,
    EasyApiClient,
    PlaywrightTransport,
    extract_auth_token,
    EasyApiError,
)
import api_flow
import token_store

# Flag set by signal handler — checked in the main loop
_shutdown_requested = False


def _signal_handler(signum, frame):
    """Handle Ctrl+C — set flag so main loop can exit cleanly."""
    global _shutdown_requested
    _shutdown_requested = True
    # Don't call cleanup_all() here — we're not in the main thread context
    # The finally block in main() will handle it.


def setup_logging():
    """Configure logging to both file and console."""
    # Windows consoles default to cp1252 — Persian symbol names in log
    # lines must not crash the bot (reconfigure to UTF-8, never fail).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    log_fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    date_fmt = "%Y-%m-%d %H:%M:%S"

    handlers = [
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]

    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
        format=log_fmt,
        datefmt=date_fmt,
        handlers=handlers,
    )


def parse_time_arg(value: str) -> dt_time:
    """Parse HH:MM:SS or HH:MM from CLI."""
    parts = value.split(":")
    if len(parts) == 2:
        return dt_time(int(parts[0]), int(parts[1]), 0)
    if len(parts) == 3:
        return dt_time(int(parts[0]), int(parts[1]), int(parts[2]))
    raise argparse.ArgumentTypeError(f"Invalid time format: {value}. Use HH:MM or HH:MM:SS")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="AutoBuy — Mofid Securities automated buy bot (Playwright)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py --time 08:44:58
  python main.py --time 08:59:59 --symbol فولاد
  python main.py --time 08:44:58 --no-verify
  python main.py --manual
        """,
    )
    parser.add_argument(
        "--time", "-t",
        type=parse_time_arg,
        required=False,
        help="Target broker clock time to click buy (HH:MM or HH:MM:SS). "
             "Default: from DEFAULT_BUY_TIME env var (08:44:58)",
    )
    parser.add_argument(
        "--symbol", "-s",
        default=None,
        help="Symbol to prepare automatically (overrides SYMBOL env var)",
    )
    parser.add_argument(
        "--manual",
        action="store_true",
        default=False,
        help="Skip automated preparation; pause so you can fill the order by hand (legacy behavior)",
    )
    parser.add_argument(
        "--no-api",
        action="store_true",
        default=False,
        help="Disable direct-API mode for this run (forces the UI path even if API_MODE=true)",
    )
    parser.add_argument(
        "--no-token-cache",
        action="store_true",
        default=False,
        help="Ignore and do not save the cached API token (always log in via Playwright)",
    )
    parser.add_argument(
        "--verify-order",
        action="store_true",
        default=True,
        help="Verify order appears in orders list after buy (default: on)",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        default=False,
        help="Skip order verification",
    )
    return parser.parse_args(argv)


def is_time_already_passed(page: Page, target: dt_time, logger, now_fn=None) -> bool:
    """Check if the broker clock has already passed the target time.

    `now_fn` lets a UI-less (cached-token) run use the API clock instead of
    the on-page clock.
    """
    try:
        now = now_fn() if now_fn is not None else get_broker_time(page)
        if now >= target:
            logger.warning(
                "Broker clock already at %s — target %s has passed!",
                now.strftime("%H:%M:%S"),
                target.strftime("%H:%M:%S"),
            )
            return True
    except Exception as e:
        logger.warning("Could not read broker clock for pre-check: %s", e)
    return False


def _shutdown_now(logger, instance_info: str, reason: str, buy_started: bool = False) -> None:
    """Exit cleanly after Ctrl+C or a dead browser: one line, no traceback.

    Ctrl+C also kills the Playwright driver, so every page call fails from that
    moment on. There is nothing left to retry — say so once and stop.
    """
    if _shutdown_requested:
        logger.info("Interrupted by user (Ctrl+C)%s — stopping.", instance_info)
        code = 0
    else:
        logger.error(
            "Browser/Playwright session lost%s (%s) — stopping.", instance_info, reason
        )
        code = 1
    if buy_started:
        logger.warning(
            "The buy burst had already started — check the orders tab manually; "
            "nothing further will be sent%s.",
            instance_info,
        )
    sys.exit(code)


def _try_prepare_api(page: Page, symbol: str, logger, save_token: bool = True):
    """
    API-mode order preparation after a fresh login. Returns the prepared dict
    (with a 'client' key for execute_buy_api) or None to signal 'fall back to
    the UI path'.

    The harvested JWT is cached ONLY after preparation succeeded — the token
    has then proven itself against the live API, so a bad token never
    poisons the next run.
    """
    try:
        token = extract_auth_token(page, timeout=10)
        logger.info("API token harvested (len=%d).", len(token))
        client = EasyApiClient(token, PlaywrightTransport(page.context.request), base_url=API_BASE_URL)
        prepared = api_flow.prepare_via_api(
            client,
            symbol=symbol,
            quantity=QUANTITY,
            price_mode=PRICE_MODE,
            price=PRICE,
        )
        prepared["client"] = client
        # Broker clock for the order-window logic in execute_buy_api (the burst
        # wait at 08:45 is driven by it). One cheap /server-time call.
        try:
            prepared["clock"] = api_flow.ApiClock(client).sync()
        except Exception as e:
            logger.debug("API clock unavailable after login: %s", e)
        if save_token and TOKEN_CACHE:
            try:
                token_store.save_token(
                    token,
                    TOKEN_CACHE_PATH,
                    account_hint=token_store.account_fingerprint(USERNAME),
                )
                logger.info("API token cached for the next run (%s).", TOKEN_CACHE_PATH)
            except Exception as e:
                logger.warning("Could not cache the API token: %s", e)
        return prepared
    except EasyApiError as e:
        logger.warning("API preparation failed: %s", e)
        return None
    except api_flow.ApiFlowError as e:
        logger.warning("API preparation rejected: %s", e)
        return None
    except Exception as e:
        require_live_browser(e)  # Ctrl+C: the UI fallback cannot work either
        logger.warning("API preparation crashed unexpectedly: %s", e)
        return None


def _is_auth_failure(exc: BaseException) -> bool:
    """True if a (possibly wrapped) exception chain contains a 401 from the API.

    `api_flow.prepare_via_api` re-raises EasyApiError as ApiFlowError, so the
    cause chain is the only place the auth failure survives.
    """
    seen = 0
    while exc is not None and seen < 10:
        if isinstance(exc, ApiAuthError):
            return True
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return False


def _ui_logged_in(page: Page, timeout_ms: int = 3000) -> bool:
    """True if the browser already shows the post-login dashboard."""
    try:
        page.locator(locators.LOGIN_SUCCESS_MARKER).first.wait_for(
            state="visible", timeout=timeout_ms
        )
        return True
    except Exception:
        return False


def _try_resume_api(page: Page, symbol: str, logger):
    """
    ADR-005 fast path: reuse the cached JWT instead of logging in.

    Opens the broker page first (the browser needs the gateway/WAF cookies
    even when the API request itself is authorised by the bearer token), then
    validates the cached token by preparing the order over HTTP and reading
    the order list — the two capabilities the buy actually needs.

    Returns the prepared dict with a 'clock' (api_flow.ApiClock) or None to
    mean 'log in normally'. The token file is deleted only on a 401, so a
    transient network hiccup never throws away a working session.
    """
    if not TOKEN_CACHE:
        logger.info("Token cache disabled (TOKEN_CACHE=false) — logging in.")
        return None

    token = token_store.load_token(
        TOKEN_CACHE_PATH,
        TOKEN_CACHE_MIN_VALIDITY,
        account_hint=token_store.account_fingerprint(USERNAME),
    )
    if not token:
        return None

    logger.info("Found a %s — trying to reuse it (skipping login).", token_store.describe(TOKEN_CACHE_PATH))
    try:
        page.goto(BROKER_URL, wait_until="domcontentloaded")
    except Exception as e:
        require_live_browser(e)
        logger.warning("Could not open the broker page for the token check: %s", e)
        return None

    client = EasyApiClient(
        token, PlaywrightTransport(page.context.request), base_url=API_BASE_URL
    )
    # Cheap direct checks FIRST, so a dead token surfaces as an auth error we
    # can act on — inside prepare_via_api the same 401 arrives wrapped in an
    # ApiFlowError ("API prep failed: … 401 …").
    try:
        client.server_timestamp()
        client.list_orders()  # verification must work too, or the buy runs blind
    except ApiAuthError as e:
        logger.warning("Cached token rejected (401): %s", e)
        token_store.clear_token(TOKEN_CACHE_PATH)
        return None
    except EasyApiError as e:
        logger.warning("Cached token unusable (%s) — logging in instead.", e)
        return None
    except Exception as e:  # never let the fast path break the normal path
        require_live_browser(e)
        logger.warning("Cached-token check crashed (%s) — logging in instead.", e)
        return None

    try:
        prepared = api_flow.prepare_via_api(
            client,
            symbol=symbol,
            quantity=QUANTITY,
            price_mode=PRICE_MODE,
            price=PRICE,
        )
    except Exception as e:  # includes ApiFlowError; never break the normal path
        require_live_browser(e)
        if _is_auth_failure(e):
            logger.warning("Cached token rejected (401): %s", e)
            token_store.clear_token(TOKEN_CACHE_PATH)
        else:
            logger.warning("API prep with the cached token failed (%s) — logging in instead.", e)
        return None

    try:
        prepared["clock"] = api_flow.ApiClock(client).sync()
    except Exception as e:
        require_live_browser(e)
        logger.warning("Could not sync the API clock (%s) — logging in instead.", e)
        return None

    prepared["client"] = client
    prepared["resumed"] = True
    return prepared


def _wait_for_buy_button(page: Page, logger):
    """Presence check that the buy button exists (Selenium-era behavior)."""
    button = find_buy_button(page, timeout=5)
    if button is not None:
        logger.info("Buy button detected – good to go.")
    else:
        logger.warning("Buy button not found, but will still attempt.")


def main(argv=None):
    global _shutdown_requested
    setup_logging()
    logger = logging.getLogger("main")
    args = parse_args(argv)

    target_time: dt_time = args.time if args.time is not None else parse_time_arg(DEFAULT_BUY_TIME)
    verify = args.verify_order and not args.no_verify
    symbol = args.symbol if args.symbol is not None else SYMBOL

    # Register signal handler for Ctrl+C BEFORE anything else
    signal.signal(signal.SIGINT, _signal_handler)

    instance_info = f" (instance: {INSTANCE_ID})" if INSTANCE_ID else ""
    symbol_info = f" - Symbol: {symbol}" if symbol else ""
    logger.info("=" * 60)
    logger.info("AutoBuy-MofidBroker starting%s%s", instance_info, symbol_info)
    logger.info("Target time : %s", target_time.strftime("%H:%M:%S"))
    logger.info("Verify order: %s | Mode: %s", verify, "manual" if args.manual else "auto")
    api_enabled = API_MODE and not args.manual and not args.no_api
    logger.info("API mode  : %s", "ON (direct HTTP orders, UI fallback)" if api_enabled else "off (UI path)")
    logger.info(
        "Token cache: %s",
        token_store.describe(TOKEN_CACHE_PATH) if TOKEN_CACHE else "off (TOKEN_CACHE=false)",
    )
    logger.info("=" * 60)

    # ── Validate credentials ──
    if not USERNAME or not PASSWORD:
        logger.error(
            "Credentials not configured. Set MOFID_USER and MOFID_PASS "
            "environment variables or create a .env file."
        )
        sys.exit(1)

    if not args.manual and QUANTITY <= 0:
        logger.error("Auto mode requires QUANTITY > 0 (env var) — or use --manual.")
        sys.exit(1)

    page = None
    prepared_api = None
    now_fn = None      # broker-clock source; API clock when there is no UI login
    ui_ready = False   # True once the on-page clock / order ticket are usable
    browser_lost = False  # set when the Playwright session died mid-run
    buy_started = False   # True from the first submit attempt onwards
    try:
        # ── Auth: cached API token first (ADR-005), Playwright login otherwise ──
        page = initialize_driver()
        if api_enabled and not args.no_token_cache:
            prepared_api = _try_resume_api(page, symbol, logger)
            if prepared_api is not None:
                now_fn = prepared_api["clock"]
                ui_ready = _ui_logged_in(page)
                if ui_ready:
                    dismiss_announcement(page)
                logger.info(
                    "Reused the cached API token — login skipped%s.",
                    "" if ui_ready else " (browser UI is not logged in: API-only run)",
                )
        if prepared_api is None:
            if not login(page):
                logger.error("Login failed. Exiting.")
                sys.exit(1)
            dismiss_announcement(page)
            ui_ready = True

        # ── Order preparation ──
        if args.manual:
            logger.info("")
            logger.info("=" * 60)
            logger.info(">>> NOW PREPARE THE ORDER MANUALLY <<<")
            logger.info("1. Navigate to the symbol (e.g. فولاد)")
            logger.info("2. Fill in price and quantity")
            logger.info("3. Make sure the BUY button is visible (do NOT click it)")
            logger.info("=" * 60)
            input("Press ENTER when ready ...")
        else:
            if api_enabled and prepared_api is None:
                prepared_api = _try_prepare_api(
                    page, symbol, logger, save_token=not args.no_token_cache
                )
                if prepared_api is None:
                    logger.warning("API prep failed → falling back to the UI path.")
            if prepared_api is not None:
                summary = prepared_api
            else:
                summary = prepare_order(page, symbol=symbol)
            logger.info(
                "Auto-prepared %s × %s @ %s (%s)",
                summary["quantity"],
                summary["symbol"],
                summary["price"],
                summary["price_mode"],
            )

        # Check: has the time already passed while we were preparing?
        if is_time_already_passed(page, target_time, logger, now_fn=now_fn):
            logger.error("Target time already passed. Exiting.")
            sys.exit(1)

        # Quick check that the buy button is present (skipped on an API-only
        # run: there is no logged-in UI to inspect)
        if ui_ready:
            _wait_for_buy_button(page, logger)

        # ── Wait for exact time ──
        # should_stop makes the wait loop honour Ctrl+C: otherwise the signal
        # handler only set a flag and the bot kept polling (and, because the
        # console event also kills the Playwright driver, flooded the terminal
        # with "Target page, context or browser has been closed").
        logger.info("Waiting for broker clock to reach %s ...", target_time.strftime("%H:%M:%S"))
        wait_until_broker_time(
            page, target_time, now_fn=now_fn, should_stop=lambda: _shutdown_requested
        )

        # Check for shutdown request before executing buy
        if _shutdown_requested:
            logger.info("Shutdown requested. Exiting before buy.")
            sys.exit(0)

        # ── Execute buy ──
        success = None
        if prepared_api is not None:
            try:
                buy_started = True
                success = api_flow.execute_buy_api(
                    prepared_api["client"],
                    prepared_api,
                    now_fn=now_fn or prepared_api.get("clock"),
                )
            except BrowserGone:
                raise
            except Exception as e:
                require_live_browser(e)
                logger.error("API buy failed (%s) — falling back to UI buy.", e)
                success = None
            if success is False:
                # API explicitly stopped (e.g. 2xx-but-not-verified or broker rejection):
                # do NOT auto-click the UI on top — require manual check.
                logger.error("API buy reported failure — NOT falling back to UI (duplicate-order risk). Manual check required.")
        if success is None and not ui_ready:
            # Cached-token run without a logged-in browser: the UI fallback
            # needs a real session first.
            logger.warning("UI fallback needed but the browser is not logged in — logging in now.")
            if login(page):
                dismiss_announcement(page)
                ui_ready = True
            else:
                logger.error("Login failed — the UI fallback is unavailable.")
                success = False
        if success is None:
            # UI path (either API disabled, API prep failed, or API buy raised)
            buy_started = True
            if prepared_api is not None:
                # The API path died mid-flight — an order may already exist.
                if api_flow.order_already_present(prepared_api["client"], prepared_api):
                    logger.error("Order already present after API crash — NOT falling back to UI (duplicate-order risk). Manual check required.")
                    success = False
                else:
                    logger.info("Order list clean after API crash — re-preparing via UI before UI buy.")
                    prepare_order(page, symbol=symbol)
                    success = execute_buy(page)
            else:
                success = execute_buy(page)

        if success:
            logger.info("🎉 Trade completed successfully%s.", instance_info)
        else:
            logger.error("💥 Trade failed%s. Check the browser manually.", instance_info)

    except KeyboardInterrupt:
        logger.info("Interrupted by user (Ctrl+C)%s.", instance_info)
    except BrowserGone as e:
        browser_lost = True
        _shutdown_now(logger, instance_info, str(e), buy_started)
    except TimeoutError as e:
        logger.error("Timeout%s: %s", instance_info, e)
    except Exception as e:
        # Any Playwright "target closed" error that escaped a retry loop ends
        # the run with one line instead of a traceback.
        if is_browser_gone(e):
            browser_lost = True
            _shutdown_now(logger, instance_info, str(e), buy_started)
        logger.exception("Unexpected error%s: %s", instance_info, e)
    finally:
        # Allow user to inspect, but in small sleep chunks so Ctrl+C can break through
        if not _shutdown_requested and not browser_lost:
            logger.info(
                "Keeping browser open for 10 seconds for inspection (Ctrl+C to close now)%s...",
                instance_info,
            )
            for _ in range(20):  # 20 × 0.5s = 10s, but interruptible
                if _shutdown_requested:
                    break
                time.sleep(0.5)
        cleanup_all()
        logger.info("Done%s.", instance_info)


if __name__ == "__main__":
    main()
