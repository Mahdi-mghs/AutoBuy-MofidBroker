"""
API-layer tests (ADR-004) — no browser, no network (same policy as ADR-003).

The fixtures below are the EXACT captured request/response pairs from the
2026-10-16 live capture (artifacts/api-report.md, redacted). The payload
test is byte-faithful: any change to build_buy_payload that disturbs the
captured shape fails loudly.

Covered:
- EasyApiClient.build_buy_payload == captured buy body (byte-faithful)
- createDateTime formatting (AM/PM, no leading-zero hour, 12h edge cases)
- resolve_price modes (ceiling/reference/fixed) + validation
- prepare_via_api semantics over a mocked transport
- execute_buy_api: single-submit success, retry-on-transport-error,
  no-duplicate stop on 2xx-but-unverified, rejection handling
- EasyApiClient._request: header rules (no content-type on GET; Bearer
  token; content-type on POST), 401 handling (ApiAuthError), WAF/HTML body
  detection
- API_BUY_MAX_ATTEMPTS caps the real-order burst
- order_appeared matching (nested + flat rows)
"""

import json
import time as time_mod
from datetime import datetime, timezone, timedelta

import pytest

import api_flow
import easy_api
from easy_api import (
    EasyApiClient,
    EasyApiError,
    MockTransport,
    TEHRAN_TZ,
)
from api_flow import (
    ApiFlowError,
    classify_submit_response,
    execute_buy_api,
    log_order_review,
    prepare_via_api,
    resolve_price,
)


# ── Captured fixtures (2026-10-16, فولاد) ───────────────
CAPTURED_ORDER_BODY = {
    "order": {
        "price": 4540,
        "quantity": 1,
        "side": 0,
        "validityType": 0,
        "createDateTime": "10/16/2026, 7:17:58 PM",
        "commission": 0.003712,
        "symbolIsin": "IRO1FOLD0001",
        "symbolName": "فولاد",
        "orderModelType": 1,
        "totalValue": 4557,
        "orderFrom": 34,
    }
}

CAPTURED_SYMBOLS_ALL = {
    "symbols": [
        {"symbolIsin": "IRO1FOLD0001", "symbolName": "فولاد", "title": "فولاد مباركه", "isActive": True, "buyCommission": "0.00036875"},
        {"symbolIsin": "IRT3KHMF0001", "symbolName": "خزانه ملت", "isActive": True},
    ]
}

CAPTURED_MARKETDATA = [
    {
        "symbolIsin": "IRO1FOLD0001",
        "closingPrice": 4540,
        "highAllowedPrice": 4540,
        "lowAllowedPrice": 4280,
        "lastTradedPrice": 4540,
        "buyRatio": "0.003712",
        "minValidBuyVolume": 1,
        "maxValidBuyVolume": 400000,
        "stateCode": "A",
    }
]

ISIN = "IRO1FOLD0001"
NAME = "فولاد"


def make_client(routes):
    return EasyApiClient("test-token", MockTransport(routes), min_submit_interval=0)


def default_routes(orders=None):
    return {
        ("POST", "/symbols/api/symbols/all"): (200, json.dumps(CAPTURED_SYMBOLS_ALL)),
        ("POST", "/symbols/api/marketdata/v2"): (200, json.dumps(CAPTURED_MARKETDATA)),
        ("GET", "/core/api/order"): (200, json.dumps({"orders": orders or []})),
    }


# ── Payload byte-faithfulness ──────────────────────────
def test_buy_payload_matches_capture_byte_for_byte():
    now = datetime(2026, 10, 16, 19, 17, 58, tzinfo=TEHRAN_TZ)
    payload = EasyApiClient.build_buy_payload(
        isin=ISIN, symbol_name=NAME, quantity=1, price=4540,
        commission=0.003712, now=now,
    )
    assert payload == CAPTURED_ORDER_BODY


