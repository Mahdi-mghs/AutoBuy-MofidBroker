"""
Shutdown behaviour: Ctrl+C and a dead browser/Playwright session.

Why this exists: pressing Ctrl+C before the buy used to flood the terminal with
Playwright errors and never exit. The signal handler only set a flag; the clock
wait ignored it, and the console event also hits the Playwright driver, so the
in-flight page call failed with TargetClosedError — which the wait loop treated
as a "transient clock read failure" and retried ~20×/s until the 1 h deadline.

Covered:
- wait_until_broker_time honours `should_stop` (before and during the wait)
- a closed page/connection raises BrowserGone instead of being retried
- transient clock read failures are still retried (no regression)
- driver.is_browser_gone classification (message + cause chain)
- execute_buy_api stops on the first submit when the page is gone
- driver cleanup tolerates an already-closed browser and never raises
"""

from datetime import time as dt_time

import pytest
from playwright.sync_api import Error as PlaywrightError

import broker
import driver
from api_flow import execute_buy_api
from easy_api import EasyApiError

CLOSED_MSG = "Locator.inner_text: Target page, context or browser has been closed"


@pytest.fixture(autouse=True)
def fast_clock(monkeypatch):
    """Keep clock polling instant in tests."""
    monkeypatch.setattr(broker, "CLOCK_POLL_INTERVAL", 0.0)
    # Never let a broken loop spin for an hour: the deadline fails it fast.
    monkeypatch.setattr(broker, "CLOCK_DEADLINE_SECONDS", 5)


# ────────────────────────────────────────────────────────
# wait_until_broker_time: Ctrl+C
# ────────────────────────────────────────────────────────
def test_wait_returns_none_when_stop_already_requested():
    polls = []

    def clock():
        polls.append(1)
        return dt_time(8, 0, 0)

    result = broker.wait_until_broker_time(
        None, dt_time(8, 44, 58), now_fn=clock, should_stop=lambda: True
    )

    assert result is None          # nothing left to do → main exits before buy
    assert len(polls) == 1         # only the up-front readability check


def test_wait_stops_mid_wait_when_ctrl_c_arrives():
    """The flag flips after 3 polls: the loop must return, not keep polling."""
    polls = []

    def clock():
        polls.append(1)
        return dt_time(8, 44, 50)

    def should_stop():
        return len(polls) >= 3

    result = broker.wait_until_broker_time(
        None, dt_time(8, 44, 58), now_fn=clock, should_stop=should_stop
    )

    assert result is None
    assert len(polls) <= 4         # stopped right after the flag flipped


def test_wait_without_should_stop_still_reaches_target():
    """Default (None) keeps the old behaviour for callers that don't pass it."""
    seq = iter([dt_time(8, 44, 50), dt_time(8, 44, 57), dt_time(8, 44, 58)])

    reached = broker.wait_until_broker_time(
        None, dt_time(8, 44, 58), now_fn=lambda: next(seq)
    )

    assert reached == dt_time(8, 44, 58)


# ────────────────────────────────────────────────────────
# wait_until_broker_time: dead page/driver
# ────────────────────────────────────────────────────────
def test_wait_raises_browser_gone_instead_of_retrying():
    calls = []

    def dead_clock():
        calls.append(1)
        raise PlaywrightError(CLOSED_MSG)

    with pytest.raises(driver.BrowserGone):
        broker.wait_until_broker_time(None, dt_time(8, 44, 58), now_fn=dead_clock)

    assert len(calls) == 1         # one attempt, no retry storm


def test_wait_keeps_retrying_transient_clock_errors():
    seq = iter([dt_time(8, 44, 50), ValueError("stale element"), dt_time(8, 44, 58)])

    def flaky_clock():
        value = next(seq)
        if isinstance(value, Exception):
            raise value
        return value

    reached = broker.wait_until_broker_time(None, dt_time(8, 44, 58), now_fn=flaky_clock)

    assert reached == dt_time(8, 44, 58)


