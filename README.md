# AutoBuy-MofidBroker

Automated buy bot for Mofid Securities (EasyTrader platform).  
Waits for a precise broker clock time, then clicks the buy button repeatedly until accepted.

---

## Features

- **Precise timing** — Busy-waits on broker clock (not system clock)
- **Multi-instance support** — Run separate instances per symbol with isolated Chrome profiles
- **Order verification** — Checks the orders tab after successful click
- **Robust button finding** — Multiple fallback locators for the buy button
- **Notification detection** — DOM scan for Persian/English success messages
- **Environment-based config** — Credentials and settings via `.env` or env vars
- **Clean shutdown** — Ctrl+C properly closes the Chrome browser

---

## Installation

```bash
pip install -r requirements.txt
```

### ChromeDriver

This project uses a **local ChromeDriver** (offline-friendly).  
Edit `.env` to point to your driver:

```
CHROMEDRIVER_PATH=C:\Users\Mahdi\.cache\selenium\chromedriver\win64\148.0.7778.97\chromedriver.exe
```

If your driver is on your `PATH`, leave this empty and it will auto-discover.

---

## Configuration (`config.py`)

| Variable | Default | Description |
|----------|---------|-------------|
| `MOFID_USER` | (required) | Your broker username |
| `MOFID_PASS` | (required) | Your broker password |
| `DEFAULT_BUY_TIME` | `08:44:58` | Target time for buy (HH:MM:SS) |
| `SYMBOL` | `""` | Symbol name for log context |
| `BROKER_URL` | `https://d.easytrader.ir/` | Login URL |
| `VERIFY_ORDER` | `true` | Verify order in orders list |
| `CHROME_USER_DATA_DIR` | `""` | Separate Chrome profile per instance |
| `HEADLESS` | `false` | Run Chrome without UI |
| `INSTANCE_ID` | `""` | Identifier for multi-instance logs |

Create `.env` from template:

```bash
cp .env.example .env
# Edit .env with your credentials
```

---

## Usage

### Single instance (default time)

```bash
python main.py
```
Uses `DEFAULT_BUY_TIME` (08:44:58).

### Specify time

```bash
python main.py --time 09:00:00
```

### Multi-instance (one terminal per symbol)

Terminal 1:
```bash
INSTANCE_ID=folad CHROME_USER_DATA_DIR=C:\chrome-folad python main.py
```

Terminal 2:
```bash
INSTANCE_ID=kalan CHROME_USER_DATA_DIR=C:\chrome-kalan python main.py
```

Each instance:
- Has its own Chrome profile (no cookie/login conflicts)
- Logs show `[instance: folad]` or `[instance: kalan]`

---

## Workflow

1. **Login** — Automated via Selenium
2. **Manual setup** — User prepares symbol/price/quantity (script waits for Enter)
3. **Time wait** — Waits for broker clock ≥ target time
4. **Auto-buy** — Clicks buy button up to 25 times, 150ms apart
5. **Verification** — Optionally checks orders tab for confirmation

---

## Logging

All logs write to:
- Console (stdout)
- `autobuy.log` (configurable via `LOG_FILE`)

Log format:
```
2026-06-10 12:20:17 [INFO] main: AutoBuy-MofidBroker starting (instance: folad) - Symbol: فولاد
```

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| "Login failed" | Check credentials in `.env` |
| "Buy button not found" | Make sure the symbol page is loaded and form is visible |
| "Broker clock not found" | Wait for full page load; check network connection |
| Silent Chrome after Ctrl+C | Ensure `--user-data-dir` isn't locked by another process |
| "Order not accepted after all retries" | Manual check; broker may have rejected due to price/quantity |

---

## Warning

This tool automates sensitive financial operations.  
Test thoroughly in a safe environment before trading real money.  
Use at your own risk.

---

## License

MIT