# Glossary

Terms used in this codebase. Persian trading vocabulary (as shown in the
EasyTrader UI) is kept next to its English explanation.

## Trading / market

| Term | Definition |
|------|------------|
| **Broker (کارگزاری)** | Licensed intermediary that routes orders to the exchange. This bot talks to **Mofid Securities (مفید)**. |
| **EasyTrader (ایزی‌تریدر)** | Mofid's web trading platform at `d.easytrader.ir` — the site under automation. |
| **Symbol / نماد** | Exchange-traded instrument ticker (e.g. `فولاد`). The bot searches for it and must open its own order ticket. |
| **Order ticket / برگه سفارش** | The form with price, quantity and the submit button for one symbol (`order-buy-btn` opens it). |
| **Ceiling price / سقف قیمت** | Highest price allowed today for the symbol (previous close + daily band, ±%5 or ±%10 by instrument). `PRICE_MODE=ceiling` clicks the `order-form-max-price` chip. |
| **Floor price / کف قیمت** | Lowest allowed price today (previous close − band). Chip `order-form-min-price`. |
| **Reference price / قیمت مرجع** | Broker-provided fair price (prior close for equities; volume-weighted for some funds). `PRICE_MODE=reference` uses the displayed closing price (`symbol-header-closing-price`) as its stand-in — this UI shows no dedicated control for equities (see ADR-002). |
| **Closing price / قیمت پایانی** | Last settled price of the previous session; basis for today's band. |
| **Fixed price / قیمت ثابت** | An explicit price typed by the user (`PRICE_MODE=fixed` + `PRICE`). |
| **Quantity / تعداد** | Number of shares to buy. Iranian board-lot rules: multiples of 1 (most symbols' lot = 1 share today; the ticket enforces min/max via `order-form-min/max-quantity`). |
| **Broker clock / ساعت بورس** | Exchange server time shown in-page (`#easy-clock-id`). The bot waits on *this* clock, not the system clock, before clicking. |
| **Market hours** | Continuous trading 08:30–15:00 Tehran time; the bot refuses targets after 15:30 (`MARKET_LATEST`). |
| **Orders list / سفارش‌ها / سفارشات** | Panel (`d-order-list[data-cy="order-list-container"]`) listing submitted orders; used to verify an accepted order. |
| **Submit buy / ارسال خرید** | The green button that actually sends the order (`oms-order-form-submit-button-buy`). The only action that costs money. |
| **Draft / پیش‌نویس** | Secondary button that saves an order without sending it (`oms-order-form-draft-button-buy`). Never clicked by the bot. |
| **Toast / notification** | Brief broker message confirming or rejecting an order, hosted in `<app-notify>`; keywords are classified by `is_order_accepted`. |

## Bot internals

| Term | Definition |
|------|------------|
| **Auto mode** | Default flow: bot navigates, fills price/quantity itself (`prepare_order`), waits, buys. |
| **Manual mode (`--manual`)** | Legacy flow: bot pauses at `input("Press ENTER when ready")` and the human fills the ticket. |
| **Price mode (`PRICE_MODE`)** | `ceiling` (default) \| `reference` \| `fixed` — how the order price is decided (ADR-002). |
| **Dry run / smoke test** | `smoke_test.py`: full real-browser flow that stops before the submit click — verifies readiness without placing an order (ADR-003). |
| **Preparation read-back** | After filling, `prepare_order` re-reads quantity/price and aborts if they don't match — no half-prepared tickets. |
| **Async ticket defaults** | Server-side values (quantity 10,000, last trade price) the order ticket loads after opening; they can overwrite an early fill, hence `_fill_verified` (fill → read → refill). |
| **Buy burst** | Up to `BUY_MAX_ATTEMPTS` (25) clicks at `BUY_RETRY_INTERVAL` (0.10s) once the target time hits; stops at first accepted response. |
| **Persistent context** | Playwright's per-profile browser session; corresponds to the Selenium-era `CHROME_USER_DATA_DIR` multi-instance isolation. |
| **data-cy attribute** | Test-hook attribute the site places on custom elements (`<ui-button>`, …); primary selector strategy in `locators.py`. |
| **Size-warning overlay** | `<size-warning>` modal the site shows below the 1024px breakpoint; blocks all clicks — hence the pinned 1600×1000 viewport. |
