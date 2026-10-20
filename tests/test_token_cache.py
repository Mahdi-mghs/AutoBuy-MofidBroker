"""
Cached-token store + API clock tests (ADR-005) — no browser, no network.

Covered:
- JWT `exp` extraction (claim present / absent / garbage tokens)
- save → load round-trip, atomic file, 0600 permissions, loggable return value
- expiry: deleted, not retried; the min-validity margin
- tokens without an `exp` claim fall back to a bounded TTL
- corrupt/truncated files are discarded instead of trusted
- `describe()` never leaks the token itself
- ApiClock: single sync + drift, and no per-poll hammering of /server-time
"""

import base64
import json
import os
from datetime import datetime
from pathlib import Path

import pytest

import token_store
from api_flow import ApiClock
from easy_api import TEHRAN_TZ

NOW = 1_700_000_000.0  # fixed epoch for deterministic expiry math


def make_jwt(exp=None, extra=None) -> str:
    """A structurally valid JWT (signature is fake — we never verify it)."""

    def seg(payload):
        raw = json.dumps(payload).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    header = {"alg": "RS256", "typ": "JWT"}
    payload = {"sub": "1234567890", "name": "Test User"}
    if exp is not None:
        payload["exp"] = exp
    if extra:
        payload.update(extra)
    return f"{seg(header)}.{seg(payload)}.{'s' * 64}"


# ── JWT parsing ────────────────────────────────────────
def test_jwt_expiry_reads_the_claim():
    assert token_store.jwt_expiry(make_jwt(exp=NOW + 300)) == int(NOW + 300)


@pytest.mark.parametrize("bad", ["", "not-a-jwt", "only.two", "a.b.c.d", "...."])
def test_jwt_expiry_none_for_unusable_tokens(bad):
    assert token_store.jwt_expiry(bad) is None


def test_jwt_expiry_none_when_claim_absent():
    assert token_store.jwt_expiry(make_jwt()) is None


# ── save / load / clear ────────────────────────────────
def test_save_load_roundtrip(tmp_path):
    path = tmp_path / "tok.json"
    token = make_jwt(exp=NOW + 600)

    record = token_store.save_token(token, path, now_fn=lambda: NOW)

    assert "token" not in record, "the returned record must be safe to log"
    assert record["expires_at"] == int(NOW + 600)
    assert record["expiry_from_claim"] is True
    assert token_store.load_token(path, min_validity_s=120, now_fn=lambda: NOW) == token
    # Nothing else is persisted: no username, no password, no refresh token.
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert set(stored) == {
        "version", "token", "saved_at", "expires_at", "expiry_from_claim", "account",
    }


def test_account_binding_blocks_another_users_session(tmp_path):
    """Switching MOFID_USER must never resume the previous account's session."""
    path = tmp_path / "tok.json"
    mine = token_store.account_fingerprint("0012345678")
    other = token_store.account_fingerprint("0099999999")
    token_store.save_token(
        make_jwt(exp=NOW + 600), path, now_fn=lambda: NOW, account_hint=mine
    )

    assert token_store.load_token(path, now_fn=lambda: NOW, account_hint=mine) is not None
    # Same token, different configured account → refused AND dropped.
    token_store.save_token(
        make_jwt(exp=NOW + 600), path, now_fn=lambda: NOW, account_hint=other
    )
    assert token_store.load_token(path, now_fn=lambda: NOW, account_hint=mine) is None
    assert not path.exists()


def test_account_fingerprint_is_not_the_username():
    fingerprint = token_store.account_fingerprint("0012345678")
    assert "0012345678" not in fingerprint
    assert len(fingerprint) == 16
    assert token_store.account_fingerprint(" 0012345678 ") == fingerprint  # trimmed


def test_expired_token_is_deleted_not_reused(tmp_path):
    path = tmp_path / "tok.json"
    token_store.save_token(make_jwt(exp=NOW - 1), path, now_fn=lambda: NOW)

    assert token_store.load_token(path, now_fn=lambda: NOW) is None
    assert not path.exists()


def test_min_validity_margin_rejects_soon_expiring_token(tmp_path):
    path = tmp_path / "tok.json"
    token = make_jwt(exp=NOW + 30)
    token_store.save_token(token, path, now_fn=lambda: NOW)

    assert token_store.load_token(path, min_validity_s=120, now_fn=lambda: NOW) is None
    assert not path.exists()


