# AutoBuy-MofidBroker

Automated buy bot for Mofid Securities (EasyTrader platform), rewritten on **Playwright**.
Logs in, **prepares the order itself** (symbol, price, quantity), waits for the precise broker
clock time, then clicks the buy button repeatedly until accepted.

TIP: Persian documentation is [here](README-FA.md).

---

## Features

- **Fully automated preparation** — navigates to the symbol, opens the buy ticket, fills price and quantity; no more manual `Press ENTER` step
- **Price modes** — `ceiling` (سقف قیمت, default), `reference` (قیمت پایانی), or `fixed` price
- **Safety read-backs** — the ticket's symbol is asserted, and quantity/price are re-read after filling; the bot aborts instead of trading a wrong symbol
- **Precise timing** — busy-waits on broker clock (not system clock)
- **Multi-instance support** — run separate instances per symbol with isolated Chrome profiles
- **Order verification** — checks the orders list after a successful click
- **Robust button finding** — primary `data-cy` locator plus fallbacks; auto-retrying clicks
- **Notification detection** — toast capture plus DOM scan for Persian/English messages; failure keywords are checked before success ones
- **Manual fallback** — `--manual` restores the old "prepare it yourself" pause
- **Environment-based config** — credentials and settings via `.env` or env vars
- **Clean shutdown** — Ctrl+C properly closes the browser
- **No ChromeDriver** — Playwright drives your installed Google Chrome; no version hunting

---

## Installation

```bash
pip install -r requirements.txt
```

Playwright uses your installed **Google Chrome** (`channel="chrome"`), so no browser
download is required. If Chrome is missing, run:

```bash
playwright install chromium
```
and change `channel="chrome"` to `channel="chromium"` in `driver.py`.

Create `.env` from the template:

```bash
cp .env.example .env
# Edit .env with your credentials
```

---

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `MOFID_USER` | (required) | Your broker username |
| `MOFID_PASS` | (required) | Your broker password |
| `SYMBOL` | `""` | Symbol to trade, e.g. `فولاد` (required in auto mode) |
| `QUANTITY` | `0` | Number of shares (required > 0 in auto mode) |
| `PRICE_MODE` | `ceiling` | `ceiling` (سقف قیمت) \| `reference` (قیمت پایانی) \| `fixed` |
| `PRICE` | `0` | Explicit price — only used when `PRICE_MODE=fixed` |
| `DEFAULT_BUY_TIME` | `08:44:58` | Target time for buy (HH:MM:SS) |
| `BROKER_URL` | `https://d.easytrader.ir/` | Login URL |
| `VERIFY_ORDER` | `true` | Verify order in orders list |
| `CHROME_USER_DATA_DIR` | `""` | Separate Chrome profile per instance |
| `HEADLESS` | `false` | Run Chrome without UI |
| `INSTANCE_ID` | `""` | Identifier for multi-instance logs |
| `API_MODE` | `true` | Direct HTTP order mode (ADR-004) — prepares & submits over the EasyTrader API through the browser context; falls back to the UI path on any API failure |
| `API_BASE_URL` | `https://api-mts.orbis.easytrader.ir` | API base URL (captured endpoint) |
| `API_BUY_MAX_ATTEMPTS` | `20` | Order POSTs **at/after the open** (each is a real order). 20 × 350 ms ≈ 7 s of coverage — enough to reach the front of the buy queue (ADR-006) |
| `API_PREOPEN_MAX_ATTEMPTS` | `30` | Submits refused because the session has not opened yet (`11000`/`8702`). Retried without spending the budget above; bounded so a holiday can't loop |
| `API_BUY_RETRY_INTERVAL` | `0.35` | Seconds between those POSTs (send-to-send). The broker refuses orders closer than 300 ms (ADR-006) |
| `API_MIN_SUBMIT_INTERVAL` | `0.32` | Hard floor for that gap — measured as the fastest spacing the broker accepts |
| `API_ORDER_WINDOW_GRACE` | `8` | Session 08:45–12:35, order registration 08:45–12:30; the burst warns when the open is further away than this |
| `TOKEN_CACHE` | `true` | Reuse the previous run's JWT and skip the login when it still works (ADR-005) |
| `TOKEN_CACHE_PATH` | `.mofid_token.json` | Cache file (project-relative, git-ignored, `0600` on POSIX) |
| `TOKEN_CACHE_MIN_VALIDITY` | `120` | Only reuse a token valid for at least this many seconds |

