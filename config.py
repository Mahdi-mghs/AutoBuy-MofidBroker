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
# Default target time if --time not provided (HH:MM:SS). The session opens
# at 08:45:00 and the OMS only accepts orders from then on, so the burst is
# deliberately started a couple of seconds early and retries through the
# pre-open refusals to be first at the open (ADR-006).
DEFAULT_BUY_TIME = os.getenv("DEFAULT_BUY_TIME", "08:44:58")

# ── Logging ─────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = os.getenv("LOG_FILE", "autobuy.log")

# ── Order result verification ───────────────────────────
# If True, after a successful click we check the orders tab
VERIFY_ORDER = os.getenv("VERIFY_ORDER", "true").lower() == "true"
# How long to wait for the order to appear in the list (seconds)
VERIFY_ORDER_TIMEOUT = int(os.getenv("VERIFY_ORDER_TIMEOUT", "10"))

# ── Direct API mode (ADR-004) ────────────────────────────
# API_MODE=true → after login, symbol/price/order traffic goes over raw
# HTTP through Playwright's browser context (WAF-safe). Any API failure
# falls back to the UI path. Set false to disable.
API_MODE = os.getenv("API_MODE", "true").lower() == "true"
API_BASE_URL = os.getenv("API_BASE_URL", "https://api-mts.orbis.easytrader.ir")
# Order POSTs that can actually become an order. The burst is aimed at the
# millisecond the session opens: 20 submits at 350 ms cover ~7 s of open-time,
# which is what it takes to land at/near the front of the buy queue. Pre-open
# refusals (11000/8702) do NOT consume this budget — see
# API_PREOPEN_MAX_ATTEMPTS.
API_BUY_MAX_ATTEMPTS = int(os.getenv("API_BUY_MAX_ATTEMPTS", "20"))
# Submits refused because the session has not opened yet (code 8702
# "ساعت مجاز ثبت سفارش" or 11000 "وضعیت گروه اجازه فعالیت نمی‌دهد"). Nothing
# reached the book, so retrying is free — but bounded: 30 × 350 ms ≈ 10 s of
# pre-open coverage, after which the run gives up instead of spamming a
# closed/holiday market.
API_PREOPEN_MAX_ATTEMPTS = int(os.getenv("API_PREOPEN_MAX_ATTEMPTS", "30"))
# Send-to-send spacing between submits. The broker rejects two orders closer
# than 300 ms apart (code 13021, measured live 2026-10-18), so this must stay
# above API_MIN_SUBMIT_INTERVAL. 350 ms leaves room for jitter.
API_BUY_RETRY_INTERVAL = float(os.getenv("API_BUY_RETRY_INTERVAL", "0.35"))
# Hard floor. 320 ms — not the documented 300 — because measured arrival jitter
# made 300 ms spacing trip the rule 2 times in 5; 320 ms was clean. Lower values
# are raised to this (a throttled submit never reaches the book).
API_MIN_SUBMIT_INTERVAL = float(os.getenv("API_MIN_SUBMIT_INTERVAL", "0.32"))
# The session runs 08:45:00–12:35:00 and the OMS accepts new orders
# 08:45–12:30 (code 8702). If the session is further away than this many
# seconds when the burst starts, the log says so — the pre-open retry budget
# won't reach the open. 8 s is enough for the default 08:44:58 target.
API_ORDER_WINDOW_GRACE = float(os.getenv("API_ORDER_WINDOW_GRACE", "8"))

# ── API token cache (ADR-005) ────────────────────────────
# Reuse the JWT saved by the previous run when it is still valid: skip the
# Playwright login and validate it with one read-only request. Any rejection
# clears the cache and falls back to the normal login.
TOKEN_CACHE = os.getenv("TOKEN_CACHE", "true").lower() == "true"
# Relative paths resolve against the project root (next to config.py).
TOKEN_CACHE_PATH = os.getenv("TOKEN_CACHE_PATH", ".mofid_token.json")
# A cached token must stay valid at least this long (seconds) to be reused.
TOKEN_CACHE_MIN_VALIDITY = int(os.getenv("TOKEN_CACHE_MIN_VALIDITY", "120"))
