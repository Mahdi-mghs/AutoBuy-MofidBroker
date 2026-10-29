"""
EasyTrader HTTP client (ADR-004) — direct API calls replacing UI clicks.

Built EXCLUSIVELY from traffic captured live on 2026-10-16 by
capture_endpoints.py (artifacts/api-report.md). Every request shape here
mirrors what the real web app sends.

Auth model: the web app logs in via OIDC PKCE at login.emofid.com and
carries a JWT as `Authorization: Bearer <token>` on all API calls. We
reuse the freshly-bootstrapped Playwright login and harvest the token
from the page's own network traffic (fresh token each run — ADR-004).

TRANSPORT (important): Mofid's API gateway sits behind a WAF that
challenges non-browser TLS fingerprints (a python-requests call gets an
HTML challenge page instead of JSON, verified live 2026-10-16). The
default transport is therefore Playwright's APIRequestContext
(`context.request`), which sends requests through the real Chromium
network stack — same TLS fingerprint, same cookies, always passes.
The plain `requests` transport is kept only for tests/mocks.

Captured request/response facts (2026-10-16, symbol فولاد):

  POST /symbols/api/symbols/all          body {"hash":"null"}
      → {"symbols":[{"symbolIsin","symbolName",...,"buyCommission",...}]}
  POST /symbols/api/marketdata/v2        body ["<isin>", ...]
      → [{"symbolIsin","highAllowedPrice","lowAllowedPrice","closingPrice",
          "lastTradedPrice","closePrice","buyRatio":"0.003712",
          "minValidBuyVolume","maxValidBuyVolume","stateCode",...}]
  POST /core/api/v2/order
      → {"order":{"price":4540,"quantity":1,"side":0,"validityType":0,
          "createDateTime":"10/16/2026, 7:17:58 PM","commission":0.003712,
          "symbolIsin":"IRO1FOLD0001","symbolName":"فولاد",
          "orderModelType":1,"totalValue":4557,"orderFrom":34}}
      headers: authorization Bearer JWT (len ~1888), content-type json,
               accept-language fa, origin/referer d.easytrader.ir
      side: 0 = BUY (captured from the buy-ticket submit click)
      totalValue = round(price*quantity*(1+commission))   (4540*1.003712→4557)
  GET /core/api/order                    → {"orders":[...],"algoSuffixCharacters":...}
  GET /easy/api/account/server-time/{ms} → {"diff":852,"serverTimestamp":1792165672816}
  GET /easy/api/money
      → {"t0":1268425599,"buyPowerT0":1261209968,"buyPowerT1":1261209968,
         "block":0,"credit":0,...}         all amounts in RIAL (like prices)
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

API_BASE = "https://api-mts.orbis.easytrader.ir"

# Token harvest regex — matches "Bearer <jwt>" exactly as the app sends it.
_BEARER_RE = re.compile(r"^Bearer (.+)$", re.IGNORECASE)
_MIN_TOKEN_LEN = 64

READ_TIMEOUT_MS = 10_000

# ── Order submit throttle (measured live 2026-10-18) ────
# The broker rejects two order submissions closer than 300 ms apart, whichever
# side fired them, with a 2xx body carrying:
#   {"isSuccessful": false, "omsError": [{"code": 13021,
#     "name": "OrderIntervalError",
#     "error": "ارسال سفارش با فاصله کمتراز 300 میلی‌ثانیه مجاز نمی‌باشد"}]}
# Measured with the shipped gate (2026-10-18, 5 submits per setting):
#   300 ms spacing → 2 of 5 throttled (arrival jitter drops some pairs under
#                     the server's 300 ms comparison)
#   320 / 350 / 400 ms → clean
# So the hard floor is 320 ms, not the documented 300 ms, and the default
# burst spacing stays at 350 ms.
SUBMIT_INTERVAL_FLOOR_S = 0.32

# Fallback commission rate (فولاد capture). Real value is read per-symbol
# from marketdata/v2 buyRatio; this is only a last-ditch default.
DEFAULT_BUY_COMMISSION = 0.003712

SIDE_BUY = 0            # captured from the live buy-ticket submit
ORDER_MODEL_TYPE = 1    # captured literal
ORDER_FROM = 34         # captured literal
VALIDITY_TYPE_DAY = 0   # captured literal (day order)

# Tehran time for createDateTime — same wall clock as the browser
# (Iran has no DST since 2022; fixed +3:30).
TEHRAN_TZ = timezone(timedelta(hours=3, minutes=30))

# «قدرت خرید» per settlement day in the /easy/api/money body, best first:
# buyPowerT0 is what a T+0 buy can spend. t0 is the plain cash balance
# (مانده قابل معامله) — only a fallback, since blocks are not deducted from it.
_BUY_POWER_KEYS = ("buyPowerT0", "t0", "buyPowerT1", "t1", "buyPowerT2", "t2")


def buying_power(money: dict) -> int | None:
    """Buying power (RIAL) from an /easy/api/money body; None when absent."""
    if not isinstance(money, dict):
        return None
    for key in _BUY_POWER_KEYS:
        value = money.get(key)
        if isinstance(value, (int, float)):
            return int(value)
    return None


class EasyApiError(RuntimeError):
    """Any API problem the caller should treat as 'fall back to UI'."""


class ApiAuthError(EasyApiError):
    """HTTP 401 — the bearer token is expired or rejected. Caller should
    discard any cached token and log in again."""


# ── Trading session vs. order-registration window ──────
# The Tehran session runs 08:45:00–12:35:00. The OMS accepts NEW orders in
# 08:45–12:30 and says so in the 8702 text ("ساعت مجاز ثبت سفارش از 8:45 تا
# 12:30 است"), so the order window is what the burst has to respect.
ORDER_WINDOW_OPEN = (8, 45)
ORDER_WINDOW_CLOSE = (12, 30)
# OMS codes worth reacting to individually.
OMS_CODE_ORDER_INTERVAL = 13021   # "فاصله کمتر از 300 میلی‌ثانیه"
OMS_CODE_ORDER_WINDOW = 8702      # outside 08:45–12:30
# 11000 "وضعیت گروه اجازه فعالیت نمی‌دهد" — the symbol group is not active
# yet. Seen on a real-money run at 08:44:57, i.e. 3 s before the open: the
# session had not started, so the order did not reach the book. It is the
# same class of answer as 8702, not a reason to give up (measured 2026-10-18).
OMS_CODE_GROUP_INACTIVE = 11000
# Codes that mean "not open yet" — retry at the open, do not treat as a
# rejection of the order itself.
OMS_CODES_NOT_OPEN = frozenset({OMS_CODE_ORDER_WINDOW, OMS_CODE_GROUP_INACTIVE})
# 8006 "حداقل سفارش خرید ۵۰۰ هزار تومان است" — minimum order value. Repeat it
# and the answer is identical, so it stays a plain rejection (captured live
# 2026-10-18).
OMS_CODE_MIN_ORDER_VALUE = 8006


def parse_oms_error(resp) -> dict | None:
    """
    Extract the structured OMS error from an order response, or None.

    Live rejection shape (2026-10-18, market closed):

      {"isSuccessful": false, "id": "1121BnWUPNSsROa6",
       "message": "8702: ساعت مجاز ثبت سفارش از 8:45 تا 12:30 است.",
       "omsError": [{"error": "…", "code": 8702, "name": "OmsCustomError"}]}

    Returns {"code": int|None, "name": str, "message": str} or None when the
    body carries no error.
    """
    if not isinstance(resp, dict):
        return None
    # Live shape uses omsError; older/other bodies use errors/error — accept all.
    errs = resp.get("omsError") or resp.get("errors")
    first = errs[0] if isinstance(errs, list) and errs and isinstance(errs[0], dict) else None
    top = resp.get("message") or resp.get("error")
    if first is None and not top and resp.get("isSuccessful") is not False:
        return None
    code = None
    name = ""
    message = str(top or "")
    if first is not None:
        try:
            code = int(first.get("code"))
        except (TypeError, ValueError):
            code = None
        name = str(first.get("name") or "")
        message = str(first.get("error") or first.get("message") or message)
    if not isinstance(top, str) and not message:
        message = str(top or "")
    return {"code": code, "name": name, "message": message}


# ── Transports ──────────────────────────────────────────
class PlaywrightTransport:
    """Sends HTTP via Playwright's APIRequestContext (Chromium net stack)."""

    def __init__(self, api_request_context):
        # api_request_context = playwright BrowserContext.request
        self.ctx = api_request_context

    def request(self, *, method: str, url: str, headers: dict, json_body):
        try:
            if method == "GET":
                resp = self.ctx.get(url, headers=headers, timeout=READ_TIMEOUT_MS)
            else:
                data = json.dumps(json_body) if json_body is not None else None
                resp = self.ctx.post(url, headers=headers, data=data, timeout=READ_TIMEOUT_MS)
        except Exception as e:
            raise EasyApiError(f"{method} {url} transport error: {e.__class__.__name__}: {e}") from e
        return resp.status, resp.text()