def test_create_datetime_formatting():
    build = EasyApiClient.build_buy_payload
    d = lambda h, m=7, s=58: datetime(2026, 10, 16, h, m, s, tzinfo=TEHRAN_TZ)
    assert build(isin=ISIN, symbol_name=NAME, quantity=1, price=1, commission=0, now=d(19))["order"]["createDateTime"] == "10/16/2026, 7:07:58 PM"
    assert build(isin=ISIN, symbol_name=NAME, quantity=1, price=1, commission=0, now=d(1))["order"]["createDateTime"] == "10/16/2026, 1:07:58 AM"
    # Midnight edge: hour 0 → 12 AM; noon: hour 12 → 12 PM
    assert build(isin=ISIN, symbol_name=NAME, quantity=1, price=1, commission=0, now=d(0))["order"]["createDateTime"] == "10/16/2026, 12:07:58 AM"
    assert build(isin=ISIN, symbol_name=NAME, quantity=1, price=1, commission=0, now=d(12))["order"]["createDateTime"] == "10/16/2026, 12:07:58 PM"


def test_buy_payload_validations():
    build = EasyApiClient.build_buy_payload
    with pytest.raises(EasyApiError):
        build(isin=ISIN, symbol_name=NAME, quantity=0, price=100)
    with pytest.raises(EasyApiError):
        build(isin=ISIN, symbol_name=NAME, quantity=1, price=0)
    with pytest.raises(EasyApiError):
        build(isin="", symbol_name=NAME, quantity=1, price=100)


def test_total_value_rounds_like_capture():
    now = datetime(2026, 10, 16, 12, 0, 0, tzinfo=TEHRAN_TZ)
    p = EasyApiClient.build_buy_payload(isin=ISIN, symbol_name=NAME, quantity=3, price=1000, commission=0.003712, now=now)
    assert p["order"]["totalValue"] == round(3 * 1000 * 1.003712)


# ── resolve_price ──────────────────────────────────────
MD = CAPTURED_MARKETDATA[0]


def test_resolve_price_ceiling_uses_high_allowed():
    assert resolve_price(MD, "ceiling", 0) == 4540


def test_resolve_price_reference_uses_closing():
    assert resolve_price(MD, "reference", 0) == 4540


def test_resolve_price_fixed():
    assert resolve_price(MD, "fixed", 5000) == 5000
    with pytest.raises(ApiFlowError):
        resolve_price(MD, "fixed", 0)


def test_resolve_price_missing_fields():
    with pytest.raises(ApiFlowError):
        resolve_price({"highAllowedPrice": 0}, "ceiling", 0)
    with pytest.raises(ApiFlowError):
        resolve_price(MD, "bogus", 0)


# ── prepare_via_api ────────────────────────────────────
def test_prepare_via_api_happy_path():
    client = make_client(default_routes())
    summary = prepare_via_api(client, NAME, quantity=10, price_mode="ceiling")
    assert summary["isin"] == ISIN
    assert summary["quantity"] == 10
    assert summary["price"] == 4540
    assert summary["commission"] == pytest.approx(0.003712)


def test_prepare_via_api_quantity_bounds():
    client = make_client(default_routes())
    with pytest.raises(ApiFlowError):
        prepare_via_api(client, NAME, quantity=0)
    with pytest.raises(ApiFlowError):
        prepare_via_api(client, NAME, quantity=1_000_000)  # > maxValidBuyVolume


def test_prepare_via_api_unknown_symbol():
    routes = default_routes()
    routes[("POST", "/symbols/api/marketdata/v2")] = (200, "[]")
    client = make_client(routes)
    with pytest.raises(ApiFlowError):
        prepare_via_api(client, "نجیب", quantity=1)


# ── Account money / buying power (قدرت خرید) ───────────
CAPTURED_MONEY = {
    "t0": 1268425599,
    "t1": 1261209968,
    "t2": 1261209968,
    "buyPowerT0": 1261209968,
    "buyPowerT1": 1261209968,
    "buyPowerT2": 1261209968,
    "blockT2": 0,
    "withdrawBlockT2": 0,
    "marginBlock": 0,
    "credit": 0,
    "avandCredit": 0,
    "walletWithdrawBalanceT0": 1261209968,
    "warrantValueCredit": 0,
    "hamiBalance": 1268340605,
    "block": 0,
}


