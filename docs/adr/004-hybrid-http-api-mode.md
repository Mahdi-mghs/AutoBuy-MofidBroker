# ADR-004 — Hybrid HTTP API mode for orders

**Status:** implemented (2026-10-16) · **Branch:** `api-method`

## Context

The bot submitted orders by driving the UI (`prepare_order` → `execute_buy`):
open ticket, fill fields, wait for clock, click ارسال خرید, capture the toast.
Each attempt costs ~0.5–2s of latency. Community projects referenced an
`OmsOrder` endpoint (`https://d11.emofid.com/easy/api/OmsOrder`), but those
references are 2019-era and unverified against today's app.

## Decision

Capture the live app's real traffic, then implement a **hybrid flow**:
Playwright still performs the login (OIDC PKCE at `login.emofid.com`), but
everything else — symbol lookup, live quote, order submission, order
verification — goes over raw HTTP. The UI path is kept intact as an automatic
fallback. `API_MODE=true` (default) enables it; `--no-api` forces the UI for
one run.

## Captured facts (live, 2026-10-16)

Discovered with `capture_endpoints.py` (zero-submit: the submit POST is
logged and `route.abort()`ed before it reaches the broker). Full data in
`artifacts/api-report.md` (regenerate any time; artifacts/ is gitignored).

- **Base URL moved**: `https://api-mts.orbis.easytrader.ir` (micro-service
  style paths: `/core/api/…`, `/symbols/api/…`, `/easy/api/…`). The old
  `d11.emofid.com` host from community code is gone.
- **Submit endpoint**: `POST /core/api/v2/order`
  (NOT `OmsOrder` — the community reference was wrong/stale).
- **Payload** (captured from the real buy click; `side: 0` = BUY):

  ```json
  {"order":{"price":4540,"quantity":1,"side":0,"validityType":0,
    "createDateTime":"10/16/2026, 7:17:58 PM","commission":0.003712,
    "symbolIsin":"IRO1FOLD0001","symbolName":"فولاد",
    "orderModelType":1,"totalValue":4557,"orderFrom":34}}
  ```

  - `totalValue = round(price × quantity × (1 + commission))`
  - `createDateTime` = en-US 12-hour, no leading zero on the hour
  - `commission` = per-symbol buy fee, read from `marketdata/v2.buyRatio`
- **Other endpoints in use**: `POST /symbols/api/symbols/all` (name→ISIN
  table), `POST /symbols/api/marketdata/v2` (quote, ceiling
  `highAllowedPrice`, volume limits), `GET /core/api/order` (verification),
  `GET /easy/api/account/server-time/{ms}` (server clock).
- **Auth**: `Authorization: Bearer <JWT ~1888 chars>` harvested from the
  page's own outbound requests (fresh token each run; no token storage).
- **WAF**: the API gateway challenges non-browser TLS fingerprints — a
  python-`requests` call gets an HTML WAF page even with a valid token
  (verified live). The client therefore sends through Playwright's
  APIRequestContext (`context.request`), i.e. the real Chromium network
  stack. GET rows must not carry a `content-type` header (WAF-trigger).

## Consequences

- Order submission latency drops from clicks+toast (~1s+) to one HTTP round
  trip (~100–300ms), and repeat bursts are identical HTTP POSTs instead of
  DOM re-clicks.
- Validation moves server-side: price/quantity limits come from
  `marketdata/v2` (`minValidBuyVolume`, `highAllowedPrice`), so no read-back
  races with async ticket defaults.
- **Fragility**: these are undocumented endpoints; they can change without
  notice. The client fails loudly (EasyApiError) and the orchestrator falls
  back to the proven UI path, so the bot never fails silently.
- Because acceptance responses are shape-unverified (the capture aborted the
  first submit), success is defined by the **order list** (`/core/api/order`),
  not by the submit response. On 2xx-but-unverified the API path STOPS
  instead of retrying, to avoid duplicate orders; main() then requires manual
  check rather than auto-clicking the UI (duplicate-order risk).
- `execute_buy_api` mirrors `execute_buy`'s burst semantics but succeeds on the
  first HTTP 2xx + order-verification instead of the toast keyword heuristic.
  The burst spacing and the broker's 300 ms order-interval rule (13021) are
  covered in [ADR-006](006-order-cadence-and-window.md); the response body is
  `isSuccessful`/`omsError`, so a rejection is now recognised structurally.

## Testing

- `tests/test_api.py`: byte-faithful payload vs the captured JSON, price
  resolution, prep semantics, header rules (no `content-type` on GET), 401 +
  WAF-body handling, retry-vs-stop behaviour — all on MockTransport fixtures
  cloned from the capture.
- Full live read-path verification was run (server time, symbol lookup, quote,
  order list, buildable payload) — see the 2026-10-16 `VERIFY_RESULT: ALL_OK`
  log in the session transcript.
- `python smoke_test.py --symbol فولاد` still exercises the UI path as before.
