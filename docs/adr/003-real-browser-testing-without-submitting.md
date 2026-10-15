# ADR-003: Real-browser testing without ever submitting an order

- **Status:** Accepted
- **Date:** 2026-10-15

## Context

The bot's purpose is to click the final **ارسال خرید** (submit buy)
button at an exact broker-clock time. Verification therefore needs the
real site (login, symbol search, order ticket, clock, toasts), but any
test that reaches the submit click places a **real order with real
money** whenever the market is open.

Interview decision: test in a real browser **up to but not including**
the submit click. The dry run is test-only — no permanent `--dry-run`
CLI flag (production `main.py` always buys, by design).

## Decision

### 1. Selector discovery harness (throwaway)

`discover_selectors.py` (pre-rewrite, run manually) logged in with real
credentials, opened the order ticket, and dumped inputs/buttons/labels +
screenshots into `artifacts/`. Its output is baked into `locators.py`
and into ADR-002's table. It never clicked the buy button.

### 2. Committed smoke test (`smoke_test.py`)

Exercises the entire production path through the same modules the bot
uses (`login → dismiss_announcement → get_broker_time → prepare_order →
find_buy_button`), then **stops**:

- asserts symbol/quantity/price read-backs,
- asserts the buy button is *visible* (never `.click()`s it),
- screenshots each stage to `artifacts/smoke-*.png` for human review,
- exits non-zero on any failure.

### 3. Orchestration/unit tests (`tests/`)

Run without a browser (fast, CI-safe):

- `wait_until_broker_time` with an injected fake clock — target reached,
  target never reached (deadline), after-hours rejection;
- `is_order_accepted` Persian/English keyword classification;
- `main()` flow order with stubbed `login/prepare_order/execute_buy` —
  auto mode prepares before waiting, `--manual` still pauses, expired
  target time aborts before any buy.

## Consequences

- **+** The risky click is structurally excluded from every automated
  test; only `main.py`'s production path can submit.
- **+** Screenshots make regressions in ticket state (wrong price/qty)
  visible at a glance.
- **−** The final submit itself is only exercised in production; the
  buyer must watch the first real run (the broker's toast +
  orders-list verification are the production-side safety nets).
- **−** Smoke test needs live credentials and network access; it is not
  run in CI.