def test_money_returns_the_captured_balance_body():
    routes = default_routes()
    routes[("GET", "/easy/api/money")] = (200, json.dumps(CAPTURED_MONEY))
    client = make_client(routes)
    assert client.money() == CAPTURED_MONEY
    assert easy_api.buying_power(client.money()) == 1261209968


def test_buying_power_falls_back_to_cash_then_settlement_days():
    assert easy_api.buying_power({"buyPowerT1": 5, "t1": 9}) == 5
    assert easy_api.buying_power({"t1": 9, "t2": 7}) == 9  # no buyPowerT0 at all
    assert easy_api.buying_power({}) is None
    assert easy_api.buying_power(None) is None


def test_prepare_via_api_reads_the_buying_power_and_order_value():
    routes = default_routes()
    routes[("GET", "/easy/api/money")] = (200, json.dumps(CAPTURED_MONEY))
    summary = prepare_via_api(make_client(routes), NAME, quantity=10)
    assert summary["buying_power"] == 1261209968
    assert summary["order_value"] == round(4540 * 10 * 1.003712)


def test_prepare_via_api_warns_when_the_order_exceeds_the_balance(caplog):
    routes = default_routes()
    routes[("GET", "/easy/api/money")] = (200, json.dumps({"buyPowerT0": 1_000}))
    with caplog.at_level("WARNING", logger="api_flow"):
        summary = prepare_via_api(make_client(routes), NAME, quantity=10)
    assert summary["order_value"] > summary["buying_power"]
    assert any("exceeds the buying power" in r.message for r in caplog.records)


def test_prepare_via_api_survives_an_unavailable_balance():
    # No /easy/api/money route → MockTransport answers 500 → balance unknown.
    summary = prepare_via_api(make_client(default_routes()), NAME, quantity=1)
    assert "buying_power" not in summary
    assert summary["order_value"] == round(4540 * 1.003712)


def test_order_review_flags_a_shortfall(caplog, tmp_path):
    known = dict(_prepared(), order_value=4_557, buying_power=4_000)
    with caplog.at_level("WARNING", logger="api_flow"):
        assert log_order_review(known) is False
    assert any("SHORT by" in r.message for r in caplog.records)

    covered = dict(_prepared(), order_value=4_557, buying_power=1_000_000)
    assert log_order_review(covered) is True

    unknown = _prepared()  # no balance read at all (e.g. UI-prepared order)
    assert log_order_review(unknown) is True
    assert unknown["order_value"] == round(4540 * 1.003712)


# ── execute_buy_api ────────────────────────────────────
def _prepared(client=None):
    return {
        "symbol": NAME,
        "isin": ISIN,
        "quantity": 1,
        "price": 4540,
        "price_mode": "ceiling",
        "commission": 0.003712,
        "client": client,
    }


class FakeClock:
    """Monotonically ticking clock so polling deadlines terminate in tests."""

    def __init__(self, step=1.0):
        self.t = 0.0
        self.step = step

    def __call__(self):
        self.t += self.step
        return self.t


def test_execute_buy_api_success_single_submit(monkeypatch):
    burst_row = {"order": {"symbolIsin": ISIN, "quantity": 1, "price": 4540}}
    # GET /core/api/order grows from 0 → 1 after the submit
    responses = iter([json.dumps({"orders": []}), json.dumps({"orders": [burst_row]})])
    routes = {
        ("POST", "/symbols/api/symbols/all"): (200, json.dumps(CAPTURED_SYMBOLS_ALL)),
        ("POST", "/core/api/v2/order"): (200, json.dumps({})),
        ("GET", "/core/api/order"): (200, lambda: next(responses)),
    }
    calls = []
    transport = MockTransport(routes, calls)
    client = EasyApiClient("tok", transport, min_submit_interval=0)
    monkeypatch.setattr(api_flow.time, "sleep", lambda s: None)
    assert execute_buy_api(client, _prepared(), timeout_fn=FakeClock())
    submissions = [c for c in calls if "/core/api/v2/order" in c["url"]]
    assert len(submissions) == 1  # exactly ONE order risk, no duplicates
    assert submissions[0]["json_body"] == CAPTURED_ORDER_BODY or \
        submissions[0]["json_body"]["order"]["symbolIsin"] == ISIN


