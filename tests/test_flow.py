"""
Orchestration + timing unit tests — no browser, no network (ADR-003).

What is covered here:
- broker.wait_until_broker_time timing/guard logic (fake clock injected)
- broker.is_order_accepted notification classification
- order._read_number price/quantity parsing
- main() flow wiring with stubbed login/prepare/execute_buy
- CLI argument parsing
"""

import builtins
import logging
from datetime import time as dt_time

import pytest

import broker
import main as main_mod
from api_flow import ApiFlowError
from easy_api import ApiAuthError
from order import _read_number


# ────────────────────────────────────────────────────────
# broker.wait_until_broker_time
# ────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def fast_clock(monkeypatch):
    """Keep clock polling instant in tests."""
    monkeypatch.setattr(broker, "CLOCK_POLL_INTERVAL", 0.001)


def test_wait_reaches_target_and_returns_time(monkeypatch):
    seq = iter([dt_time(8, 44, 50), dt_time(8, 44, 57), dt_time(8, 44, 58)])

    def fake_clock():
        return next(seq)

    reached = broker.wait_until_broker_time(None, dt_time(8, 44, 58), now_fn=fake_clock)
    assert reached == dt_time(8, 44, 58)


def test_wait_accepts_target_already_passed_and_still_valid(monkeypatch):
    # clock 15:30:00 is exactly the last valid moment (MARKET_LATEST)
    reached = broker.wait_until_broker_time(
        None, dt_time(8, 44, 58), now_fn=lambda: dt_time(15, 30, 0)
    )
    assert reached == dt_time(15, 30, 0)


def test_wait_raises_timeout_when_deadline_exceeded(monkeypatch):
    monkeypatch.setattr(broker, "CLOCK_DEADLINE_SECONDS", 0)
    with pytest.raises(TimeoutError):
        broker.wait_until_broker_time(None, dt_time(8, 44, 58), now_fn=lambda: dt_time(8, 0, 0))


def test_wait_rejects_after_hours_clock(monkeypatch):
    """15:30:01 must never satisfy the condition, even with target passed."""
    monkeypatch.setattr(broker, "CLOCK_DEADLINE_SECONDS", 1)
    with pytest.raises(TimeoutError):
        broker.wait_until_broker_time(
            None, dt_time(8, 44, 58), now_fn=lambda: dt_time(15, 30, 1)
        )


def test_wait_raises_when_clock_unreadable():
    def broken():
        raise ValueError("no clock element")

    with pytest.raises(TimeoutError):
        broker.wait_until_broker_time(None, dt_time(8, 44, 58), now_fn=broken)


# ────────────────────────────────────────────────────────
# broker.is_order_accepted
# ────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "message,expected",
    [
        ("سفارش با موفقیت ثبت شد", True),      # successfully registered
        ("سفارش شما پذیرفته شد", True),         # accepted
        ("Order accepted by broker", True),
        ("submitted", True),
        ("خطا در ثبت سفارش", False),            # error
        ("سفارش ناموفق بود", False),            # unsuccessful
        ("Order rejected: invalid price", False),
        ("(no notification found)", False),
        ("سایر پیام‌های ناشناخته", False),       # unclassified → not success
    ],
)
def test_notification_classification(message, expected):
    assert broker.is_order_accepted(message) is expected


# ────────────────────────────────────────────────────────
# order._read_number
# ────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "text,expected",
    [("4,540", 4540), ("10,000", 10000), ("186,355", 186355), ("0", 0), ("", 0), ("—", 0)],
)
def test_read_number_parses_formatted_values(text, expected):
    assert _read_number(text) == expected


# ────────────────────────────────────────────────────────
# main() orchestration (stubbed buyer)
# ────────────────────────────────────────────────────────
@pytest.fixture
def calls():
    return []


