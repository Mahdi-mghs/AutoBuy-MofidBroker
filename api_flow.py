"""
Hybrid orchestration (ADR-004): browser-bootstrapped login/token + raw HTTP
for symbol/price/order; automatic fallback to the proven UI path if any
API step fails.

Flow (API_MODE=true):
1. Playwright login (existing broker.login) + dismiss announcement
2. easy_api.extract_auth_token — harvest JWT from page traffic
3. easy_api.EasyApiClient over PlaywrightTransport (WAF-safe)
4. find_symbol → market_data (ceiling/reference price from HTTP, no clicks)
   + /easy/api/money for the balance check (قدرت خرید / مانده قابل معامله)
5. Buyer-clock wait — broker.wait_until_broker_time (UI clock stays the
   reference; it reports the same time as the API serverTimestamp)
6. submit-buy burst over HTTP — spaced by the client's 320 ms submit gate
   (broker rule 13021); pre-open refusals (8702/11000) are retried without
   spending the order budget, so a burst started at 08:44:58 is still in
   flight when the session opens at 08:45:00
7. list_orders verification (UI orders-tab check only as fallback)

Any EasyApiError after login → raise ApiFlowError → main() falls back to
the UI path (prepare_order + execute_buy) instead of failing the run.
"""

import logging
import time
from datetime import datetime, time as dt_time
from typing import NamedTuple

from config import (
    API_BUY_MAX_ATTEMPTS,
    API_BUY_RETRY_INTERVAL,
    API_MIN_SUBMIT_INTERVAL,
    API_ORDER_WINDOW_GRACE,
    API_PREOPEN_MAX_ATTEMPTS,
    VERIFY_ORDER,
)
from driver import require_live_browser
from easy_api import (
    EasyApiClient,
    EasyApiError,
    ORDER_WINDOW_CLOSE,
    ORDER_WINDOW_OPEN,
    OMS_CODES_NOT_OPEN,
    OMS_CODE_ORDER_INTERVAL,
    TEHRAN_TZ,
    buying_power,
    parse_oms_error,
)

logger = logging.getLogger(__name__)


class ApiFlowError(RuntimeError):
    """Raised when the API flow can't proceed — caller falls back to UI."""


class ApiClock:
    """
    Broker wall clock derived from the HTTP API.

    Needed when the browser UI is not logged in (cached-token resume), so
    there is no on-page clock to read. Polling /server-time at the 50 ms
    clock interval would be ~20 req/s — nothing like the real app and an
    obvious rate-limit magnet — so the offset is measured once and the local
    clock carries it, with a lazy re-sync every `resync_after_s`.
    """

    def __init__(
        self,
        client: EasyApiClient,
        tz=TEHRAN_TZ,
        resync_after_s: float = 60.0,
        now_fn=time.time,
    ):
        self.client = client
        self.tz = tz
        self.resync_after_s = resync_after_s
        self._now_fn = now_fn
        self._offset_ms = 0.0
        self._synced_at = 0.0

    def sync(self) -> "ApiClock":
        local_ms = self._now_fn() * 1000.0
        self._offset_ms = float(self.client.server_timestamp()) - local_ms
        self._synced_at = self._now_fn()
        logger.info("API clock synced (offset %+.0f ms).", self._offset_ms)
        return self

    def now(self) -> datetime:
        if self._synced_at and (self._now_fn() - self._synced_at) > self.resync_after_s:
            try:
                self.sync()
            except Exception as e:
                self._synced_at = self._now_fn()  # don't retry on every poll
                logger.warning("API clock re-sync failed (%s) — keeping drift.", e)
        ts = (self._now_fn() * 1000.0 + self._offset_ms) / 1000.0
        return datetime.fromtimestamp(ts, self.tz)

    def time(self):
        """Current broker wall-clock time (naive, like the on-page clock)."""
        return self.now().time()

    def __call__(self):
        return self.time()


