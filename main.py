"""
Main entry point — CLI orchestration for AutoBuy-MofidBroker.

Usage:
    python main.py --time 08:44:58
    python main.py --time 09:00:00 --verify-order
    python main.py --time 08:44:58 --no-verify
"""

import argparse
import logging
import signal
import sys
import time
from datetime import time as dt_time

from config import LOG_LEVEL, LOG_FILE, USERNAME, PASSWORD, DEFAULT_BUY_TIME, INSTANCE_ID, SYMBOL
from driver import initialize_driver, cleanup_all
from broker import login, dismiss_announcement, wait_until_broker_time, get_broker_time
from order import execute_buy

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


def parse_args():
    parser = argparse.ArgumentParser(
        description="AutoBuy — Mofid Securities automated buy bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py --time 08:44:58
  python main.py --time 09:00 --no-verify
        """,
    )
    parser.add_argument(
        "--time", "-t",
        type=parse_time_arg,
        required=False,
        help="Target broker clock time to click buy (HH:MM or HH:MM:SS). Default: from DEFAULT_BUY_TIME env var (08:44:58)",
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
    return parser.parse_args()


def is_time_already_passed(driver, target: dt_time) -> bool:
    """Check if the broker clock has already passed the target time."""
    try:
        now = get_broker_time(driver)
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


def main():
    global _shutdown_requested
    setup_logging()
    logger = logging.getLogger("main")
    args = parse_args()

    target_time: dt_time = args.time if args.time is not None else parse_time_arg(DEFAULT_BUY_TIME)
    verify = args.verify_order and not args.no_verify

    # Register signal handler for Ctrl+C BEFORE anything else
    signal.signal(signal.SIGINT, _signal_handler)

    instance_info = f" (instance: {INSTANCE_ID})" if INSTANCE_ID else ""
    symbol_info = f" - Symbol: {SYMBOL}" if SYMBOL else ""
    logger.info("=" * 60)
    logger.info("AutoBuy-MofidBroker starting%s%s", instance_info, symbol_info)
    logger.info("Target time : %s", target_time.strftime("%H:%M:%S"))
    logger.info("Verify order: %s", verify)
    logger.info("=" * 60)

    # ── Validate credentials ──
    if not USERNAME or not PASSWORD:
        logger.error(
            "Credentials not configured. Set MOFID_USER and MOFID_PASSWORD "
            "environment variables or create a .env file."
        )
        sys.exit(1)

    driver = None
    try:
        # ── Login ──
        driver = initialize_driver()
        if not login(driver):
            logger.error("Login failed. Exiting.")
            sys.exit(1)
        dismiss_announcement(driver)

        # ── Manual preparation ──
        logger.info("")
        logger.info("=" * 60)
        logger.info(">>> NOW PREPARE THE ORDER MANUALLY <<<")
        logger.info("1. Navigate to the symbol (e.g. فولاد)")
        logger.info("2. Fill in price and quantity")
        logger.info("3. Make sure the BUY button is visible (do NOT click it)")
        logger.info("=" * 60)
        input("Press ENTER when ready ...")

        # Check: has the time already passed while we were preparing?
        if is_time_already_passed(driver, target_time):
            logger.error("Target time already passed. Exiting.")
            sys.exit(1)

        # Quick check that the buy button is present
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.common.exceptions import TimeoutException

        try:
            WebDriverWait(driver, 5).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, 'button[data-cy="oms-order-form-submit-button-buy"]')
                )
            )
            logger.info("Buy button detected – good to go.")
        except TimeoutException:
            logger.warning("Buy button not found, but will still attempt.")

        # ── Wait for exact time ──
        logger.info("Waiting for broker clock to reach %s ...", target_time.strftime("%H:%M:%S"))
        wait_until_broker_time(driver, target_time)

        # Check for shutdown request before executing buy
        if _shutdown_requested:
            logger.info("Shutdown requested. Exiting before buy.")
            sys.exit(0)

        # ── Execute buy ──
        success = execute_buy(driver)

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
            logger.info("Keeping browser open for 10 seconds for inspection (Ctrl+C to close now)%s...", instance_info)
            for _ in range(20):  # 20 × 0.5s = 10s, but interruptible
                if _shutdown_requested:
                    break
                time.sleep(0.5)
        cleanup_all()
        logger.info("Done%s.", instance_info)


if __name__ == "__main__":
    main()