def test_execute_buy_api_retries_on_transport_errors(monkeypatch):
    attempts = {"n": 0}

    class Flaky:
        def request(self, *, method, url, headers, json_body):
            if "/core/api/v2/order" in url:
                attempts["n"] += 1
                raise EasyApiError("boom")
            if "/core/api/order" in url:
                return 200, json.dumps({"orders": []})
            return 200, "{}"

    client = EasyApiClient("tok", Flaky(), min_submit_interval=0)
    monkeypatch.setattr(api_flow.time, "sleep", lambda s: None)
    monkeypatch.setattr(api_flow, "API_BUY_MAX_ATTEMPTS", 3)
    result = execute_buy_api(client, _prepared(), timeout_fn=FakeClock())
    assert result is False
    assert attempts["n"] == 3


def test_execute_buy_api_rejection_stops_immediately(monkeypatch):
    calls = []

    class Rejecting:
        def request(self, *, method, url, headers, json_body):
            calls.append(url)
            if "/core/api/v2/order" in url:
                return 200, json.dumps({"errors": [{"code": 1, "message": "موجودی کافی نیست"}]})
            return 200, json.dumps({"orders": []})

    client = EasyApiClient("tok", Rejecting(), min_submit_interval=0)
    monkeypatch.setattr(api_flow.time, "sleep", lambda s: None)
    monkeypatch.setattr(api_flow, "API_BUY_MAX_ATTEMPTS", 5)
    assert execute_buy_api(client, _prepared(), timeout_fn=FakeClock()) is False
    order_posts = [u for u in calls if "/core/api/v2/order" in u]
    assert len(order_posts) == 1  # rejection visible → no blind re-submitting


def test_execute_buy_api_2xx_unverified_stops(monkeypatch):
    """2xx but the order never appears → STOP (no duplicates), return False."""
    submits = {"n": 0}

    class NeverVerify:
        def request(self, *, method, url, headers, json_body):
            if "/core/api/v2/order" in url:
                submits["n"] += 1
                return 200, json.dumps({})
            return 200, json.dumps({"orders": []})

    client = EasyApiClient("tok", NeverVerify(), min_submit_interval=0)
    monkeypatch.setattr(api_flow.time, "sleep", lambda s: None)
    assert execute_buy_api(client, _prepared(), timeout_fn=FakeClock()) is False
    assert submits["n"] == 1


# ── Transport/header rules ─────────────────────────────
def test_get_has_no_content_type_post_has():
    calls = []
    client = EasyApiClient("tok", MockTransport(default_routes(), calls), min_submit_interval=0)

    client.server_timestamp = lambda: client._request("GET", "/easy/api/account/server-time/123")
    try:
        client.all_symbols()
    except EasyApiError:
        pass
    gets = [c for c in calls if c["method"] == "GET"]
    posts = [c for c in calls if c["method"] == "POST"]
    for g in gets:
        assert "content-type" not in g["headers"], "GET must NOT carry content-type (WAF challenges it)"
    for p in posts:
        assert p["headers"].get("content-type") == "application/json"
        assert p["headers"]["authorization"].startswith("Bearer tok")
        assert p["headers"]["origin"] == "https://d.easytrader.ir"


def test_waf_html_body_raises_helpful_error():
    routes = default_routes()
    routes[("POST", "/symbols/api/symbols/all")] = (200, "<html lang=\"en\"><title>WAF</title></html>")
    client = make_client(routes)
    with pytest.raises(EasyApiError) as exc:
        client.all_symbols()
    assert "WAF" in str(exc.value)