# ────────────────────────────────────────────────────────
# driver.is_browser_gone
# ────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "exc,expected",
    [
        (PlaywrightError(CLOSED_MSG), True),
        (PlaywrightError("Playwright connection closed"), True),
        (PlaywrightError("Connection closed while reading from the driver"), True),
        (PlaywrightError("Event loop is closed! Is Playwright already stopped?"), True),
        # A real timeout / ordinary failure is NOT a dead browser.
        (PlaywrightError("Locator.inner_text: Timeout 5000ms exceeded."), False),
        (ValueError("Unexpected broker time format: ''"), False),
        (KeyboardInterrupt(), False),
        (None, False),
    ],
)
def test_is_browser_gone_classification(exc, expected):
    assert driver.is_browser_gone(exc) is expected


def test_is_browser_gone_follows_the_cause_chain():
    """The transport wraps Playwright errors in EasyApiError (raise ... from e)."""
    gone = PlaywrightError(CLOSED_MSG)
    try:
        try:
            raise gone
        except PlaywrightError as e:
            raise EasyApiError("POST /core/api/v2/order transport error: TargetClosedError") from e
    except EasyApiError as wrapped:
        assert driver.is_browser_gone(wrapped) is True


def test_require_live_browser_reraises_as_browser_gone():
    with pytest.raises(driver.BrowserGone):
        driver.require_live_browser(PlaywrightError(CLOSED_MSG))

    # An ordinary error passes through untouched.
    driver.require_live_browser(ValueError("boom"))


# ────────────────────────────────────────────────────────
# Order burst: a dead page must not burn the attempt budget
# ────────────────────────────────────────────────────────
class _GoneClient:
    def __init__(self):
        self.submits = 0
        self.list_calls = 0

    def list_orders(self):
        self.list_calls += 1
        return []

    def submit_buy(self, **_kwargs):
        self.submits += 1
        raise EasyApiError(
            "POST /core/api/v2/order transport error: TargetClosedError: "
            "Target page, context or browser has been closed"
        )


def test_execute_buy_api_stops_when_the_browser_is_gone():
    client = _GoneClient()
    prepared = {
        "isin": "IRO1FOLD0001",
        "symbol": "فولاد",
        "quantity": 10,
        "price": 4540,
        "commission": 0.003712,
    }

    with pytest.raises(driver.BrowserGone):
        execute_buy_api(client, prepared, now_fn=lambda: dt_time(8, 45, 0))

    assert client.submits == 1     # one submit, then straight out


# ────────────────────────────────────────────────────────
# Cleanup after the driver died
# ────────────────────────────────────────────────────────
class _FakeContext:
    def __init__(self, exc=None):
        self.exc = exc
        self.closed = False

    def close(self):
        if self.exc is not None:
            raise self.exc
        self.closed = True


class _FakePlaywright:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


def _session(default_exc=None):
    return {
        "pw": _FakePlaywright(),
        "browser": None,
        "context": _FakeContext(default_exc),
        "done": False,
    }


def test_cleanup_survives_an_already_closed_browser(monkeypatch):
    logs = []

    class _Log:
        def info(self, msg, *a):
            logs.append(("info", msg % a if a else msg))

        def warning(self, msg, *a):
            logs.append(("warning", msg % a if a else msg))

        def debug(self, msg, *a):
            logs.append(("debug", msg % a if a else msg))

    monkeypatch.setattr(driver, "logger", _Log())
    session = _session(PlaywrightError(CLOSED_MSG))
    driver._active_sessions.append(session)

    driver._cleanup_session(session)          # must not raise

    assert session["done"] is True
    assert session["pw"].stopped is True
    assert session not in driver._active_sessions
    assert not any(level == "warning" for level, _ in logs)


def test_cleanup_closes_a_live_session_normally(monkeypatch):
    logs = []
    monkeypatch.setattr(driver, "logger", type("L", (), {
        "info": lambda self, m, *a: logs.append(m % a if a else m),
        "warning": lambda self, m, *a: logs.append(("warn", m % a if a else m)),
        "debug": lambda self, m, *a: None,
    })())
    session = _session()
    driver._active_sessions.append(session)

    driver._cleanup_session(session)

    assert session["context"].closed is True
    assert session["pw"].stopped is True
    assert not any(isinstance(entry, tuple) and entry[0] == "warn" for entry in logs)