class RequestsTransport:
    """Plain python-requests — fine for tests/mocks; live WAF may block it."""

    def __init__(self, session):
        self.session = session  # requests.Session

    def request(self, *, method: str, url: str, headers: dict, json_body, timeout=(5, 10)):
        try:
            kwargs = {"headers": headers, "timeout": timeout}
            if json_body is not None:
                kwargs["data"] = json.dumps(json_body)
            resp = self.session.request(method, url, **kwargs)
        except Exception as e:
            raise EasyApiError(f"{method} {url} transport error: {e.__class__.__name__}: {e}") from e
        return resp.status_code, resp.text()


class MockTransport:
    """Scripted responses for tests. `routes` maps (method, path_substring) → (status, text)."""

    def __init__(self, routes: dict, calls: list | None = None):
        self.routes = routes
        self.calls = calls if calls is not None else []

    def request(self, *, method: str, url: str, headers: dict, json_body):
        self.calls.append(
            {"method": method, "url": url, "headers": headers, "json_body": json_body}
        )
        for (m, frag), (status, text) in self.routes.items():
            if m == method and frag in url:
                if callable(text):
                    text = text()
                return status, text
        return 500, '{"error":"no route"}'


# ── Client ──────────────────────────────────────────────
class EasyApiClient:
    """
    One instance = one login session token + one transport.

    All methods raise EasyApiError on failure (timeouts, non-200, missing
    schema) so the orchestration layer can fall back to the UI path.
    """

    def __init__(
        self,
        token: str,
        transport,
        base_url: str = API_BASE,
        min_submit_interval: float = SUBMIT_INTERVAL_FLOOR_S,
        monotonic_fn=time.monotonic,
        sleep_fn=time.sleep,
    ):
        if not token:
            raise EasyApiError("Empty auth token.")
        self.base_url = base_url.rstrip("/")
        self.transport = transport
        self.token = token.strip()
        self._symbols_cache: list[dict] | None = None
        self._cache_lock = threading.Lock()
        # Broker rule: never two order POSTs closer than this (send-to-send).
        self.min_submit_interval = max(0.0, float(min_submit_interval))
        self._monotonic = monotonic_fn
        self._sleep = sleep_fn
        self._last_submit_at: float | None = None

    # ── headers ─────────────────────────────────────────────
    def _headers(self, *, content_type: bool = False) -> dict:
        """Exactly the captured header set (minus the noise).

        NOTE: content-type only on bodies — the WAF challenges GET requests
        that carry a content-type header (verified live 2026-10-16).
        """
        h = {
            "authorization": f"Bearer {self.token}",
            "accept": "application/json, text/plain, */*",
            "accept-language": "fa",
            "origin": "https://d.easytrader.ir",
            "referer": "https://d.easytrader.ir/",
        }
        if content_type:
            h["content-type"] = "application/json"
        return h

    def _url(self, path: str) -> str:
        return self.base_url + path

    def _request(self, method: str, path: str, *, json_body=None) -> dict | list:
        url = self._url(path)
        headers = self._headers(content_type=json_body is not None and method != "GET")
        status, text = self.transport.request(method=method, url=url, headers=headers, json_body=json_body)
        if status == 401:
            raise ApiAuthError(f"{method} {path} → 401 Unauthorized (token expired?).")
        if not (200 <= status < 300):
            snippet = (text or "")[:150].replace("\n", " ")
            if "waf" in snippet.lower() or "<html" in snippet.lower():
                raise EasyApiError(
                    f"{method} {path} → HTTP {status}: WAF/HTML challenge (transport is not "
                    f"browser-like — use PlaywrightTransport)."
                )
            raise EasyApiError(f"{method} {path} → HTTP {status}: {snippet}")
        try:
            return json.loads(text)
        except ValueError as e:
            snippet = (text or "")[:150].replace("\n", " ")
            if "<html" in snippet.lower():
                raise EasyApiError(
                    f"{method} {path} → HTML body (WAF challenge) with HTTP {status}"
                ) from e
            raise EasyApiError(f"{method} {path} → non-JSON body: {snippet}") from e

    # ── server clock ───────────────────────────────────────
    def server_timestamp(self) -> int:
        """Broker server time, epoch MILLISECONDS — the same clock the UI's
        broker clock shows, usable for precise timing decisions."""
        data = self._request("GET", f"/easy/api/account/server-time/{int(datetime.now().timestamp() * 1000)}")
        ts = data.get("serverTimestamp")
        if not ts:
            raise EasyApiError(f"server-time response missing serverTimestamp: {data!r}")
        return int(ts)

    # ── symbols ────────────────────────────────────────────
    def all_symbols(self, force: bool = False) -> list[dict]:
        """Full symbol table (name → isin map + commissions). Cached."""
        with self._cache_lock:
            if self._symbols_cache is not None and not force:
                return self._symbols_cache
            data = self._request("POST", "/symbols/api/symbols/all", json_body={"hash": "null"})
            symbols = data.get("symbols") if isinstance(data, dict) else None
            if not symbols:
                raise EasyApiError("symbols/all response has no 'symbols' list.")
            self._symbols_cache = symbols
            return symbols

    def find_symbol(self, name: str) -> dict:
        """Exact-name match for a symbol (Persian name or title)."""
        target = (name or "").strip()
        if not target:
            raise EasyApiError("Empty symbol name.")
        matches = [
            s for s in self.all_symbols()
            if (s.get("symbolName") or "").strip() == target
            or (s.get("title") or "").strip() == target
        ]
        if not matches:
            raise EasyApiError(
                f"Symbol {name!r} not found in symbols/all "
                f"({len(self.all_symbols())} symbols loaded)."
            )
        # Prefer active ones
        matches.sort(key=lambda s: not s.get("isActive", False))
        return matches[0]

    # ── market data ────────────────────────────────────────
    def market_data(self, isin: str) -> dict:
        """
        Live quote for one ISIN: highAllowedPrice (ceiling!), lowAllowedPrice,
        closingPrice, lastTradedPrice, buyRatio = buy commission rate, etc.
        """
        data = self._request("POST", "/symbols/api/marketdata/v2", json_body=[isin])
        if not isinstance(data, list) or not data:
            raise EasyApiError(f"marketdata/v2 returned no rows for {isin}.")
        for row in data:
            if row.get("symbolIsin") == isin:
                return row
        raise EasyApiError(f"marketdata/v2 missing isin {isin} in {len(data)} rows.")

    def buy_commission(self, md: dict) -> float:
        """Buy commission rate parsed from marketdata buyRatio (fallback: capture value)."""
        try:
            rate = float(md.get("buyRatio"))
        except (TypeError, ValueError):
            rate = DEFAULT_BUY_COMMISSION
        return rate

    # ── account money ──────────────────────────────────────
    def money(self) -> dict:
        """Balances in RIAL: buyPowerT0 = قدرت خرید, t0 = مانده قابل معامله
        (captured live 2026-10-16)."""
        data = self._request("GET", "/easy/api/money")
        if not isinstance(data, dict):
            raise EasyApiError(f"money response is not an object: {str(data)[:120]}")
        return data

    # ── order: submit ──────────────────────────────────────
    @staticmethod
    def build_buy_payload(
        *,
        isin: str,
        symbol_name: str,
        quantity: int,
        price: int,
        commission: float = DEFAULT_BUY_COMMISSION,
        now: datetime | None = None,
    ) -> dict:
        """
        Byte-faithful clone of the captured request:

        {"order":{"price":4540,"quantity":1,"side":0,"validityType":0,
          "createDateTime":"10/16/2026, 7:17:58 PM","commission":0.003712,
          "symbolIsin":"IRO1FOLD0001","symbolName":"فولاد",
          "orderModelType":1,"totalValue":4557,"orderFrom":34}}
        """
        if quantity <= 0:
            raise EasyApiError(f"quantity must be > 0 (got {quantity}).")
        if price <= 0:
            raise EasyApiError(f"price must be > 0 (got {price}).")
        if not isin:
            raise EasyApiError("symbolIsin empty.")
        n = now or datetime.now(TEHRAN_TZ)
        # Captured shape has NO leading zero on the 12-hour part: "10/16/2026, 7:17:58 PM"
        h12 = n.hour % 12 or 12
        create_date = (
            f"{n.month:02d}/{n.day:02d}/{n.year}, {h12}:{n.minute:02d}:{n.second:02d} "
            f"{'AM' if n.hour < 12 else 'PM'}"
        )
        total_value = round(price * quantity * (1 + commission))
        return {
            "order": {
                "price": price,
                "quantity": quantity,
                "side": SIDE_BUY,
                "validityType": VALIDITY_TYPE_DAY,
                "createDateTime": create_date,
                "commission": commission,
                "symbolIsin": isin,
                "symbolName": symbol_name,
                "orderModelType": ORDER_MODEL_TYPE,
                "totalValue": total_value,
                "orderFrom": ORDER_FROM,
            }
        }

    def _wait_submit_slot(self, interval: float) -> float:
        """Sleep until `interval` seconds have passed since the last submit.

        Measured send-to-send (monotonic), NOT response-to-send: the broker
        compares its own receive timestamps, and a response arrives ~60–110 ms
        after the request left, so sleeping after the response is unreliable.
        Returns the seconds actually slept (for logging/tests).
        """
        waited = 0.0
        if self._last_submit_at is not None and interval > 0:
            gap = self._monotonic() - self._last_submit_at
            if gap < interval:
                waited = interval - gap
                logger.debug("Order throttle: waiting %.0f ms before the next submit.", waited * 1000)
                self._sleep(waited)
        self._last_submit_at = self._monotonic()
        return waited

    def submit_buy(
        self,
        *,
        isin: str,
        symbol_name: str,
        quantity: int,
        price: int,
        commission: float = DEFAULT_BUY_COMMISSION,
        now: datetime | None = None,
        min_interval: float | None = None,
    ) -> dict | list:
        """POST one buy order, honouring the broker's 300 ms order interval.

        `min_interval` can raise the spacing but never lower it below the
        client's floor (`min_submit_interval`) — a sub-300 ms submit is
        rejected by the broker with code 13021 and would just waste a burst
        slot.
        """
        interval = self.min_submit_interval
        if min_interval is not None:
            interval = max(float(min_interval), self.min_submit_interval)
        self._wait_submit_slot(interval)
        # Built AFTER the wait so createDateTime is the send time.
        payload = self.build_buy_payload(
            isin=isin, symbol_name=symbol_name, quantity=quantity,
            price=price, commission=commission, now=now,
        )
        logger.info("Submitting buy via API: payload.order=%s", payload["order"])
        return self._request("POST", "/core/api/v2/order", json_body=payload)

    # ── order: verify ──────────────────────────────────────
    def list_orders(self) -> list[dict]:
        data = self._request("GET", "/core/api/order")
        orders = data.get("orders") if isinstance(data, dict) else None
        if orders is None:
            raise EasyApiError(f"order list response missing 'orders': {str(data)[:120]}")
        return orders

    def order_appeared(
        self,
        *,
        isin: str,
        quantity: int,
        price: int,
        baseline: list[dict] | None = None,
    ) -> bool:
        """
        True if a matching order row exists (and, given a baseline,
        the row count grew past it). Rows may be nested {"order":{...}}
        or flat — match on whichever carries the fields.
        """
        orders = self.list_orders()
        base_len = len(baseline) if baseline is not None else 0
        if len(orders) <= base_len:
            return False
        for o in orders:
            row = o.get("order") or o
            isin_f = row.get("symbolIsin") or row.get("isin") or o.get("symbolIsin")
            qty_f = row.get("quantity")
            price_f = row.get("price")
            if str(isin_f or "") == str(isin):
                if qty_f in (None, quantity) and price_f in (None, price, float(price)):
                    return True
        # Growth without a matching row: don't claim success on
        # partially-loaded data — False is the safe answer.
        return False


