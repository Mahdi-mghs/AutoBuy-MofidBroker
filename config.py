"""
Configuration — loads from environment variables or .env file.
Never hardcode credentials in source code.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env file if present (for local development)
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    load_dotenv(env_path)

# ── Broker ──────────────────────────────────────────────
BROKER_URL = os.getenv("BROKER_URL", "https://d.easytrader.ir/")
SYMBOL = os.getenv("SYMBOL", "")
USERNAME = os.getenv("MOFID_USER", "")
PASSWORD = os.getenv("MOFID_PASS", "")

# ── Browser (Playwright uses installed Google Chrome) ──
CHROME_USER_DATA_DIR = os.getenv("CHROME_USER_DATA_DIR", "")
HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"
INSTANCE_ID = os.getenv("INSTANCE_ID", "")

# ── Order preparation (automated) ───────────────────────
# QUANTITY: number of shares
QUANTITY = int(os.getenv("QUANTITY", "0"))
# PRICE_MODE: how to decide the order price
#   ceiling  — click سقف قیمت (max allowed price)   [default]
#   reference — click قیمت مرجع (reference price)
#   fixed    — type the explicit PRICE value
PRICE_MODE = os.getenv("PRICE_MODE", "ceiling").lower()
# PRICE: explicit price used only when PRICE_MODE=fixed
PRICE = int(os.getenv("PRICE", "0"))

# ── Timeouts (seconds) ──────────────────────────────────
DEFAULT_TIMEOUT = int(os.getenv("DEFAULT_TIMEOUT", "20"))
LOGIN_TIMEOUT = int(os.getenv("LOGIN_TIMEOUT", "20"))
NOTIFY_TIMEOUT = float(os.getenv("NOTIFY_TIMEOUT", "1"))

# ── Retry settings ──────────────────────────────────────
LOGIN_MAX_ATTEMPTS = int(os.getenv("LOGIN_MAX_ATTEMPTS", "5"))
BUY_MAX_ATTEMPTS = int(os.getenv("BUY_MAX_ATTEMPTS", "25"))
BUY_RETRY_INTERVAL = float(os.getenv("BUY_RETRY_INTERVAL", "0.10"))

# ── Clock wait ──────────────────────────────────────────
CLOCK_POLL_INTERVAL = float(os.getenv("CLOCK_POLL_INTERVAL", "0.05"))
# Safety: give up waiting for broker time after this many seconds
CLOCK_DEADLINE_SECONDS = int(os.getenv("CLOCK_DEADLINE_SECONDS", "3600"))
# Default target time if --time not provided (HH:MM:SS)
DEFAULT_BUY_TIME = os.getenv("DEFAULT_BUY_TIME", "08:44:58")

# ── Logging ─────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = os.getenv("LOG_FILE", "autobuy.log")

# ── Order result verification ───────────────────────────
# If True, after a successful click we check the orders tab
VERIFY_ORDER = os.getenv("VERIFY_ORDER", "true").lower() == "true"
# How long to wait for the order to appear in the list (seconds)
VERIFY_ORDER_TIMEOUT = int(os.getenv("VERIFY_ORDER_TIMEOUT", "10"))
