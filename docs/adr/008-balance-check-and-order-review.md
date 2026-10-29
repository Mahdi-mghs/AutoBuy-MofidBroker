# ADR-008 — Read the balance, review the order, then burst

Status: accepted (2026-10-25)

## Context

The bot used to go straight from "price resolved" to "submit 20 times". Two
things were missing for a run with real money:

- **No balance check.** If the order costs more than the account holds, the
  burst is pointless: the broker rejects it (that is what the "balance" row of
  the refusal table in ADR-006 means) and the run ends with a generic
  "not accepted".
- **No confirmation of what is being bought.** The only trace was
  `API order prepared: {...}` — a raw dict — logged before the (sometimes
  hour-long) wait, so at 08:44:58 nothing on screen said *“1 × فولاد at the
  ceiling, 4,557 Rial”*.

The account money is a captured endpoint (`artifacts/api-report.md`,
2026-10-16):

```
GET /easy/api/money
→ {"t0":1268425599,"buyPowerT0":1261209968,"buyPowerT1":1261209968,
   "block":0,"credit":0,...}
```

All amounts are **Rial**, the same unit as `price` and `totalValue` in the order
payload — no conversion, no chance of a 10× mistake. `buyPowerT0` is
«قدرت خرید», the amount a T+0 buy may spend; `t0` is the plain cash balance
(«مانده قابل معامله»), which does not subtract blocks, hence the fallback order:
`buyPowerT0 → t0 → buyPowerT1 → t1 → buyPowerT2 → t2`.

## Decision

1. **Read the money during preparation**, not at the burst. The burst starts at
   08:44:58 and every millisecond before the first submit matters; a
   `GET /easy/api/money` there would cost ~100–200 ms of queue position.
   Preparation happens seconds-to-minutes earlier, so the balance is read once,
   there, and stored in the prepared dict (`buying_power`, `order_value`).
2. **Warn, never block.** If `order_value > buying_power` the bot logs two
   ⚠️ warnings (at preparation, and again in the review) and submits anyway. The
   broker is the final judge: a credit line (`credit`/`avandCredit`) can cover
   the gap, overdraft rules differ per account, and a genuine refusal stops the
   burst on the first submit anyway. Blocking would trade a loud, harmless
   warning for a silent missed buy at the open.
3. **One review block, printed right before the burst** (`log_order_review`),
   from values already in memory — no request at that point:
   symbol/ISIN, quantity, price + mode, commission, order value, buying power,
   balance left after the order (or the shortfall).
4. **A missing balance is not a failure.** Any error from `/easy/api/money`
   (WAF hiccup, schema change, a UI-prepared order with no API client) logs
   `balance check unavailable` and the run continues; a dead browser still
   raises `BrowserGone` (ADR-007) instead of being swallowed here.

## Consequences

- The balance is a snapshot from preparation time. If another session spends it
  in between, the review still shows the old number — the broker's rejection
  stays authoritative.
- Quantity/price are unchanged by the check: a short order still fires, so the
  warning must be read, not assumed to prevent anything.
- `order_value` uses the same rounding as the payload's `totalValue`, so the
  number in the review is the number the broker charges for.
- Tests: captured `/easy/api/money` fixture, fallback order, shortfall warning
  in `prepare_via_api` and in `log_order_review`, and an unavailable-balance
  path (no route → 500 → warn and continue).