@pytest.fixture
def stub_main(monkeypatch, calls):
    """Stub every side-effecting dependency of main()."""
    import main as m

    monkeypatch.setattr(m, "setup_logging", lambda: None)
    monkeypatch.setattr(m, "cleanup_all", lambda: calls.append("cleanup"))
    monkeypatch.setattr(m, "initialize_driver", lambda: (calls.append("driver"), object())[1])
    monkeypatch.setattr(m, "login", lambda page: (calls.append("login"), True)[1])
    monkeypatch.setattr(m, "dismiss_announcement", lambda page: calls.append("dismiss"))
    monkeypatch.setattr(
        m,
        "prepare_order",
        lambda page, symbol=None, **kw: (
            calls.append(("prepare", symbol))
            or {"symbol": symbol or "", "quantity": 1, "price": 4540, "price_mode": "ceiling"}
        ),
    )
    monkeypatch.setattr(m, "find_buy_button", lambda page, timeout=5: object())
    monkeypatch.setattr(m, "is_time_already_passed", lambda page, target, logger, **kw: False)
    monkeypatch.setattr(
        m, "wait_until_broker_time", lambda page, target, **kw: calls.append(("wait", target))
    )
    # Cached-token fast path (ADR-005) stays out of the way unless a test opts in.
    monkeypatch.setattr(m, "TOKEN_CACHE", False)
    monkeypatch.setattr(m, "_try_resume_api", lambda page, symbol, logger: None)
    monkeypatch.setattr(m, "_ui_logged_in", lambda page, timeout_ms=3000: True)
    monkeypatch.setattr(
        m, "execute_buy", lambda page: (calls.append("buy"), True)[1]
    )
    monkeypatch.setattr(m.time, "sleep", lambda s: None)  # skip the 10s grace loop
    monkeypatch.setattr(m, "USERNAME", "u")
    monkeypatch.setattr(m, "PASSWORD", "p")
    monkeypatch.setattr(m, "QUANTITY", 5)
    monkeypatch.setattr(m, "_shutdown_requested", False)
    return m


def test_auto_flow_prepares_then_waits_then_buys(stub_main, calls):
    stub_main.main(["--time", "08:44:58", "--symbol", "فولاد"])

    assert ("prepare", "فولاد") in calls
    assert "buy" in calls
    # order matters: preparation strictly before the clock wait, buy last
    assert calls.index(("prepare", "فولاد")) < calls.index(
        ("wait", dt_time(8, 44, 58))
    ) < calls.index("buy")
    assert calls[-1] == "cleanup"


def test_manual_mode_pauses_instead_of_preparing(stub_main, calls, monkeypatch):
    inputs = []
    monkeypatch.setattr(builtins, "input", lambda prompt="": inputs.append(prompt) or "")
    monkeypatch.setattr(stub_main, "QUANTITY", 0)  # must be tolerated in manual mode

    stub_main.main(["--time", "08:44:58", "--manual"])

    assert len(inputs) == 1                      # the pause happened exactly once
    assert not any(isinstance(c, tuple) and c[0] == "prepare" for c in calls)
    assert "buy" in calls


def test_expired_target_time_exits_before_buying(stub_main, calls, monkeypatch):
    monkeypatch.setattr(stub_main, "is_time_already_passed", lambda page, t, log, **kw: True)

    with pytest.raises(SystemExit) as exc:
        stub_main.main(["--time", "08:44:58", "--symbol", "فولاد"])

    assert exc.value.code == 1
    assert "buy" not in calls
    assert ("wait", dt_time(8, 44, 58)) not in calls


def test_auto_mode_requires_quantity(stub_main, calls, monkeypatch):
    monkeypatch.setattr(stub_main, "QUANTITY", 0)

    with pytest.raises(SystemExit) as exc:
        stub_main.main(["--time", "08:44:58", "--symbol", "فولاد"])

    assert exc.value.code == 1
    assert "driver" not in calls   # validated before launching a browser


def test_symbol_cli_overrides_env(stub_main, calls, monkeypatch):
    monkeypatch.setattr(stub_main, "SYMBOL", "خودرو")
    stub_main.main(["--time", "08:44:58", "--symbol", "فولاد"])
    assert ("prepare", "فولاد") in calls


# ────────────────────────────────────────────────────────
# main() shutdown: Ctrl+C before the buy, dead browser
# ────────────────────────────────────────────────────────
def test_wait_receives_a_should_stop_hook(stub_main, calls, monkeypatch):
    """The clock wait must be able to see Ctrl+C (it used to ignore it)."""
    seen = {}

    def wait(page, target, **kw):
        seen.update(kw)
        return dt_time(8, 44, 58)

    monkeypatch.setattr(stub_main, "wait_until_broker_time", wait)
    stub_main.main(["--time", "08:44:58", "--symbol", "فولاد"])

    assert callable(seen.get("should_stop"))
    assert seen["should_stop"]() is False   # nothing pressed yet


