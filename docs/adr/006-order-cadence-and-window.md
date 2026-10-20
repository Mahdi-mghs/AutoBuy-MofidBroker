# ADR-006 — Order cadence and the order window (measured)

**Status:** implemented (2026-10-18) · **Branch:** `api-method`

## Context

ADR-004 shipped a submit burst of `API_BUY_MAX_ATTEMPTS` POSTs spaced
`API_BUY_RETRY_INTERVAL` (0.15 s). That number was a guess. Two things were
unknown:

1. Whether the broker enforces a minimum gap between order submissions, and
2. What the submit response looks like when it is **not** an accepted order
   (ADR-004 could only capture the request, not a response).

Both are visible while the market is closed: every order is refused by
exchange rules, so a probe can submit freely without any money at risk
(2026-10-18, 04:40–08:16 Tehran, `artifacts/probe_*.py`).

## Measured facts

Every submit answers **HTTP 200/2xx**; the verdict is in the body:

```json
{"isSuccessful": false, "id": "1121BnWUPNSsROa6",
 "message": "8702: ساعت مجاز ثبت سفارش از 8:45 تا 12:30 است.",
 "omsError": [{"error": "ساعت مجاز ثبت سفارش از 8:45 تا 12:30 است.",
               "code": 8702, "name": "OmsCustomError"}]}
```

- **Order-interval rule** — two submits closer than 300 ms are refused with
  **code 13021 `OrderIntervalError`**:
  `"ارسال سفارش با فاصله کمتراز 300 میلی‌ثانیه مجاز نمی‌باشد"`.
  The comparison is server-side (arrival time), per account, and independent
  of symbol, price, quantity and whether the previous order was accepted or
  refused. A throttled submit **never reaches the order book**.
- **Exact spacing (measured with the shipped gate, 5 sends per setting):**
  300 ms → 2 of 5 throttled; 320 / 350 / 400 ms → clean. Arrival jitter eats
  part of the documented 300 ms margin, so the *safe* floor is **320 ms**.
- **Order window** — the session runs **08:45:00–12:35:00**; new orders are
  only accepted **08:45–12:30** (code 8702). The window check runs *before*
  price/quantity validation: with the market closed, a price of 1 and a
  quantity of `max+1` both return 8702, so band limits cannot be probed this
  way (client-side checks from `marketdata/v2` remain the guard).
