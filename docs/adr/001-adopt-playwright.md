# ADR-001: Adopt Playwright instead of Selenium

- **Status:** Accepted
- **Date:** 2026-10-15

## Context

The bot was built on Selenium 4 with a hand-managed ChromeDriver:

- `config.py` hard-coded a chromedriver path tied to a specific Chrome
  version (`…\chromedriver\win64\148…`, later `151…`), which breaks every
  time Chrome auto-updates.
- `webdriver-manager` was kept as an offline-hostile fallback that
  downloads a driver at runtime.
- Selenium's `WebDriverWait` + `expected_conditions` boilerplate appeared
  in every module, and stale-element handling had to be hand-written
  (`StaleElementReferenceException` retries in `broker.py` / `order.py`).

The user asked for a full rewrite on Playwright, automating the manual
steps that had accumulated (see ADR-002).

## Decision

Rewrite all browser interaction on the **Playwright Python sync API**:

1. Use `channel="chrome"` so Playwright drives the **installed system
   Google Chrome** — no chromedriver binary, no `webdriver-manager`, no
   version pinning.
2. Drop `selenium` and `webdriver-manager` from `requirements.txt`;
   Playwright + python-dotenv are the only runtime dependencies.
3. Replace `WebDriverWait/EC` chains with Playwright `Locator` objects,
   which auto-wait and auto-re-resolve the DOM node on every action —
   stale-element errors disappear by construction.
4. Keep the existing module layout (`driver.py`, `broker.py`, `order.py`,
   `main.py`, `locators.py`) so the rewrite is reviewable against the old
   code line-for-line.

## Consequences

- **+** One dependency manages browser download/launch/teardown; offline
  machines with Chrome installed work out of the box.
- **+** `launch_persistent_context` natively supports the per-instance
  `CHROME_USER_DATA_DIR` profiles used for multi-symbol runs.
- **+** `page.screenshot()` gives cheap visual verification artifacts
  (used by `smoke_test.py`).
- **−** Selenium-specific knowledge (By/EC/StaleElement) no longer applies;
  contributors must know Playwright locators.
- **−** The frozen legacy reference `manualBot_deepseek.py` can no longer
  run once Selenium is uninstalled; it is kept as documentation only.
