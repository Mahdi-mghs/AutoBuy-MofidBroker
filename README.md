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

1. **Login** — automated with Playwright
2. **Preparation** — automated: sidebar search → exact symbol → buy ticket → quantity → price per `PRICE_MODE` → read-back verification
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
| Silent Chrome after Ctrl+C | Make sure `--user-data-dir` isn't locked by another process |

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

---

## License

MIT