def resolve_price(md: dict, price_mode: str, price: int) -> int:
    """
    Decide the order price from live market data (no UI chips).

    ceiling   → highAllowedPrice  (سقف قیمت, server-confirmed)
    reference → closingPrice      (قیمت پایانی)
    fixed     → explicit price (must be > 0, checked by caller too)
    """
    mode = (price_mode or "ceiling").lower()
    if mode == "fixed":
        p = int(price or 0)
        if p <= 0:
            raise ApiFlowError("PRICE_MODE=fixed requires PRICE > 0.")
        return p
    if mode == "ceiling":
        p = int(md.get("highAllowedPrice") or 0)
        if p <= 0:
            raise ApiFlowError("marketdata highAllowedPrice missing/zero.")
        return p
    if mode in ("reference", "closing"):
        p = int(md.get("closingPrice") or 0)
        if p <= 0:
            raise ApiFlowError("marketdata closingPrice missing/zero.")
        return p
    raise ApiFlowError(f"Unknown price mode {price_mode!r} (ceiling|reference|fixed).")


def prepare_via_api(
    client: EasyApiClient,
    symbol: str,
    quantity: int | None = None,
    price_mode: str = "ceiling",
    price: int = 0,
) -> dict:
    """
    API twin of order.prepare_order: symbol → isin, live quote, final price.
    Returns the summary dict + isin/commission for execute_buy_api.
    Raises ApiFlowError on any problem.
    """
    if not symbol:
        raise ApiFlowError("No symbol configured.")
    if quantity is None or int(quantity) <= 0:
        raise ApiFlowError("quantity must be > 0.")

    try:
        md = client.market_data(client.find_symbol(symbol)["symbolIsin"])
    except EasyApiError as e:
        raise ApiFlowError(f"API prep failed: {e}") from e
    final_price = resolve_price(md, price_mode, price)

    min_q = md.get("minValidBuyVolume")
    max_q = md.get("maxValidBuyVolume")
    if min_q and int(quantity) < int(min_q):
        raise ApiFlowError(f"quantity {quantity} below minValidBuyVolume {min_q}.")
    if max_q and int(quantity) > int(max_q):
        raise ApiFlowError(f"quantity {quantity} above maxValidBuyVolume {max_q}.")

    commission = client.buy_commission(md)
    summary = {
        "symbol": symbol,
        "isin": md.get("symbolIsin"),
        "quantity": int(quantity),
        "price": final_price,
        "price_mode": (price_mode or "ceiling").lower(),
        "commission": commission,
        "high_allowed": md.get("highAllowedPrice"),
        "low_allowed": md.get("lowAllowedPrice"),
        "closing": md.get("closingPrice"),
        # Same rounding as build_buy_payload's totalValue (RIAL).
        "order_value": round(final_price * int(quantity) * (1 + commission)),
    }

    # Balance check (قدرت خرید / مانده قابل معامله) — read here, BEFORE the clock
    # wait, so the burst pays no extra round trip on the opening millisecond.
    try:
        summary["buying_power"] = buying_power(client.money())
    except EasyApiError as e:
        logger.warning("Account balance unavailable (%s) — balance check skipped.", e)
    gap = _shortfall(summary)
    if gap is not None:
        logger.warning(
            "⚠️ Order value %s exceeds the buying power %s by %s — the broker will "
            "reject this for insufficient balance. Top up or lower QUANTITY=%d.",
            _rial(summary["order_value"]), _rial(summary["buying_power"]), _rial(gap),
            int(quantity),
        )

    logger.info("API order prepared: %s", summary)
    return summary


def _rial(amount: int) -> str:
    """The API speaks RIAL, users think TOMAN — show both."""
    return f"{int(amount):,} Rial ({int(amount) // 10:,} Toman)"


def _shortfall(prepared: dict) -> int | None:
    """Rial by which the order value exceeds the buying power.

    None when the balance is unknown or covers the order.
    """
    power = prepared.get("buying_power")
    value = prepared.get("order_value")
    if power is None or value is None:
        return None
    gap = int(value) - int(power)
    return gap if gap > 0 else None