- **Pre-open refusal** — at **08:44:57** a submit answers **11000**
  `وضعیت گروه اجازه فعالیت نمی‌دهد` ("the group status does not permit
  activity"), not 8702. Same meaning: the session has not started, the order
  never reached the book. This was found the expensive way, on a real-money
  run (see *Correction* below).
- **Minimum order value** — inside the session, a 1-share فولاد order
  (≈4 280 Toman) is refused with **8006** `حداقل سفارش خرید ۵۰۰ هزار تومان
  است` (500 000 Toman). Repeating it gives the same answer, so it is a plain
  rejection, not a retry case.
- **Read endpoints are not throttled at this scale**: 8 × `GET /core/api/order`
  at 50 ms spacing was clean, so `ApiClock`/verification polling is unaffected.

## Decision

1. **Submit gate in the client.** `EasyApiClient` records the last submit's
   monotonic time and sleeps before the next POST so sends are at least
   `min_submit_interval` apart (`SUBMIT_INTERVAL_FLOOR_S = 0.32`). The gate is
   send-to-send, not response-to-send: a response arrives 60–110 ms after the
   request left, and sleeping after it produces bursts that look spaced but are
   not. Callers may raise the interval, never lower it below the floor.
   `createDateTime` is built *after* the wait, so it is the real send time.
2. **Verdict parsing instead of "any message means failure".** The 2xx body is
   classified (`api_flow.classify_submit_response`):
   `accepted` (`isSuccessful: true`, even with a message) · `throttled` (13021) ·
   `not_open` (8702 or 11000, plus the Persian wording as a fallback) ·
   `rejected` (any other OMS code). This also fixes a latent bug: a success
   body carrying an informational message used to read as a rejection.
   **`11000` is not a rejection.** Treating it as one is what made a real-money
   run at 08:44:57 give up on attempt 1, three seconds before the session
   opened.
3. **Throttle is retried, not counted.** A 13021 answer means no order existed,
   so it does not consume `API_BUY_MAX_ATTEMPTS`; the burst simply waits out the
   gap. A real rejection still stops the burst immediately.
4. **Hammer the open, do not sleep through it.** `DEFAULT_BUY_TIME` is
   08:44:58, i.e. *before* the window opens, on purpose: the goal is to be at
   or near the front of the buy queue. A `not_open` answer **never consumes the
   order budget** and is retried immediately, so the burst is in flight when
   08:45:00 arrives. It is bounded by `API_PREOPEN_MAX_ATTEMPTS` (30 ≈ 10 s of
   pre-open coverage) so a holiday cannot loop. After the session opens, the
   clock decides: while the broker clock is past 12:30 a `not_open` means
   "closed for the day" and stops the burst.
5. Defaults: `API_BUY_MAX_ATTEMPTS` 5 → **20** (20 × 0.35 s ≈ 7 s of open-time
   submits — the queue race, not a duplicate-order hazard, because the burst
   stops at the first accepted order), new `API_PREOPEN_MAX_ATTEMPTS` = **30**,
   `API_BUY_RETRY_INTERVAL` 0.15 → **0.35 s**, new `API_MIN_SUBMIT_INTERVAL` =
   **0.32 s** (hard floor), `API_ORDER_WINDOW_GRACE` = **8 s** (now only drives
   the "the open is too far away for this burst" warning).

## Correction after the real-money run (2026-10-18, 08:44:57)

The first version of this ADR said: on 8702 near the open, sleep until 08:45:00
and submit once. Two things were wrong with that:

1. At 08:44:57 the broker answers **11000**, not 8702, and the old classifier
   put every unknown code in `rejected` — so the run logged
   `broker rejected (code 11000) — not re-submitting` and gave up. The whole
   point of a 08:44:58 target is to *not* give up there.
2. "Submit once at 08:45:00" is not the objective. The objective is ~20 submits
   concentrated on the first seconds after the open, which is what a queue race
   for the first buyer requires.

Both are fixed by the `not_open` class + separate pre-open budget described
above.

## Consequences

- A 20-submission open-time burst covers ~7 s. That is unavoidable: the broker
  will not accept anything faster than 300 ms, and 320 ms is the measured safe
  floor.
- Pre-open submits are cheap but visible: 30 POSTs at 350 ms is ~10 s of
  traffic aimed at a session that is about to open. On a holiday the burst
  stops after that budget and says so in the log.
- The burst still stops on the first **accepted and verified** order, so 20
  attempts is not 20 orders.
- The first post-open submit lands **0–350 ms after 08:45:00**, depending on
  where the 350 ms cadence falls relative to the open. Starting at the default
  08:44:58 puts it at ~08:45:00.10. Trying to hit the exact millisecond would
  mean breaking the 300 ms submit gate for that one send, which trades a
  guaranteed refusal for a fraction of a queue slot — not worth it.
- If the broker clock is unavailable (`now_fn=None`) a `not_open` is retried
  without the "closed for the day" check: bounded by the pre-open budget only.
- Throttle retries are free but not unlimited (`(attempts + refusals) × 4`
  iterations) so a broker-side change cannot loop forever.
- Load per second on the order endpoint is now bounded by design; the previous
  0.15 s burst was ~6.7 order POSTs/s, above the broker's own limit.

## Testing

- `tests/test_api.py`: captured 13021 / 8702 / 11000 / 8006 bodies classified;
  gate spacing with a fake monotonic clock (including "cannot go below the
  floor"); throttled **and** pre-open submits do not consume the attempt
  budget; `11000 → 11000 → 8702 → accepted` at 08:44:57 with a budget of ONE
  order still succeeds; pre-open refusals stop after
  `API_PREOPEN_MAX_ATTEMPTS`; a `not_open` after 12:30 stops immediately.
- Live 2026-10-18 (`artifacts/live_throttle_check.py`): 130 ms spacing →
  reproduced 13021; the shipped gate (320 ms floor) → 5 submits, zero 13021;
  `execute_buy_api` with the broker clock → one submit, window-closed stop.
- Live 2026-10-18 (`artifacts/live_preopen_retry_check.py`, session open): two
  injected pre-open answers were retried and the burst stopped only on the real
  **8006** minimum-value refusal — the retry path works end-to-end through the
  real transport without any order that could fill (quantity 1 ≈ 4 280 Toman
  < 500 000 Toman).
- A real **accepted** buy was still not observed: that requires a real order at
  the open.
