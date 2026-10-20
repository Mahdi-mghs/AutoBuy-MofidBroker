# ADR-007 — Ctrl+C must stop the bot, not the log

Status: accepted (2026-10-20)

## Context

Pressing Ctrl+C before the buy did not stop the bot. It printed a wall of
Playwright errors and sat there:

```
[WARNING] broker: Clock read failed (Locator.inner_text: Target page, context or browser has been closed), retrying...
[WARNING] broker: Clock read failed (Connection closed while reading from the driver), retrying...
[WARNING] broker: Clock read failed (Playwright connection closed), retrying...
   … ~1200 lines, one per poll, for up to an hour
```

Two independent defects produced that:

1. `_signal_handler` only set `_shutdown_requested`. `wait_until_broker_time`
   never looked at it, so the "wait for 08:44:58" loop kept polling. The flag
   was checked once, *after* the wait returned — i.e. after the target time,
   which is exactly when the user no longer wants to wait.
2. On Windows the console Ctrl+C event is delivered to every process attached
   to the console, including the Playwright **driver** process
   (`playwright/driver/node.exe`). The driver exits, the browser/connection
   goes with it, and the in-flight page call fails with `TargetClosedError`
   ("Target page, context or browser has been closed") — not
   `KeyboardInterrupt`. The wait loop treated that as a transient
   "clock read failed, retrying…" and retried it ~20×/s until the 1 h
   `CLOCK_DEADLINE_SECONDS`.

The app cannot prevent the console event from reaching the driver:
`PipeTransport` spawns it without `CREATE_NEW_PROCESS_GROUP`, and a process can
only make *itself* ignore Ctrl+C. So a dead session after Ctrl+C is the normal
case and has to be handled, not avoided.

Reproduced (and now regression-tested) with a real browser: spawn the bot in
its own console process group, send a genuine `CTRL_C_EVENT` mid-wait.

## Decision

1. **The clock wait is interruptible.** `wait_until_broker_time(...,
   should_stop=...)` polls the callable every iteration; when it returns True the
   wait returns `None` and `main()` exits before the buy. `main()` passes
   `lambda: _shutdown_requested`.
2. **A dead page is not a transient error.** `driver.is_browser_gone(exc)` walks
   the cause chain for target/connection-closed markers, and
   `driver.require_live_browser(exc)` re-raises it as `driver.BrowserGone`.
   Every broad `except Exception` that would otherwise retry or fall back calls
   it first: the clock wait, the API burst (`execute_buy_api`), the token-resume
   and API-prep paths, `login()`.
3. **One line, clean exit.** `main()` catches `BrowserGone` and logs either
   `Interrupted by user (Ctrl+C) — stopping.` (exit 0) or
   `Browser/Playwright session lost (…) — stopping.` (exit 1), then runs the
   normal `finally` cleanup. If the burst had already started it adds
   "check the orders tab manually" instead of claiming nothing was sent.
4. **Cleanup tolerates a corpse.** `cleanup_all()` logs "browser was already
   closed" at INFO when the connection is gone, instead of a warning per close
   attempt, and never raises.
5. **No traceback for this class of error anywhere.** `main()`'s generic
   handler also classifies a stray closed-target error as a shutdown, so a dead
   browser during login/profile setup cannot dump a stack trace either.

## Consequences

- Ctrl+C before the buy: about one log line, exit 0, browser closed (by
  Playwright's own teardown or by us, whichever wins the race).
- Ctrl+C at/after the buy: same exit, plus the "check the orders tab" warning —
  a submit may already have reached the broker.
- A browser that dies for another reason (Chrome crash, driver OOM) now ends the
  run in a second with exit 1 instead of polling for an hour.
- Tests: `CLOCK_POLL_INTERVAL=0` plus `CLOCK_DEADLINE_SECONDS=5` keeps a broken
  wait loop failing fast instead of hanging the suite.
