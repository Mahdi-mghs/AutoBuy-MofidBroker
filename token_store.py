"""
Cached API token store (ADR-005).

The EasyTrader web app authenticates every API call with a short-lived JWT
(`Authorization: Bearer …`, captured at ~1888 chars). ADR-004 harvests a
fresh token after the Playwright login; this module keeps that token on disk
so the NEXT run can try it before paying for a login — and, more importantly,
so a cached token can be validated with one read-only request instead of a
full OIDC round-trip right before the opening bell.

Security rules followed here:

- the file is written atomically with 0600 permissions (POSIX; best effort
  on Windows), never into the repo, never printed, never logged;
- only the raw JWT plus a non-reversible hash of the configured username
  (`account_fingerprint`) is stored — no plaintext username, no password, no
  refresh token (none was observed);
- a token is only usable while its own `exp` claim says so, minus a safety
  margin — an expired token is deleted, not retried;
- `describe()` yields a safe log line (minutes remaining), never the token.

The JWT payload is base64url-decoded WITHOUT signature verification: we only
need the expiry for bookkeeping, never to make a trust decision. The server
still validates the signature on every request.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Relative paths resolve against the project root (next to config.py), so the
# cache lands in the same place no matter which directory the bot starts from.
PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_PATH = ".mofid_token.json"

# A token must remain valid at least this long to be worth reusing — otherwise
# it could expire between the readiness check and the actual order burst.
DEFAULT_MIN_VALIDITY_S = 120

# If the JWT carries no `exp` claim, trust it for this long at most.
UNKNOWN_EXP_TTL_S = 3600

CACHE_VERSION = 1
_MIN_TOKEN_LEN = 64


def resolve_path(path: str | os.PathLike | None = None) -> Path:
    """Absolute path of the cache file (relative paths are project-relative)."""
    p = Path(path or DEFAULT_PATH)
    return p if p.is_absolute() else PROJECT_DIR / p


# ── JWT helpers ─────────────────────────────────────────
def _b64url_decode(segment: str) -> bytes:
    pad = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + pad)


def jwt_expiry(token: str) -> int | None:
    """`exp` claim (epoch seconds) from a JWT, or None if absent/unreadable."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload = json.loads(_b64url_decode(parts[1]))
    except Exception:
        return None
    exp = payload.get("exp") if isinstance(payload, dict) else None
    if isinstance(exp, (int, float)):
        return int(exp)
    return None


def account_fingerprint(username: str) -> str:
    """Stable, non-reversible tag for the configured account.

    Stored next to the token so switching MOFID_USER can never silently
    resume a DIFFERENT account's session (a real hazard for a trading bot).
    The username itself is never written to the cache.
    """
    return hashlib.sha256((username or "").strip().encode("utf-8")).hexdigest()[:16]


def _read_record(path: str | os.PathLike | None = None) -> dict | None:
    p = resolve_path(path)
    try:
        with open(p, "r", encoding="utf-8") as fh:
            record = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        logger.warning("Cached token file unreadable (%s) — discarding it.", e)
        clear_token(p)
        return None
    return record if isinstance(record, dict) else None


# ── Store API ───────────────────────────────────────────
def save_token(
    token: str,
    path: str | os.PathLike | None = None,
    now_fn=time.time,
    account_hint: str | None = None,
) -> dict:
    """
    Persist one JWT. Returns the stored record WITHOUT the token itself, so
    the result is safe to log.

    Written to a temp file in the same directory, chmod 0600, then atomically
    moved into place, so a crash can never leave a half-written cache.
    """
    token = (token or "").strip()
    if len(token) < _MIN_TOKEN_LEN:
        raise ValueError("Refusing to cache a token that is too short to be a JWT.")

    now = int(now_fn())
    exp = jwt_expiry(token)
    record = {
        "version": CACHE_VERSION,
        "token": token,
        "saved_at": now,
        "expires_at": exp if exp is not None else now + UNKNOWN_EXP_TTL_S,
        "expiry_from_claim": exp is not None,
        "account": account_hint,
    }

    p = resolve_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh)
        try:
            os.chmod(tmp_name, 0o600)
        except OSError:  # pragma: no cover - Windows is best effort
            pass
        os.replace(tmp_name, p)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    if exp is None:
        logger.info("Cached API token (no exp claim — assuming %d min).", UNKNOWN_EXP_TTL_S // 60)
    return {k: v for k, v in record.items() if k != "token"}


def load_token(
    path: str | os.PathLike | None = None,
    min_validity_s: int = DEFAULT_MIN_VALIDITY_S,
    now_fn=time.time,
    account_hint: str | None = None,
) -> str | None:
    """
    Return the cached token if it is still valid for at least `min_validity_s`
    (and belongs to the configured account); otherwise delete the file and
    return None so the caller logs in fresh.
    """
    record = _read_record(path)
    if not record:
        return None

    stored_account = record.get("account")
    if account_hint and stored_account and stored_account != account_hint:
        logger.warning(
            "Cached token belongs to a different account (MOFID_USER changed) — "
            "discarding it and logging in."
        )
        clear_token(path)
        return None
    if account_hint and not stored_account:
        logger.debug("Cached token has no account tag — accepting it (legacy record).")

    token = record.get("token")
    if not isinstance(token, str) or len(token) < _MIN_TOKEN_LEN:
        logger.warning("Cached token file has no usable token — discarding it.")
        clear_token(path)
        return None

    expires_at = record.get("expires_at")
    if not isinstance(expires_at, (int, float)):
        # Avoid trusting an unparseable expiry forever.
        saved_at = record.get("saved_at")
        if isinstance(saved_at, (int, float)):
            expires_at = int(saved_at) + UNKNOWN_EXP_TTL_S
        else:
            clear_token(path)
            return None

    remaining = float(expires_at) - float(now_fn())
    if remaining < float(min_validity_s):
        logger.info(
            "Cached API token has %d s left (need %d) — discarding it and logging in.",
            int(remaining), int(min_validity_s),
        )
        clear_token(path)
        return None
    return token


def describe(
    path: str | os.PathLike | None = None,
    now_fn=time.time,
) -> str:
    """Safe, token-free one-line description for logs."""
    record = _read_record(path)
    if not record:
        return "no cached token"
    expires_at = record.get("expires_at")
    if not isinstance(expires_at, (int, float)):
        return "cached token (expiry unknown)"
    remaining = int(float(expires_at) - float(now_fn()))
    if remaining <= 0:
        return "cached token expired"
    minutes, seconds = divmod(remaining, 60)
    return f"cached token valid for {minutes}m{seconds:02d}s"


def clear_token(path: str | os.PathLike | None = None) -> bool:
    """Delete the cache file. Returns True if a file was removed."""
    try:
        resolve_path(path).unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as e:  # pragma: no cover - permissions etc.
        logger.warning("Could not delete the cached token file: %s", e)
        return False