def test_ctrl_c_during_wait_exits_before_buy(stub_main, calls, monkeypatch):
    def wait(page, target, **kw):
        calls.append(("wait", target))
        stub_main._shutdown_requested = True   # the signal handler ran
        kw["should_stop"]()                    # and the wait saw it
        return None

    monkeypatch.setattr(stub_main, "wait_until_broker_time", wait)

    with pytest.raises(SystemExit) as exc:
        stub_main.main(["--time", "08:44:58", "--symbol", "فولاد"])

    assert exc.value.code == 0        # a user abort is not a failure
    assert "buy" not in calls
    assert calls[-1] == "cleanup"


def test_dead_browser_after_ctrl_c_exits_zero(stub_main, calls, monkeypatch):
    from driver import BrowserGone

    def wait(page, target, **kw):
        stub_main._shutdown_requested = True
        raise BrowserGone("the Playwright browser/driver is gone")

    monkeypatch.setattr(stub_main, "wait_until_broker_time", wait)

    with pytest.raises(SystemExit) as exc:
        stub_main.main(["--time", "08:44:58", "--symbol", "فولاد"])

    assert exc.value.code == 0
    assert "buy" not in calls
    assert calls[-1] == "cleanup"


def test_dead_browser_without_ctrl_c_exits_one(stub_main, calls, monkeypatch, caplog):
    from driver import BrowserGone

    def wait(page, target, **kw):
        raise BrowserGone("the Playwright browser/driver is gone")

    monkeypatch.setattr(stub_main, "wait_until_broker_time", wait)

    with pytest.raises(SystemExit) as exc:
        stub_main.main(["--time", "08:44:58", "--symbol", "فولاد"])

    assert exc.value.code == 1
    assert "buy" not in calls
    assert calls[-1] == "cleanup"


# ────────────────────────────────────────────────────────
# main() with a cached API token (ADR-005)
# ────────────────────────────────────────────────────────
def _resumed_prepared():
    return {
        "symbol": "فولاد",
        "isin": "IRO1FOLD0001",
        "quantity": 1,
        "price": 4540,
        "price_mode": "ceiling",
        "commission": 0.003712,
        "client": object(),
        "clock": lambda: dt_time(8, 0, 0),
        "resumed": True,
    }


def test_cached_token_resume_skips_login_and_ui(stub_main, calls, monkeypatch):
    monkeypatch.setattr(stub_main, "_try_resume_api", lambda page, symbol, logger: _resumed_prepared())
    monkeypatch.setattr(stub_main, "_ui_logged_in", lambda page, timeout_ms=3000: False)
    monkeypatch.setattr(stub_main.api_flow, "execute_buy_api", lambda client, prep, **kw: True)

    stub_main.main(["--time", "08:44:58", "--symbol", "فولاد"])

    assert "login" not in calls                      # the whole point: no login
    assert not any(isinstance(c, tuple) and c[0] == "prepare" for c in calls)
    assert ("wait", dt_time(8, 44, 58)) in calls


def test_api_only_run_logs_in_before_ui_fallback(stub_main, calls, monkeypatch):
    def boom(client, prep):
        raise RuntimeError("API died mid-flight")

    monkeypatch.setattr(stub_main, "_try_resume_api", lambda page, symbol, logger: _resumed_prepared())
    monkeypatch.setattr(stub_main, "_ui_logged_in", lambda page, timeout_ms=3000: False)
    monkeypatch.setattr(stub_main.api_flow, "execute_buy_api", boom)
    monkeypatch.setattr(stub_main.api_flow, "order_already_present", lambda client, prep: False)

    stub_main.main(["--time", "08:44:58", "--symbol", "فولاد"])

    assert "login" in calls                          # fallback needs a real session
    assert "buy" in calls


def test_no_token_cache_flag_forces_login(stub_main, calls, monkeypatch):
    resumed = []
    monkeypatch.setattr(
        stub_main, "_try_resume_api", lambda page, symbol, logger: resumed.append(1) or None
    )
    monkeypatch.setattr(stub_main, "_try_prepare_api", lambda *a, **kw: None)

    stub_main.main(["--time", "08:44:58", "--symbol", "فولاد", "--no-token-cache"])

    assert resumed == []                              # cache never consulted
    assert "login" in calls


# ────────────────────────────────────────────────────────
# main._try_resume_api — cache decision logic
# ────────────────────────────────────────────────────────
class _FakePage:
    """Just enough Page for the resume path: goto() + a browser context."""

    class _Ctx:
        request = object()

    context = _Ctx()

    def __init__(self):
        self.goto_calls = []

    def goto(self, url, wait_until=None):
        self.goto_calls.append(url)