def test_401_raises():
    routes = default_routes()
    routes[("POST", "/symbols/api/symbols/all")] = (401, "")
    client = make_client(routes)
    with pytest.raises(EasyApiError) as exc:
        client.all_symbols()
    assert "401" in str(exc.value)


def test_401_is_auth_error_subclass():
    """main() relies on this type to know it must drop the cached token."""
    routes = default_routes()
    routes[("GET", "/core/api/order")] = (401, "")
    client = make_client(routes)
    with pytest.raises(easy_api.ApiAuthError):
        client.list_orders()


def test_api_burst_defaults_cover_the_queue_race():
    """20 open-time attempts (~7 s at 350 ms) is what reaches the front of the
    buy queue; pre-open refusals have their own, separate budget."""
    import config

    assert config.API_BUY_MAX_ATTEMPTS >= 20
    assert config.API_PREOPEN_MAX_ATTEMPTS >= 20
    assert config.API_PREOPEN_MAX_ATTEMPTS * config.API_BUY_RETRY_INTERVAL >= 8


# ── order_appeared matching ────────────────────────────
def test_order_appeared_nested_and_flat_rows():
    routes = default_routes()

    def case(row, baseline_rows):
        r = default_routes()
        r[("GET", "/core/api/order")] = (200, json.dumps({"orders": baseline_rows + [row]}))
        c = make_client(r)
        return c.order_appeared(isin=ISIN, quantity=1, price=4540, baseline=baseline_rows)

    nested = {"order": {"symbolIsin": ISIN, "quantity": 1, "price": 4540}}
    flat = {"symbolIsin": ISIN, "quantity": 1, "price": 4540}
    assert case(nested, []) is True
    assert case(flat, []) is True
    assert case({"order": {"symbolIsin": "OTHER0000", "quantity": 1, "price": 4540}}, []) is False
    # no growth vs baseline → False
    r = default_routes()
    r[("GET", "/core/api/order")] = (200, json.dumps({"orders": [flat]}))
    c = make_client(r)
    assert c.order_appeared(isin=ISIN, quantity=1, price=4540, baseline=[flat]) is False


# ── Broker throttle: the 300 ms order-interval rule (live 2026-10-18) ──
# These two bodies are verbatim captures from POST /core/api/v2/order while the
# market was closed. Every submit returns HTTP 2xx — the verdict is the body.
CAPTURED_THROTTLED = {
    "isSuccessful": False,
    "id": "1121BnWUPNq>k!5M",
    "message": "13021: ارسال سفارش با فاصله کمتراز 300 میلی‌ثانیه مجاز نمی‌باشد",
    "omsError": [{
        "name": "OrderIntervalError",
        "error": "ارسال سفارش با فاصله کمتراز 300 میلی‌ثانیه مجاز نمی‌باشد",
        "code": 13021,
    }],
}

CAPTURED_WINDOW_CLOSED = {
    "isSuccessful": False,
    "id": "1121BnWUPNSsROa6",
    "message": "8702: ساعت مجاز ثبت سفارش از 8:45 تا 12:30 است.",
    "omsError": [{
        "error": "ساعت مجاز ثبت سفارش از 8:45 تا 12:30 است.",
        "code": 8702,
        "name": "OmsCustomError",
    }],
}

# 08:44:57 on a real-money run (2026-10-18) — 3 s before the session opened.
# The old classifier called this a rejection and gave up on attempt 1.
CAPTURED_GROUP_INACTIVE = {
    "isSuccessful": False,
    "id": "1121BnWUPNpre1",
    "message": "11000: وضعیت گروه اجازه فعالیت نمی‌دهد",
    "omsError": [{
        "error": "وضعیت گروه اجازه فعالیت نمی‌دهد",
        "code": 11000,
        "name": "OmsCustomError",
    }],
}