See [`.env.example`](.env.example) for the full list (timeouts, retry counts, log file).

---

## Usage

### Single instance (auto-prepared order)

```bash
python main.py --time 08:44:58 --symbol فولاد
```

`--symbol` overrides `SYMBOL`; the target time defaults to `DEFAULT_BUY_TIME`.

### Skip order verification

```bash
python main.py --time 09:00:00 --no-verify
```

### Disable the API for one run

```bash
python main.py --no-api
```

With `API_MODE=true` (default) the bot logs in with Playwright and then does
everything else over EasyTrader's raw HTTP API (symbol lookup, ceiling price,
order submission, order verification) — much faster and race-free. Any API
failure automatically falls back to the proven UI path. See
[ADR-004](docs/adr/004-hybrid-http-api-mode.md) for the captured endpoints
and the safety rationale.

### Order cadence and the order window

The session runs **08:45:00–12:35:00** and the broker accepts new orders
**08:45–12:30**, refusing two submissions closer than **300 ms** apart
(`13021 OrderIntervalError`) — measured live, see
[ADR-006](docs/adr/006-order-cadence-and-window.md).

There are two kinds of "no" and they must not be confused:

| Broker answer | Meaning | What the burst does |
|---|---|---|
| `13021` | two submits < 300 ms apart; nothing reached the book | retry after the gate, free |
| `8702` / `11000` | the session has not opened yet (or is over) | before 08:45: keep submitting (free); after 12:30: stop |
| anything else (`8006`, bands, balance) | a real refusal of the order | stop — the answer will not change |

That is why the target is **08:44:58**: the first submits are refused with
`11000 "وضعیت گروه اجازه فعالیت نمی‌دهد"`, they cost nothing, and the burst is
still in flight at 08:45:00 when the book opens. After the open it has
`API_BUY_MAX_ATTEMPTS` (20) fresh submits at 350 ms — about 7 s — to land at or
near the front of the buy queue. An earlier version treated `11000` as a
rejection and gave up 3 s before the open on a real-money run.

### Balance check and the pre-burst review

Before the burst, the bot reads the account money (`GET /easy/api/money`, RIAL)
and prints what it is about to buy:

```
Order review:
  symbol        فولاد (IRO1FOLD0001)
  quantity      1 share(s)
  price         4,540 Rial (454 Toman)  [ceiling]
  commission    0.3712% → 17 Rial (1 Toman)
  order value   4,557 Rial (455 Toman)
  buying power  1,261,209,968 Rial (126,120,996 Toman)
  balance after 1,261,205,411 Rial (126,120,541 Toman)
```

If the order value is larger than the buying power (قدرت خرید, `buyPowerT0`), you
get a **warning** — at preparation time and again in the review — but the order
is still submitted: the broker is the final judge (a credit line can cover the
gap, and a real refusal stops the burst anyway). The balance is read during
preparation, *before* the clock wait, so the burst pays no extra round trip on
the opening millisecond.

### Reuse the cached API token (skip the login)

```bash
python main.py --time 08:44:58 --symbol فولاد   # run 1 — logs in, caches the JWT (~12 h)
python main.py --time 08:44:58 --symbol فولاد   # run 2 — no login: cached token reused
python main.py --no-token-cache                 # force a fresh Playwright login
```

The token harvested after a successful login is saved to `TOKEN_CACHE_PATH`.
The next run validates it with read-only calls (server clock, order list,
symbol + quote) and, if it still works, skips the login entirely and uses the
API clock instead of the on-page clock. A rejected token is deleted and the run
falls back to the normal login, so nothing changes for you when it expires.
The cache is bound to the configured `MOFID_USER` — changing accounts never
resumes the previous account's session. See
[ADR-005](docs/adr/005-cached-api-token.md) for the safety analysis.

### Legacy manual preparation

```bash
python main.py --manual
```

The bot logs in, then pauses so you can fill the ticket yourself — the old behavior.

### Multi-instance (one terminal per symbol)

Terminal 1:
```bash
INSTANCE_ID=folad CHROME_USER_DATA_DIR=C:\chrome-folad SYMBOL=فولاد QUANTITY=10 python main.py
```

Terminal 2:
```bash
INSTANCE_ID=kalan CHROME_USER_DATA_DIR=C:\chrome-kalan SYMBOL=کالن QUANTITY=5 python main.py
```

Each instance has its own Chrome profile (no cookie/login conflicts) and tagged log lines.

---

## Workflow