def log_order_review(prepared: dict) -> bool:
    """
    Short review of the pending order, printed right before the submit burst.

    No I/O: price, value and balance were all read during preparation. Returns
    False when the order costs more than the buying power — a warning only, the
    submit still happens because the broker is the final judge (a credit line
    can cover the gap, and a real refusal stops the burst anyway).
    """
    qty = int(prepared["quantity"])
    price = int(prepared["price"])
    commission = float(prepared.get("commission") or 0.0)
    value = int(prepared.get("order_value") or round(price * qty * (1 + commission)))
    prepared["order_value"] = value

    logger.info("Order review:")
    logger.info("  symbol        %s (%s)", prepared.get("symbol"), prepared.get("isin"))
    logger.info("  quantity      %s share(s)", f"{qty:,}")
    logger.info("  price         %s  [%s]", _rial(price), prepared.get("price_mode") or "?")
    logger.info(
        "  commission    %.4f%% → %s", commission * 100, _rial(round(price * qty * commission))
    )
    logger.info("  order value   %s", _rial(value))

    power = prepared.get("buying_power")
    if power is None:
        logger.warning("  buying power  unknown — balance check unavailable.")
        return True
    logger.info("  buying power  %s", _rial(power))
    gap = _shortfall(prepared)
    if gap is not None:
        logger.warning(
            "  ⚠️ SHORT by %s — the broker will reject this order for insufficient "
            "balance. Top up or lower QUANTITY=%d.",
            _rial(gap), qty,
        )
        return False
    logger.info("  balance after %s", _rial(int(power) - value))
    return True


# ── Broker response classification (measured live 2026-10-18) ──
# Every submit answers HTTP 2xx; the verdict is in the JSON body:
#   {"isSuccessful": false, "id": "...", "message": "8702: ...",
#    "omsError": [{"error": "...", "code": 8702, "name": "OmsCustomError"}]}
SUBMIT_ACCEPTED = "accepted"
SUBMIT_THROTTLED = "throttled"
SUBMIT_NOT_OPEN = "not_open"
SUBMIT_REJECTED = "rejected"

# Wording the OMS uses when the session simply has not opened yet. Codes are
# the primary signal; these strings are a fallback for the day the codes are
# renumbered. Both were captured live: 8702 (before 08:45) and 11000
# ("وضعیت گروه اجازه فعالیت نمی‌دهد", 08:44:57 on a real-money run).
_NOT_OPEN_HINTS = ("اجازه فعالیت نمی‌دهد", "ساعت مجاز ثبت سفارش")


class SubmitOutcome(NamedTuple):
    kind: str
    code: int | None
    message: str

    @property
    def ok(self) -> bool:
        return self.kind == SUBMIT_ACCEPTED


def classify_submit_response(resp) -> SubmitOutcome:
    """
    Read a 2xx submit body as a verdict.

    accepted  — isSuccessful true, or a body with no error field at all
    throttled — 13021 OrderIntervalError: the previous submit was < 300 ms
                ago, so THIS ORDER NEVER REACHED THE BOOK. Not a rejection;
                re-submitting after the interval is the intended behaviour.
    not_open  — 8702 (outside 08:45–12:30) or 11000 ("وضعیت گروه اجازه فعالیت
                نمی‌دهد"): the session has not started yet. The order never
                reached the book, so this is NOT a refusal of the order — a
                real-money run at 08:44:57 got 11000 and the old code gave up.
                Keep submitting until the open, then stop on a real verdict.
    rejected  — any other OMS error (e.g. 8006 minimum order value): the broker
                would answer exactly the same thing again, so stop.
    """
    if isinstance(resp, dict) and resp.get("isSuccessful") is True:
        err = parse_oms_error(resp)
        return SubmitOutcome(SUBMIT_ACCEPTED, err and err["code"], (err and err["message"]) or "")
    err = parse_oms_error(resp)
    if err is None:
        return SubmitOutcome(SUBMIT_ACCEPTED, None, "")
    code, msg = err["code"], err["message"]
    if code == OMS_CODE_ORDER_INTERVAL:
        return SubmitOutcome(SUBMIT_THROTTLED, code, msg)
    if code in OMS_CODES_NOT_OPEN or any(h in msg for h in _NOT_OPEN_HINTS):
        return SubmitOutcome(SUBMIT_NOT_OPEN, code, msg)
    return SubmitOutcome(SUBMIT_REJECTED, code, msg)


def window_state(now_fn=None) -> str:
    """'early' | 'open' | 'late' | 'unknown' from the broker clock."""
    if now_fn is None:
        return "unknown"
    try:
        now = now_fn()
    except Exception:
        return "unknown"
    t = now.time() if isinstance(now, datetime) else now
    if not isinstance(t, dt_time):
        return "unknown"
    if t < dt_time(*ORDER_WINDOW_OPEN):
        return "early"
    if t > dt_time(*ORDER_WINDOW_CLOSE):
        return "late"
    return "open"