# Inside the session, 1 share of فولاد is below the 500 000 Toman minimum:
# a real, permanent refusal — retrying it is pointless (captured 2026-10-18).
CAPTURED_MIN_ORDER_VALUE = {
    "isSuccessful": False,
    "id": "1121BnWUPNLYK(sW",
    "message": "8006: حداقل سفارش خرید ۵۰۰ هزار تومان است.",
    "omsError": [{
        "error": "حداقل سفارش خرید ۵۰۰ هزار تومان است.",
        "code": 8006,
        "name": "OmsCustomError",
    }],
}


def test_classify_captured_throttle_body():
    out = classify_submit_response(CAPTURED_THROTTLED)
    assert out.kind == "throttled"
    assert out.code == 13021
    assert "300" in out.message


def test_classify_captured_window_body():
    out = classify_submit_response(CAPTURED_WINDOW_CLOSED)
    assert out.kind == "not_open"
    assert out.code == 8702


def test_classify_captured_group_inactive_body_is_not_open():
    """11000 before 08:45 must NOT read as a rejection (real-money run)."""
    out = classify_submit_response(CAPTURED_GROUP_INACTIVE)
    assert out.kind == "not_open"
    assert out.code == 11000
    assert "اجازه فعالیت" in out.message


def test_classify_not_open_wording_without_a_known_code():
    """If the OMS renumbers, the Persian wording still identifies the state."""
    body = {"isSuccessful": False, "message": "وضعیت گروه اجازه فعالیت نمی‌دهد"}
    assert classify_submit_response(body).kind == "not_open"


def test_classify_min_order_value_is_a_plain_rejection():
    """8006 (500 000 Toman minimum) repeats identically — stop, don't retry."""
    out = classify_submit_response(CAPTURED_MIN_ORDER_VALUE)
    assert out.kind == "rejected"
    assert out.code == 8006


def test_classify_success_with_informational_message_is_accepted():
    """A success body may still carry a message — it must NOT read as a rejection."""
    out = classify_submit_response({"isSuccessful": True, "message": "سفارش با موفقیت ثبت شد"})
    assert out.kind == "accepted"


def test_classify_errorless_body_is_accepted():
    assert classify_submit_response({}).kind == "accepted"


def test_classify_other_oms_error_is_rejection():
    body = {"isSuccessful": False, "message": "1: موجودی کافی نیست",
            "omsError": [{"error": "موجودی کافی نیست", "code": 16001, "name": "OmsCustomError"}]}
    out = classify_submit_response(body)
    assert out.kind == "rejected"
    assert out.code == 16001


def test_parse_oms_error_none_for_clean_body():
    assert easy_api.parse_oms_error({"isSuccessful": True}) is None


# ── Submit gate: never two order POSTs closer than 300 ms ──
class FakeMonotonic:
    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def _submit(client, n=1):
    now = datetime(2026, 10, 18, 9, 0, 0, tzinfo=TEHRAN_TZ)
    for _ in range(n):
        client.submit_buy(isin=ISIN, symbol_name=NAME, quantity=1, price=4540,
                          commission=0.003712, now=now)


def test_submit_gate_spaces_consecutive_orders():
    clock = FakeMonotonic()
    client = EasyApiClient(
        "tok",
        MockTransport({("POST", "/core/api/v2/order"): (200, json.dumps({}))}),
        min_submit_interval=0.30,
        monotonic_fn=clock,
        sleep_fn=clock.sleep,
    )
    _submit(client, n=3)
    assert len(clock.sleeps) == 2                       # first submit waits for nothing
    for s in clock.sleeps:
        assert s == pytest.approx(0.30, abs=1e-6)
    # send-to-send spacing is exactly the floor
    assert client._last_submit_at == pytest.approx(1000.0 + 0.60)


def test_submit_gate_cannot_go_below_the_broker_floor():
    """A 100 ms burst is refused by the broker (13021) — the gate must raise it."""
    clock = FakeMonotonic()
    client = EasyApiClient(
        "tok",
        MockTransport({("POST", "/core/api/v2/order"): (200, json.dumps({}))}),
        min_submit_interval=0.30,
        monotonic_fn=clock,
        sleep_fn=clock.sleep,
    )
    _submit(client, n=2)
    client.submit_buy(isin=ISIN, symbol_name=NAME, quantity=1, price=4540,
                      commission=0, min_interval=0.05)
    assert clock.sleeps[-1] == pytest.approx(0.30, abs=1e-6)


