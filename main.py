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
)
from driver import initialize_driver, cleanup_all
from broker import login, dismiss_announcement, wait_until_broker_time, get_broker_time
from order import execute_buy, prepare_order, find_buy_button

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


def is_time_already_passed(page: Page, target: dt_time, logger) -> bool:
    """Check if the broker clock has already passed the target time."""
    try:
        now = get_broker_time(page)
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
    try:
        # ── Login ──
        page = initialize_driver()
        if not login(page):
            logger.error("Login failed. Exiting.")
            sys.exit(1)
        dismiss_announcement(page)

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
            summary = prepare_order(page, symbol=symbol)
            logger.info(
                "Auto-prepared %s × %s @ %s (%s)",
                summary["quantity"],
                summary["symbol"],
                summary["price"],
                summary["price_mode"],
            )

        # Check: has the time already passed while we were preparing?
        if is_time_already_passed(page, target_time, logger):
            logger.error("Target time already passed. Exiting.")
            sys.exit(1)

        # Quick check that the buy button is present
        _wait_for_buy_button(page, logger)

        # ── Wait for exact time ──
        logger.info("Waiting for broker clock to reach %s ...", target_time.strftime("%H:%M:%S"))
        wait_until_broker_time(page, target_time)

        # Check for shutdown request before executing buy
        if _shutdown_requested:
            logger.info("Shutdown requested. Exiting before buy.")
            sys.exit(0)

        # ── Execute buy ──
        success = execute_buy(page)

        if success:
            logger.info("🎉 Trade completed successfully%s.", instance_info)
        else:
            logger.error("💥 Trade failed%s. Check the browser manually.", instance_info)

    except KeyboardInterrupt:
        logger.info("Interrupted by user (Ctrl+C)%s.", instance_info)
    except TimeoutError as e:
        logger.error("Timeout%s: %s", instance_info, e)
    except Exception as e:
        logger.exception("Unexpected error%s: %s", instance_info, e)
    finally:
        # Allow user to inspect, but in small sleep chunks so Ctrl+C can break through
        if not _shutdown_requested:
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