# ── Token harvest from an already-logged-in Playwright page ──
def extract_auth_token(page, timeout: float = 15.0) -> str:
    """
    Harvest the Bearer token from the page's own outbound requests — the web
    app always sends it on api-mts calls. Call right after a fresh Playwright
    login (the app fires API traffic immediately).

    `page` is a playwright.sync_api.Page.
    """
    found: dict = {}

    def _on_request(req):
        try:
            auth = (req.headers or {}).get("authorization")
            m = _BEARER_RE.match(auth.strip()) if auth else None
            if m and len(m.group(1)) >= _MIN_TOKEN_LEN:
                found["token"] = m.group(1)
        except Exception:
            pass

    try:
        page.on("request", _on_request)
    except Exception as e:
        raise EasyApiError(f"Can't attach token listener: {e}") from e

    try:
        if not found.get("token"):
            # Loaded app fires plenty of traffic; give it a moment.
            _wait_for(page, timeout)
        token = found.get("token")
        if not token:
            raise EasyApiError("No Authorization Bearer header seen on page requests.")
        return token
    finally:
        try:
            page.remove_listener("request", _on_request)
        except Exception:
            pass


def _wait_for(page, seconds: float):
    try:
        page.wait_for_timeout(int(seconds * 1000))
    except Exception:
        pass