1. **Auth** — a cached API token when one is still valid (ADR-005), otherwise an automated Playwright login
2. **Preparation** — automated: symbol lookup + live quote over HTTP (API mode), else sidebar search → exact symbol → buy ticket → quantity → price per `PRICE_MODE` → read-back verification
3. **Time wait** — waits for the broker clock ≥ target
4. **Auto-buy** — clicks buy up to 25 times, 100 ms apart, until the broker accepts
5. **Verification** — checks the orders list for the new order

If the target time has already passed during preparation, the bot exits **without buying**.

---

## Testing

Fast, browser-free tests (timing logic, notification classification, orchestration wiring):

```bash
pip install -r requirements-dev.txt
python -m pytest tests -q
```

Real-browser smoke test — runs the whole flow **except the buy click**, screenshots each
stage to `artifacts/`:

```bash
python smoke_test.py --symbol فولاد
```

It asserts the ticket symbol/quantity/price and that the buy button is visible, then exits
without submitting. See [docs/adr/003](docs/adr/003-real-browser-testing-without-submitting.md).

---

## Documentation

- [ADR-001 — Adopt Playwright instead of Selenium](docs/adr/001-adopt-playwright.md)
- [ADR-002 — Automate order preparation](docs/adr/002-automate-order-preparation.md)
- [ADR-003 — Real-browser testing without submitting](docs/adr/003-real-browser-testing-without-submitting.md)
- [ADR-004 — Hybrid HTTP API mode](docs/adr/004-hybrid-http-api-mode.md)
- [ADR-005 — Cached API token (skip the login)](docs/adr/005-cached-api-token.md)
- [ADR-006 — Order cadence and the order window (measured)](docs/adr/006-order-cadence-and-window.md)
- [ADR-007 — Graceful shutdown on Ctrl+C](docs/adr/007-graceful-shutdown-on-ctrl-c.md)
- [ADR-008 — Balance check and the pre-burst order review](docs/adr/008-balance-check-and-order-review.md)
- [Glossary](docs/glossary.md) — trading terms (Persian/English) + bot internals

---

## Logging

All logs write to console (stdout) and `autobuy.log` (configurable via `LOG_FILE`):

```
2026-10-15 11:00:08 [INFO] order: Order prepared: {'symbol': 'فولاد', 'quantity': 1, 'price': 4540, 'price_mode': 'ceiling'}
```

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| "Login failed" | Check credentials in `.env` |
| "No symbol configured" / "No quantity configured" | Set `SYMBOL` and `QUANTITY` (> 0), or use `--manual` |
| "Order ticket opened for X, expected Y" | Symbol selection picked the wrong row twice — check the symbol spelling; the bot aborts rather than trading the wrong instrument |
| "Layout overlay still active" | A modal/panel blocked the page; re-run (the bot already retries Escape/backdrop dismissal) |
| "Broker clock not found" | Wait for full page load; check the network connection |
| "Target time already passed" | Start earlier, or pass a later `--time` |
| "Order not accepted after all retries" | Manual check; broker may have rejected due to price/quantity |
| `11000` / `8702` in the log | The session is not open (or already closed). The burst retries these for free; if it exhausts `API_PREOPEN_MAX_ATTEMPTS`, the target is too far from 08:45 or the market is closed today |
| `8006` in the log | Order value is below the **500 000 Toman** minimum — raise `QUANTITY` |
| Ctrl+C before the buy | Stops immediately: `Stop requested — leaving the clock wait`, then `Exiting before buy` and exit 0. The console event also kills the Playwright driver, so cleanup may log `Browser was already closed` — that is expected, not an error (ADR-007) |
| Ctrl+C during/after the buy | The run stops too, with a `check the orders tab manually` warning — an order may already have reached the broker |
| `Browser/Playwright session lost` | The browser or driver died (Chrome closed, crash). Exit 1; nothing further is submitted |

---

## Legacy note

`manualBot_deepseek.py` is kept as a **reference only**: it is the pre-rewrite Selenium
script (hardcoded credentials/driver path). Selenium is no longer a dependency, so it no
longer runs — use `main.py --manual` for the equivalent manual flow.

---

## Warning

This tool automates sensitive financial operations.
Test thoroughly in a safe environment before trading real money.
Use at your own risk.

API mode talks to **undocumented, unofficial broker endpoints** captured from the
web app (ADR-004). They can change or be blocked without notice; the bot falls
back to the UI path when they do, but treat API order results with extra care.

---

## License

MIT
