# ADR-005 — Cache the API token and reuse it across runs

**Status:** implemented (2026-10-17) · **Branch:** `api-method` · Builds on [ADR-004](004-hybrid-http-api-mode.md)

## Context

ADR-004 harvests the JWT (`Authorization: Bearer …`) from the page's own
traffic right after the Playwright login, and throws it away when the process
exits. Every run therefore pays a full OIDC login (measured: ~18–20 s, and it
depends on the identity provider being healthy) before the API work can start.

Two questions came out of that:

1. **Can the next run reuse the previous token** and skip the login — and fall
   back to the Playwright login only when the token no longer works?
2. **Is reusing a token safe?** Would the broker see it and block the account?

Measured on the live account (2026-10-17): the JWT lives **12 hours**
(`exp - iat = 43200 s`, ~719 minutes remaining right after harvest), carries
`national_id`, `sub`, `sid`, `scope` and friends, and is rejected with
**HTTP 401** — not an HTML WAF page — when the signature is bogus.

## Decision

Cache the JWT in a single project-local file and try it before logging in.

- **File**: `TOKEN_CACHE_PATH` (default `.mofid_token.json`, project-relative),
  written atomically (temp file + `os.replace`), `0600` on POSIX, git-ignored,
  and never printed or logged. Only the JWT, its expiry, and a **hash** of the
  configured username (`account_fingerprint`) are stored — no plaintext
  username, no password, no refresh token (none was observed in the traffic).
- **Save**: only after `prepare_via_api` succeeded, i.e. the token has already
  proven itself against the live API. A bad token can never poison the cache.
- **Reuse**: at startup, if `API_MODE` and `TOKEN_CACHE` are on, open the
  broker page (the browser needs the gateway/WAF cookies even when the request
  is authorised by the bearer token), then validate with three read-only calls:
  `GET /easy/api/account/server-time/{ms}`, `GET /core/api/order` (the buy's
  verification path), and the full `prepare_via_api` (symbol → ISIN → quote).
  Any failure → normal Playwright login, unchanged.
- **Auth failures**: a 401 — direct or wrapped in `ApiFlowError` by
  `prepare_via_api` (the cause chain is inspected) — **deletes** the file.
  A transient error (WAF, network, timeout) keeps the file and logs in.
- **Clock**: if the browser UI is logged in, the on-page clock is still used.
  If not (the token authenticated us without a session), `ApiClock` derives
  broker time from ONE `/server-time` sync plus local drift, with a lazy
  re-sync every 60 s. Polling `/server-time` at the 50 ms clock interval would
  be ~20 req/s — nothing like the real app.
- **Account switch**: the hash comparison refuses (and deletes) a token that
  belongs to a different `MOFID_USER`, so changing accounts can never silently
  trade the previous one.
- **Escape hatches**: `--no-token-cache` (ignore + don't save), `TOKEN_CACHE=false`.

## Verified live (2026-10-17, no order submitted)

| Check | Result |
|---|---|
| Login → harvest → prep → cache | token len 1881, cached, `expires_at - saved_at = 12.0 h` |
| Fresh browser, **no login**, cached token → symbol + quote + order list | `RESUME_OK`; `_ui_logged_in() = False`, so the run was API-only |
| API clock from the cached token | `offset +305 / +316 / +328 ms` across runs |
| Bogus (unsigned) cached token | `GET /easy/api/account/server-time/… → 401` → **file deleted** → login fallback (`REJECT_OK`) |
| Unit suite | `python -m pytest tests -q` → 82 passed, 1 skipped (POSIX-permission test on Windows) |

## Is reusing the token safe? (the ban question)

Honest answer: **token reuse itself is not a realistic ban trigger, but nothing
here is sanctioned by the broker, and I cannot guarantee anything.**

Why reuse looks ordinary:

- The stored JWT is exactly what the web app itself keeps in browser storage
  and re-sends after every reload; the session cookie/token model is the app's,
  not something we invented. A 12-hour token that is reused within its lifetime
  is the app's own behaviour.
- Requests still go through Chromium's network stack (`context.request`) with
  the app's own host, paths, headers and TLS fingerprint — the gateway sees the
  same client it saw during the login.
- Skipping the login *reduces* authentication traffic (one OIDC login per day
  instead of one per run), which is if anything less noisy.

What actually looks machine-like, and what we do about it:

| Signal | Mitigation in this codebase |
|---|---|
| High request rate (e.g. clock polling at 20 req/s) | `ApiClock` syncs once per 60 s; the rest is local arithmetic |
| A burst of real order POSTs | `API_BUY_MAX_ATTEMPTS` default **20** at 0.35 s, but only at/after the open (20 × 0.35 s ≈ 7 s) and only until one order is accepted+verified; pre-open refusals (`11000`/`8702`) are retried without spending that budget (ADR-006) |
| Duplicate orders (money risk, and the thing brokers care about most) | stop on 2xx-but-unverified; no UI fallback after a 2xx; duplicate guard before any UI fallback |
| Credential sitting on disk | 0600 + git-ignored + bearer-only + deleted on 401 + account-bound; `--no-token-cache` if unwanted |

Residual risks that no code can remove: automated order entry at the opening
millisecond is unusual by nature, brokers' terms may restrict or prohibit API
use or bots, and an account-level trading restriction (or gateway throttling)
remains possible if the traffic pattern draws attention. No 429/rate-limit
response was observed during the read-path verification, but that is not proof
of any policy — it is one observation.

## Consequences

- A rerun inside the token's 12-hour window skips the OIDC login; the bot can
  reach the "prepared" state in ~13 s (measured) instead of ~20 s, and — more
  importantly — it survives an identity-provider/login incident right before
  the opening bell.
- One more file on disk holding a bearer credential: treated as a secret
  (git-ignored, 0600 on POSIX; on Windows the file inherits the user-profile
  ACL, since Windows ignores POSIX mode bits — the unit test for 0600 is
  SKIPPED there).
- The cache is account-bound by hash, so multi-account users should be aware
  that `MOFID_USER` changes invalidate it (that is the intended failure mode).
- If the UI is not logged in, the run is API-only: there is no order ticket to
  click, so a UI fallback first performs a real login (main() logs this).
