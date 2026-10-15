# ADR-002: Automate order preparation (kill the manual pause)

- **Status:** Accepted
- **Date:** 2026-10-15

## Context

The original flow stopped after login:

```
>>> NOW PREPARE THE ORDER MANUALLY <<<
input("Press ENTER when ready ...")
```

The human had to (1) find the symbol, (2) type price and quantity, and
(3) make sure the buy button was visible — introducing arbitrary delay
between login and the target-time click, and making the bot unusable
unattended. Symbol-search logic existed only in the legacy
`manualBot_deepseek.py` (`find_symbol`) and was never wired into the
main flow.

Interview decisions (grill session, 2026-10-15):

- **Full auto with fallback** — automate everything, keep a `--manual`
  flag that restores the old pause.
- **Configurable price modes**, default **ceiling**.
- **Dry-run testing only** — the production path always buys; no
  permanent `--dry-run` flag (test harness covers verification).

## Selectors (verified against the live DOM, 2026-10-15)

The site's `data-cy` attributes sit on Angular **custom elements**
(`<ui-button>`, `<input-widget>`), not always on the native node:

| Step | Selector |
|------|----------|
| Search tab | `li[data-cy="search-menu-icon"]` |
| Search input | `input[data-cy="layout-search-input"]` (legacy `#searchInputControl` is gone) |
| Exact result | `[data-cy="search-item-name-فولاد"]` (data-cy embeds the Persian name) |
| Open buy ticket | `[data-cy="order-buy-btn"]` |
| Ticket header (assert symbol) | `[data-cy="order-form-header-symbol-name"]` |
| Quantity field | `input[data-cy="order-form-input-quantity"]` |
| Price field | `input[data-cy="order-form-input-price"]` |
| Ceiling chip (سقف قیمت) | `[data-cy="order-form-max-price"]` |
| Floor chip (کف قیمت) | `[data-cy="order-form-min-price"]` |
| Closing price (reference) | `[data-cy="symbol-header-closing-price"]` |
| **Buy submit** | `[data-cy="oms-order-form-submit-button-buy"] button` |

Gotchas encoded in `locators.py`:

- The buy button is `<ui-button data-cy="…"><button aria-label="ارسال خرید">`
  — a `button[data-cy=…]` selector never matches.
- The site shows a `<size-warning>` overlay below the 1024px breakpoint
  that **intercepts every click**; the browser viewport is pinned to
  1600×1000.

## Decision

New `prepare_order(page, symbol, …)` in `order.py` replaces the pause:

1. Open sidebar search, type the symbol, click the exact
   `search-item-name-{symbol}` result (keyboard Down+Enter as fallback).
2. Click `order-buy-btn` to open the ticket; **assert** the ticket header
   shows the requested symbol — abort rather than trade the wrong symbol.
3. Fill quantity (`QUANTITY` env / parameter).
4. Fill price according to `PRICE_MODE`:
   - **`ceiling`** (default) — click the `order-form-max-price` chip
     (سقف قیمت, the exchange band maximum) and verify the field took it;
     if the chip didn't populate the field, type the chip's own value.
   - **`reference`** — read `symbol-header-closing-price` (قیمت پایانی,
     used here as the reference-price stand-in; this UI exposes no
     dedicated قیمت مرجع control for equities) and type it.
   - **`fixed`** — type the explicit `PRICE` value.
5. Re-read both fields and raise if quantity ≠ configured or price ≤ 0 —
   **never proceed half-prepared**.

### Async ticket defaults (found live, 2026-10-15)

The ticket is an Angular reactive form fed by server defaults (quantity
10,000, last-trade price) that load *after* the ticket opens. During the
real-browser run a default landed **after** our fill and silently replaced
it (fixed-price run: quantity came back as 10,000 instead of 1) — the
read-back caught it and aborted, which is the correct failure, but the run
was ruined. Preparation therefore:

- waits ~700 ms after the ticket opens for defaults to settle, and
- fills through `_fill_verified()` (fill → read back → refill, up to 3×),
  so a late-arriving server value can never win.

All three price modes (`ceiling`, `reference`, `fixed`) were then verified
end-to-end against the live site with the no-submit smoke test.

`main.py` gains `--symbol` (overrides `SYMBOL`) and `--manual` (legacy
pause). Auto mode refuses to start without `QUANTITY > 0`.

## Consequences

- **+** Unattended runs; preparation takes ~2s instead of human seconds-to-minutes.
- **+** Mis-selection protection: header-symbol assertion + field read-back.
- **−** `reference` mode approximates قیمت مرجع with the displayed closing
  price; on instruments where the broker exposes a true reference control,
  this should be revisited.
- **−** Selector drift on UI releases now fails loudly (RuntimeError)
  instead of silently waiting for a human.