def test_token_without_exp_uses_bounded_ttl(tmp_path):
    path = tmp_path / "tok.json"
    token = make_jwt()  # no exp claim
    token_store.save_token(token, path, now_fn=lambda: NOW)

    assert token_store.load_token(path, now_fn=lambda: NOW) == token
    later = NOW + token_store.UNKNOWN_EXP_TTL_S + 1
    assert token_store.load_token(path, now_fn=lambda: later) is None


def test_corrupt_file_is_discarded(tmp_path):
    path = tmp_path / "tok.json"
    path.write_text("{ this is not json", encoding="utf-8")

    assert token_store.load_token(path, now_fn=lambda: NOW) is None
    assert not path.exists()


def test_file_without_token_is_discarded(tmp_path):
    path = tmp_path / "tok.json"
    path.write_text(json.dumps({"version": 1, "expires_at": NOW + 600}), encoding="utf-8")

    assert token_store.load_token(path, now_fn=lambda: NOW) is None
    assert not path.exists()


def test_short_token_is_refused(tmp_path):
    with pytest.raises(ValueError):
        token_store.save_token("too-short", tmp_path / "tok.json")


def test_missing_file_is_not_an_error(tmp_path):
    assert token_store.load_token(tmp_path / "absent.json", now_fn=lambda: NOW) is None
    assert token_store.clear_token(tmp_path / "absent.json") is False


def test_clear_token_removes_the_file(tmp_path):
    path = tmp_path / "tok.json"
    token_store.save_token(make_jwt(exp=NOW + 600), path, now_fn=lambda: NOW)

    assert token_store.clear_token(path) is True
    assert not path.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits only")
def test_cache_file_is_owner_only(tmp_path):
    path = tmp_path / "tok.json"
    token_store.save_token(make_jwt(exp=NOW + 600), path, now_fn=lambda: NOW)

    assert (path.stat().st_mode & 0o777) == 0o600


def test_no_temp_file_left_behind(tmp_path):
    path = tmp_path / "tok.json"
    token_store.save_token(make_jwt(exp=NOW + 600), path, now_fn=lambda: NOW)

    assert [p.name for p in tmp_path.iterdir()] == ["tok.json"]


def test_describe_never_leaks_the_token(tmp_path):
    path = tmp_path / "tok.json"
    token = make_jwt(exp=NOW + 300)
    token_store.save_token(token, path, now_fn=lambda: NOW)

    text = token_store.describe(path, now_fn=lambda: NOW)
    assert token not in text
    assert "Bearer" not in text
    assert "valid for" in text
    assert token_store.describe(tmp_path / "absent.json") == "no cached token"


def test_default_path_is_project_relative():
    resolved = token_store.resolve_path(".mofid_token.json")
    assert resolved.is_absolute()
    assert resolved.parent == Path(token_store.__file__).resolve().parent


# ── ApiClock ───────────────────────────────────────────
class FakeTimeClient:
    def __init__(self, server_ms):
        self.server_ms = server_ms
        self.calls = 0

    def server_timestamp(self):
        self.calls += 1
        return self.server_ms


def test_api_clock_applies_the_measured_offset():
    # Local epoch 1000.0 s, broker says it is 1005.0 s → clock reads broker time.
    clock = ApiClock(FakeTimeClient(1_005_000), now_fn=lambda: 1000.0).sync()

    assert clock.time() == datetime.fromtimestamp(1005.0, TEHRAN_TZ).time()
    assert clock() == clock.time()  # callable, so it plugs into now_fn=


def test_api_clock_does_not_poll_server_time_every_read():
    state = {"t": 1000.0}
    client = FakeTimeClient(1_000_000)
    clock = ApiClock(client, resync_after_s=60.0, now_fn=lambda: state["t"]).sync()
    assert client.calls == 1

    for _ in range(200):  # 200 clock reads inside the resync window
        clock.time()
    assert client.calls == 1, "clock reads must not hammer /server-time"

    state["t"] += 61.0
    clock.time()
    assert client.calls == 2


def test_api_clock_survives_a_failed_resync():
    state = {"t": 1000.0}

    class Flaky(FakeTimeClient):
        def server_timestamp(self):
            self.calls += 1
            if self.calls > 1:
                raise RuntimeError("gateway hiccup")
            return self.server_ms

    client = Flaky(1_000_000)
    clock = ApiClock(client, resync_after_s=60.0, now_fn=lambda: state["t"]).sync()

    state["t"] += 120.0  # past the resync window; the re-sync attempt fails
    # The clock keeps the last good offset instead of raising mid-wait...
    assert clock.time() == datetime.fromtimestamp(state["t"], TEHRAN_TZ).time()
    # ...and the failure does not trigger a retry on every subsequent read.
    assert client.calls == 2
    assert clock.time() is not None
    assert client.calls == 2
