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
from datetime import time as dt_time

import pytest

import broker
import main as main_mod
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
    monkeypatch.setattr(m, "is_time_already_passed", lambda page, target, logger: False)
    monkeypatch.setattr(
        m, "wait_until_broker_time", lambda page, target: calls.append(("wait", target))
    )
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
    monkeypatch.setattr(stub_main, "is_time_already_passed", lambda page, t, log: True)

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