def test_submit_gate_accepts_a_higher_interval():
    clock = FakeMonotonic()
    client = EasyApiClient(
        "tok",
        MockTransport({("POST", "/core/api/v2/order"): (200, json.dumps({}))}),
        min_submit_interval=0.30,
        monotonic_fn=clock,
        sleep_fn=clock.sleep,
    )
    client.submit_buy(isin=ISIN, symbol_name=NAME, quantity=1, price=4540,
                      commission=0, min_interval=0.5)
    client.submit_buy(isin=ISIN, symbol_name=NAME, quantity=1, price=4540,
                      commission=0, min_interval=0.5)
    assert clock.sleeps[-1] == pytest.approx(0.5, abs=1e-6)


def test_config_intervals_respect_the_broker_rule():
    import config

    # The floor is the MEASURED safe spacing, not the documented 300 ms.
    assert config.API_MIN_SUBMIT_INTERVAL >= easy_api.SUBMIT_INTERVAL_FLOOR_S
    assert config.API_BUY_RETRY_INTERVAL >= config.API_MIN_SUBMIT_INTERVAL
    assert easy_api.SUBMIT_INTERVAL_FLOOR_S > 0.30


# ── Burst behaviour against the throttle / order window ──
def _no_delay(monkeypatch):
    monkeypatch.setattr(api_flow.time, "sleep", lambda s: None)
    monkeypatch.setattr(api_flow, "API_BUY_RETRY_INTERVAL", 0.0)
    monkeypatch.setattr(api_flow, "API_MIN_SUBMIT_INTERVAL", 0.0)


def test_throttled_submit_does_not_burn_the_attempt_budget(monkeypatch):
    """13021 means the order never reached the book: retry is free."""
    bodies = iter([CAPTURED_THROTTLED, CAPTURED_THROTTLED, {"isSuccessful": True}])
    posts = {"n": 0}

    class Throttling:
        def request(self, *, method, url, headers, json_body):
            if "/core/api/v2/order" in url:
                posts["n"] += 1
                return 200, json.dumps(next(bodies))
            return 200, json.dumps({"orders": []})

    client = EasyApiClient("tok", Throttling(), min_submit_interval=0)
    _no_delay(monkeypatch)
    monkeypatch.setattr(api_flow, "VERIFY_ORDER", False)
    monkeypatch.setattr(api_flow, "API_BUY_MAX_ATTEMPTS", 1)   # budget of ONE real order

    assert execute_buy_api(client, _prepared(), timeout_fn=FakeClock()) is True
    assert posts["n"] == 3        # two throttled + the accepted one


def test_preopen_refusal_is_retried_not_treated_as_a_rejection(monkeypatch):
    """08:44:57 → 11000 → retried through the open → accepted.

    This is the real-money bug: the burst must survive pre-open refusals and
    still have its whole order budget available at 08:45:00.
    """
    now = {"t": datetime(2026, 10, 18, 8, 44, 57, tzinfo=TEHRAN_TZ)}
    posts = {"n": 0}
    bodies = iter([
        CAPTURED_GROUP_INACTIVE, CAPTURED_GROUP_INACTIVE, CAPTURED_WINDOW_CLOSED,
        {"isSuccessful": True},
    ])

    class Opening:
        def request(self, *, method, url, headers, json_body):
            if "/core/api/v2/order" in url:
                posts["n"] += 1
                now["t"] += timedelta(milliseconds=350)   # the burst advances time
                return 200, json.dumps(next(bodies))
            return 200, json.dumps({"orders": []})

    client = EasyApiClient("tok", Opening(), min_submit_interval=0)
    _no_delay(monkeypatch)
    monkeypatch.setattr(api_flow, "VERIFY_ORDER", False)
    monkeypatch.setattr(api_flow, "API_BUY_MAX_ATTEMPTS", 1)   # ONE real order only

    assert execute_buy_api(
        client, _prepared(), timeout_fn=FakeClock(), now_fn=lambda: now["t"]
    ) is True
    assert posts["n"] == 4        # 3 pre-open refusals + the accepted submit