class _AliveClient:
    def __init__(self, *a, **kw):
        pass

    def server_timestamp(self):
        return 1_000_000

    def list_orders(self):
        return []


class _DeadClient(_AliveClient):
    def server_timestamp(self):
        raise ApiAuthError("GET /... → 401 Unauthorized (token expired?)")


def _stub_cache(monkeypatch, token="t" * 100):
    """Enable the cache and record clear_token() calls. Returns the record list."""
    cleared = []
    monkeypatch.setattr(main_mod, "TOKEN_CACHE", True)
    monkeypatch.setattr(main_mod.token_store, "load_token", lambda path, minv, **kw: token)
    monkeypatch.setattr(main_mod.token_store, "describe", lambda path: "cached token (test)")
    monkeypatch.setattr(
        main_mod.token_store, "clear_token", lambda path: cleared.append(path) or True
    )
    monkeypatch.setattr(main_mod, "PlaywrightTransport", lambda ctx: None)
    return cleared


PREPARED = {
    "symbol": "فولاد",
    "isin": "IRO1FOLD0001",
    "quantity": 1,
    "price": 4540,
    "price_mode": "ceiling",
    "commission": 0.003712,
}


def test_resume_returns_prepared_order_with_api_clock(monkeypatch):
    _stub_cache(monkeypatch)
    monkeypatch.setattr(main_mod, "EasyApiClient", _AliveClient)
    monkeypatch.setattr(main_mod.api_flow, "prepare_via_api", lambda *a, **kw: dict(PREPARED))

    prepared = main_mod._try_resume_api(_FakePage(), "فولاد", logging.getLogger("t"))

    assert prepared["resumed"] is True
    assert prepared["price"] == 4540
    assert callable(prepared["clock"])  # plugs straight into wait_until_broker_time


def test_resume_drops_cached_token_on_401(monkeypatch):
    cleared = _stub_cache(monkeypatch)
    monkeypatch.setattr(main_mod, "EasyApiClient", _DeadClient)

    assert main_mod._try_resume_api(_FakePage(), "فولاد", logging.getLogger("t")) is None
    assert cleared, "a rejected (401) token must be deleted, not retried"


def test_resume_drops_token_when_prep_wraps_the_401(monkeypatch):
    """prepare_via_api re-raises as ApiFlowError — the cause chain still counts."""
    cleared = _stub_cache(monkeypatch)
    monkeypatch.setattr(main_mod, "EasyApiClient", _AliveClient)

    def wrapped(*a, **kw):
        outer = ApiFlowError("API prep failed: POST /symbols/api/symbols/all → 401")
        outer.__cause__ = ApiAuthError("401 Unauthorized")
        raise outer

    monkeypatch.setattr(main_mod.api_flow, "prepare_via_api", wrapped)

    assert main_mod._try_resume_api(_FakePage(), "فولاد", logging.getLogger("t")) is None
    assert cleared


def test_resume_keeps_token_on_transient_error(monkeypatch):
    """A WAF/network hiccup must not throw away a working session token."""
    cleared = _stub_cache(monkeypatch)

    class Flaky(_AliveClient):
        def server_timestamp(self):
            raise ApiFlowError("transport error")

    monkeypatch.setattr(main_mod, "EasyApiClient", Flaky)

    assert main_mod._try_resume_api(_FakePage(), "فولاد", logging.getLogger("t")) is None
    assert cleared == []


def test_is_auth_failure_walks_the_cause_chain():
    outer = ApiFlowError("prep failed")
    outer.__cause__ = ApiAuthError("401")

    assert main_mod._is_auth_failure(outer) is True
    assert main_mod._is_auth_failure(ValueError("nothing to do with auth")) is False


# ────────────────────────────────────────────────────────
# CLI parsing
# ────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "value,expected",
    [("08:44", dt_time(8, 44, 0)), ("08:44:58", dt_time(8, 44, 58)), ("9:05:07", dt_time(9, 5, 7))],
)
def test_parse_time_arg_valid(value, expected):
    assert main_mod.parse_time_arg(value) == expected


def test_parse_time_arg_invalid():
    with pytest.raises(main_mod.argparse.ArgumentTypeError):
        main_mod.parse_time_arg("084458")