def seconds_until_window_open(now_fn=None) -> float | None:
    """
    Seconds from the broker clock until 08:45 today (None if unknown).

    Accepts either a datetime-returning clock or a `time`-returning one —
    `ApiClock.__call__` returns a bare `time` (that is what the broker-clock
    wait consumes), and Iran has no DST, so anchoring it to today's Tehran
    date is safe.
    """
    if now_fn is None:
        return None
    try:
        now = now_fn()
    except Exception:
        return None
    if isinstance(now, dt_time):
        now = datetime.combine(datetime.now(TEHRAN_TZ).date(), now, tzinfo=TEHRAN_TZ)
    if not isinstance(now, datetime):
        return None
    target = now.replace(
        hour=ORDER_WINDOW_OPEN[0], minute=ORDER_WINDOW_OPEN[1], second=0, microsecond=0
    )
    return (target - now).total_seconds()


def execute_buy_api(
    client: EasyApiClient,
    prepared: dict,
    timeout_fn=time.time,
    now_fn=None,
) -> bool:
    """
    API twin of order.execute_buy: rapid HTTP submits until the order list
    confirms. Semantics mirror the UI path:

    - TRANSPORT or HTTP failure (EasyApiError) → retry (same as failed clicks)
    - 2xx response → verify in the order list so we NEVER fire duplicate
      orders while verification lags.
    - VERIFY_ORDER=false → first accepted response wins (no polling).
    - Broker throttle (13021) → wait out the 300 ms rule and resubmit; the
      attempt budget is untouched because nothing reached the book.
    - Session not open yet (8702 / 11000, i.e. before 08:45) → the order never
      reached the book: resubmit immediately and do NOT spend the attempt
      budget. This is the whole point of targeting 08:44:58 — the burst must
      survive the pre-open refusals and be in flight the moment the session
      opens. Bounded by API_PREOPEN_MAX_ATTEMPTS so a holiday can't loop.
    - Anything else (8006 minimum value, bands, balance) → stop: the broker
      would answer identically.

    `now_fn` is a broker-clock callable (api_flow.ApiClock or similar) used
    to tell "not open yet" from "closed for the day"; None means "unknown"
    and the pre-open path relies on the refusal codes alone.

    Returns True if the order was accepted (and verified, if enabled).
    """
    isin = prepared["isin"]
    symbol_name = prepared["symbol"]
    quantity = int(prepared["quantity"])
    price = int(prepared["price"])
    commission = float(prepared.get("commission") or 0.003712)

    max_attempts = max(1, int(API_BUY_MAX_ATTEMPTS))
    max_refusals = max(1, int(API_PREOPEN_MAX_ATTEMPTS))

    # What is about to be bought, and does the balance cover it? Logged from the
    # numbers preparation already read — no extra request before the open.
    log_order_review(prepared)

    # The broker rejects submits < 300 ms apart; never go below the floor.
    interval = max(float(API_BUY_RETRY_INTERVAL), float(API_MIN_SUBMIT_INTERVAL))
    logger.info(
        "API buy burst starting (up to %d order attempts + %d pre-open retries, "
        "%.0f ms apart → ~%.1fs of coverage)",
        max_attempts, max_refusals, interval * 1000,
        max_attempts * interval,
    )

    baseline = None
    if VERIFY_ORDER:
        try:
            baseline = client.list_orders()
            logger.info("Baseline order count: %d", len(baseline))
        except Exception as e:
            require_live_browser(e)
            logger.warning("Could not read baseline orders (%s) — verification weaker.", e)

    attempts = 0       # submits that could have become an order
    refusals = 0       # "not open yet" answers: nothing reached the book
    last_code: int | None = None
    iterations = 0
    max_iterations = (max_attempts + max_refusals) * 4   # throttles are free but capped
    while attempts < max_attempts and refusals < max_refusals and iterations < max_iterations:
        iterations += 1
        try:
            resp = client.submit_buy(
                isin=isin, symbol_name=symbol_name, quantity=quantity,
                price=price, commission=commission, min_interval=interval,
            )
        except Exception as e:
            # A dead browser (Ctrl+C kills the driver) is not a transport
            # hiccup: every further submit would fail the same way.
            require_live_browser(e)
            attempts += 1
            logger.info("Attempt %d/%d: submit error (%s)", attempts, max_attempts, e)
            continue

        outcome = classify_submit_response(resp)
        if outcome.kind == SUBMIT_THROTTLED:
            # Not an order: the broker refused the cadence. Retry is free.
            logger.warning(
                "Throttled by the interval rule (%s) — retrying after the gap, "
                "attempt %d/%d still available.",
                outcome.message, max_attempts - attempts, max_attempts,
            )
            continue

        if outcome.kind == SUBMIT_NOT_OPEN:
            last_code = outcome.code
            state = window_state(now_fn)
            if state == "late":
                logger.error(
                    "Order window already closed (code %s): %s — not retrying.",
                    outcome.code, outcome.message,
                )
                return False
            refusals += 1
            if refusals == 1:
                wait_s = seconds_until_window_open(now_fn) if state == "early" else None
                logger.info(
                    "Session not open yet (code %s: %s) — the order never reached "
                    "the book; retrying until it opens (up to %d pre-open submits).",
                    outcome.code, outcome.message, max_refusals,
                )
                if wait_s is not None and wait_s > API_ORDER_WINDOW_GRACE:
                    logger.warning(
                        "The session opens in %.1fs — the pre-open burst only covers "
                        "~%.0fs, so it may run out before then. Start within %ds of "
                        "08:45 (08:44:58 is the default target).",
                        wait_s, max_refusals * interval, int(API_ORDER_WINDOW_GRACE),
                    )
            else:
                logger.debug("Pre-open retry %d/%d (%s).", refusals, max_refusals, outcome.code)
                if refusals % 5 == 0:
                    logger.info(
                        "Pre-open retries: %d/%d (still code %s).",
                        refusals, max_refusals, outcome.code,
                    )
            continue

        attempts += 1
        if outcome.kind == SUBMIT_REJECTED:
            logger.info(
                "Attempt %d/%d: broker rejected (code %s): %s — not re-submitting.",
                attempts, max_attempts, outcome.code, outcome.message,
            )
            return False

        logger.info("Attempt %d/%d: accepted (2xx).", attempts, max_attempts)

        if not VERIFY_ORDER:
            return True

        if order_appears(client, prepared, baseline, deadline_s=3.0, timeout_fn=timeout_fn):
            logger.info("✅ Order verified in order list (API).")
            return True
        logger.warning("⚠️ 2xx but order not visible yet — checking once more before retrying.")
        if order_appears(client, prepared, baseline, deadline_s=VERIFY_ORDER_EXTRA_S, timeout_fn=timeout_fn):
            logger.info("✅ Order verified late (API).")
            return True
        # Not confirmed — retrying is dangerous (duplicate orders). Fail loudly:
        logger.error(
            "2xx response but order never appeared — STOPPING to avoid duplicates. "
            "Check manually!"
        )
        return False

    if refusals >= max_refusals and attempts < max_attempts:
        logger.error(
            "❌ Session never opened after %d pre-open submits (last code %s) — "
            "no order was placed. Check the market calendar and that the target "
            "time is within %ds of 08:45.",
            refusals, last_code, int(API_ORDER_WINDOW_GRACE),
        )
    else:
        logger.error("❌ Order not accepted after %d API attempts.", attempts)
    return False


VERIFY_ORDER_EXTRA_S = 3.0


def order_already_present(client: EasyApiClient, prepared: dict) -> bool:
    """
    True if an order matching the prepared one already exists in the order
    list (used after a mid-flight API crash, before any UI fallback, to
    prevent duplicate orders).
    """
    try:
        return bool(
            client.order_appeared(
                isin=prepared["isin"],
                quantity=int(prepared["quantity"]),
                price=int(prepared["price"]),
                baseline=None,
            )
        )
    except Exception as e:
        logger.warning("order_already_present lookup failed: %s", e)
        return False


def order_appears(client, prepared: dict, baseline, deadline_s: float, timeout_fn=time.time) -> bool:
    """Poll list_orders up to deadline_s for the matching order row."""
    isin = prepared["isin"]
    quantity = int(prepared["quantity"])
    price = int(prepared["price"])
    end = timeout_fn() + deadline_s
    while timeout_fn() < end:
        try:
            if client.order_appeared(isin=isin, quantity=quantity, price=price, baseline=baseline):
                return True
        except Exception as e:
            require_live_browser(e)  # a dead page can never confirm anything
            logger.debug("order_appeared poll error: %s", e)
        time.sleep(0.25)
    return False