def test_not_open_after_the_close_stops_immediately(monkeypatch):
    """After 12:30 the window is gone: one submit, hard stop, no retry loop."""
    posts = {"n": 0}

    class Closed:
        def request(self, *, method, url, headers, json_body):
            if "/core/api/v2/order" in url:
                posts["n"] += 1
                return 200, json.dumps(CAPTURED_WINDOW_CLOSED)
            return 200, json.dumps({"orders": []})

    client = EasyApiClient("tok", Closed(), min_submit_interval=0)
    _no_delay(monkeypatch)
    late = lambda: datetime(2026, 10, 18, 13, 0, 0, tzinfo=TEHRAN_TZ)
    assert execute_buy_api(client, _prepared(), timeout_fn=FakeClock(), now_fn=late) is False
    assert posts["n"] == 1


def test_window_state_helper():
    md = lambda h, m: datetime(2026, 10, 18, h, m, tzinfo=TEHRAN_TZ)
    assert api_flow.window_state(lambda: md(8, 0)) == "early"
    assert api_flow.window_state(lambda: md(9, 0)) == "open"
    assert api_flow.window_state(lambda: md(13, 0)) == "late"
    assert api_flow.window_state(None) == "unknown"
    assert api_flow.seconds_until_window_open(lambda: md(8, 44)) == pytest.approx(60.0)


def test_window_helpers_accept_time_only_clock():
    """ApiClock() -> datetime.time (the broker-wait contract), not a datetime."""
    from datetime import time as dtime

    assert api_flow.window_state(lambda: dtime(8, 30)) == "early"
    assert api_flow.seconds_until_window_open(lambda: dtime(8, 44, 58)) == pytest.approx(2.0)


def test_preopen_refusals_are_bounded(monkeypatch):
    """Holiday / 08:30 target: stop after API_PREOPEN_MAX_ATTEMPTS, never loop."""
    posts = {"n": 0}

    class Closed:
        def request(self, *, method, url, headers, json_body):
            if "/core/api/v2/order" in url:
                posts["n"] += 1
                return 200, json.dumps(CAPTURED_WINDOW_CLOSED)
            return 200, json.dumps({"orders": []})

    client = EasyApiClient("tok", Closed(), min_submit_interval=0)
    _no_delay(monkeypatch)
    monkeypatch.setattr(api_flow, "API_PREOPEN_MAX_ATTEMPTS", 4)
    early = lambda: datetime(2026, 10, 18, 8, 30, 0, tzinfo=TEHRAN_TZ)
    assert execute_buy_api(client, _prepared(), timeout_fn=FakeClock(), now_fn=early) is False
    assert posts["n"] == 4        # bounded: no attempt budget consumed, no loop


def test_preopen_retry_works_with_a_time_only_clock(monkeypatch):
    """Regression: ApiClock() returns a bare `time`; the pre-open path must fire."""
    from datetime import time as dtime

    bodies = iter([CAPTURED_WINDOW_CLOSED, {"isSuccessful": True}])
    posts = {"n": 0}

    class Opening:
        def request(self, *, method, url, headers, json_body):
            if "/core/api/v2/order" in url:
                posts["n"] += 1
                return 200, json.dumps(next(bodies))
            return 200, json.dumps({"orders": []})

    client = EasyApiClient("tok", Opening(), min_submit_interval=0)
    _no_delay(monkeypatch)
    monkeypatch.setattr(api_flow, "VERIFY_ORDER", False)
    assert execute_buy_api(
        client, _prepared(), timeout_fn=FakeClock(), now_fn=lambda: dtime(8, 44, 58)
    ) is True
    assert posts["n"] == 2
