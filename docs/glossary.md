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
| **Market hours** | Tehran session 08:45:00–12:35:00. New orders are only accepted 08:45–12:30 (see **Order window**). `MARKET_LATEST` (15:30) is not the session end — it is only the upper bound the broker-clock wait refuses to cross. |
| **Orders list / سفارش‌ها / سفارشات** | Panel (`d-order-list[data-cy="order-list-container"]`) listing submitted orders; used to verify an accepted order. |
| **Submit buy / ارسال خرید** | The green button that actually sends the order (`oms-order-form-submit-button-buy`). The only action that costs money. |
| **Draft / پیش‌نویس** | Secondary button that saves an order without sending it (`oms-order-form-draft-button-buy`). Never clicked by the bot. |
| **Order interval rule / فاصله سفارش** | The broker refuses two order submissions closer than 300 ms apart (`13021 OrderIntervalError`) — measured live; the bot's submit gate keeps 320 ms+ (ADR-006). |
| **Order window / ساعت مجاز ثبت سفارش** | 08:45–12:30, the hours the broker accepts orders (`8702` otherwise). Not the same as the trading session hours (08:45–12:35). |
| **Buying power / قدرت خرید** | How much a buy may spend today, from `GET /easy/api/money` → `buyPowerT0` (RIAL). Powers the pre-burst balance check (ADR-008). |
| **Tradable balance / مانده قابل معامله** | The plain cash balance (`t0`). Used only when `buyPowerT0` is absent — it does not subtract blocks. |
| **Order review** | The block `log_order_review` prints right before the burst: symbol, quantity, price, commission, order value, buying power, balance left (or the shortfall). No request at that point — the numbers come from preparation. |
| **Pre-open refusal** | A submit refused because the session has not opened yet — `8702` ("ساعت مجاز ثبت سفارش…") or, just before 08:45, `11000` ("وضعیت گروه اجازه فعالیت نمی‌دهد"). Nothing reaches the book, so the burst retries: this is how the bot is in flight at 08:45:00. After 12:30 the same answers mean "closed for the day" and stop the burst. |
| **Submit gate** | `EasyApiClient`'s send-to-send throttle: it sleeps before a POST so consecutive orders are never closer than the floor. |
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
| **API mode (`API_MODE`)** | Direct HTTP path (ADR-004): symbol lookup, quote, order submit and verification over EasyTrader's own API through Playwright's `context.request` (browser TLS, so the gateway's WAF is satisfied). |
| **WAF challenge** | The gateway's HTML block page returned to non-browser TLS fingerprints (plain `requests`); the reason all live HTTP goes through Chromium's network stack. |
| **JWT / bearer token** | The ~1888-char `Authorization: Bearer …` the web app sends on every API call; harvested from the page's own requests (ADR-004), cached for reuse (ADR-005). |
| **Token cache** | `.mofid_token.json` — the saved JWT plus its expiry and a hash of the configured account; validated with read-only calls at startup, deleted on 401 (ADR-005). |
| **API clock (`ApiClock`)** | Broker time derived from ONE `GET /easy/api/account/server-time/{ms}` sync plus local drift, used when the browser UI is not logged in. Polling that endpoint at the 50 ms on-page clock rate would be ~20 req/s. |
| **API buy burst** | Up to `API_BUY_MAX_ATTEMPTS` (20) order POSTs **after the open**, `API_BUY_RETRY_INTERVAL` (0.35 s) apart — ~7 s of queue coverage. Pre-open refusals have their own budget (`API_PREOPEN_MAX_ATTEMPTS`, 30) and never spend it. Stops on an accepted + verified order, or on a real refusal (`8006`, bands, balance). |
| **Account fingerprint** | `sha256(MOFID_USER)[:16]` stored with the cached token so a changed account can never resume the previous account's session. |
| **Graceful shutdown (Ctrl+C)** | `_shutdown_requested` is set by the signal handler; the clock wait (`should_stop`) and every retry loop (`driver.require_live_browser`) stop at the next check. The console event also kills the Playwright driver, so `driver.is_browser_gone` treats a closed target/connection as fatal instead of a transient error — retrying one is what used to flood the log (ADR-007). |
| **BrowserGone** | `driver.BrowserGone`: the page/driver is unusable (Ctrl+C, Chrome crash). `main()` turns it into one line and an exit (0 after Ctrl+C, 1 otherwise); nothing else is submitted. |
| **Size-warning overlay** | `<size-warning>` modal the site shows below the 1024px breakpoint; blocks all clicks — hence the pinned 1600×1000 viewport. |
