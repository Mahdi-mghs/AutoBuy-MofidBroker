"""
Endpoint discovery (ADR-004) — capture the real HTTP traffic the EasyTrader
web app generates, so the API client is built from facts instead of the
2019-era community references.

What it does (zero trading risk):
1. Playwright login (same flow as the bot).
2. Logs EVERY non-asset request + JSON response to
   artifacts/network-<ts>.ndjson (url, method, status, redacted headers,
   bodies — credentials/token values in headers are masked).
3. Drives the UI: symbol search → buy ticket → fills (same tested path as
   prepare_order), then arms a SAFE-SUBMIT GATE: from that moment every
   POST/PUT is logged and route.abort()ed, so nothing can reach any
   broker server. The submit click happens under this gate.
4. Parses the capture into artifacts/api-report.md: endpoint templates,
   auth header shape, symbol-search endpoint, ISIN, submit payload,
   response schemas.

Usage:
    python capture_endpoints.py --symbol فولاد
    python capture_endpoints.py --symbol فولاد --with-draft  # real draft POST (harmless record)

Requires real credentials in .env (MOFID_USER / MOFID_PASS).
"""

import argparse
import base64
import json
import re
import sys
from datetime import datetime

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

import locators
import config
from config import DEFAULT_TIMEOUT, USERNAME, PASSWORD
from driver import initialize_driver, cleanup_all

# Noise to skip entirely
_SKIP_URL = re.compile(
    r"sapi\.emofid\.com|raven|sentry|/envelope/|\.js($|\?)|\.css($|\?)|\.woff2?(%|\?|$)|"
    r"\.png($|\?)|\.svg($|\?)|\.jpg($|\?)|\.ico($|\?)|\.map($|\?)|\.ttf($|\?)|\.gif($|\?)|livechat",
    re.IGNORECASE,
)

REDACTED_HEADERS = {"authorization", "cookie", "x-api-key", "token", "auth-token", "auth"}
MAX_BODY_CHARS = 4000


def _redact_value(name: str, value: str) -> str:
    """Keep structure, hide the secret: prefix + length."""
    v = value or ""
    if name.lower() in REDACTED_HEADERS:
        prefix = v[:12] if len(v) > 12 else v
        return f"{prefix}…[len={len(v)}]"
    if "pass" in name.lower() or "user" in name.lower():
        return "[redacted]"
    return v


class TrafficRecorder:
    """Collects request/response facts into a list, dumped as ndjson at the end."""

    def __init__(self, page: Page):
        self.page = page
        self.records: list[dict] = []

    def attach(self):
        self.page.on("request", self._on_request)
        self.page.on("response", self._on_response)
        self.page.on("requestfailed", self._on_failed)

    # ── listeners ───────────────────────────────────────────
    def _on_request(self, request):
        try:
            url = request.url
            if _SKIP_URL.search(url):
                return
            headers = {k: _redact_value(k, v) for k, v in request.headers.items()}
            body = None
            if request.method in ("POST", "PUT", "PATCH"):
                body = request.post_data
                if body and len(body) > MAX_BODY_CHARS:
                    body = body[:MAX_BODY_CHARS] + f"...[truncated, total {len(body)}]"
            self.records.append(
                {
                    "ts": datetime.now().isoformat(timespec="milliseconds"),
                    "type": "request",
                    "method": request.method,
                    "url": url,
                    "headers": headers,
                    "body": body,
                }
            )
        except Exception as e:  # never let a listener crash the page
            self.records.append({"type": "error", "on": "request", "error": str(e)})

    def _on_response(self, response):
        try:
            url = response.url
            if _SKIP_URL.search(url):
                return
            rec = {
                "ts": datetime.now().isoformat(timespec="milliseconds"),
                "type": "response",
                "method": response.request.method,
                "url": url,
                "status": response.status,
                "content_type": response.headers.get("content-type", ""),
            }
            try:
                if "json" in rec["content_type"] or "text/plain" in rec["content_type"]:
                    body = response.text()
                    if len(body) > MAX_BODY_CHARS:
                        body = body[:MAX_BODY_CHARS] + f"...[truncated, total {len(body)}]"
                    rec["body"] = body
            except Exception as e:
                rec["body_error"] = str(e)
            self.records.append(rec)
        except Exception as e:
            self.records.append({"type": "error", "on": "response", "error": str(e)})

    def _on_failed(self, request):
        try:
            url = request.url
            if _SKIP_URL.search(url):
                return
            self.records.append(
                {
                    "ts": datetime.now().isoformat(timespec="milliseconds"),
                    "type": "requestfailed",
                    "method": request.method,
                    "url": url,
                    "failure": request.failure,
                }
            )
        except Exception:
            pass

    def dump(self, path):
        with open(path, "w", encoding="utf-8") as f:
            for rec in self.records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ── Safe submit gate: aborts EVERY mutating request while armed ──
class SafeSubmitGate:
    """
    Armed right before the submit click. While armed, every POST/PATCH/PUT
    is logged in full and aborted — the broker never receives it. Clears
    itself when disarm() runs (after the capture window).
    """

    def __init__(self, page: Page):
        self.page = page
        self.armed = False
        self.captured: list[dict] = []

    def _read_body(self, req) -> tuple[str | None, str | None]:
        """(body_string, body_b64) — binary bodies (gzip etc.) are b64'd."""
        try:
            body = req.post_data
            return (body, None) if body is None or len(body) <= MAX_BODY_CHARS else \
                   (body[:MAX_BODY_CHARS] + f"...[truncated, total {len(body)}]", None)
        except Exception:
            try:
                buf = req.post_data_buffer
                b64 = base64.b64encode(buf[:2000]).decode() if buf else None
                return (None, b64)
            except Exception as e:
                return (None, f"unavailable: {e}")

    def _handler(self, route):
        req = route.request
        if not self.armed:
            try:
                route.continue_()
            except Exception:
                pass
            return
        # While armed: any mutating verb is captured + aborted.
        if req.method in ("POST", "PUT", "PATCH"):
            try:
                headers = {k: _redact_value(k, v) for k, v in req.headers.items()}
                body, body_b64 = self._read_body(req)
                self.captured.append(
                    {
                        "ts": datetime.now().isoformat(timespec="milliseconds"),
                        "url": req.url,
                        "method": req.method,
                        "headers": headers,
                        "body": body,
                        "body_b64": body_b64,
                    }
                )
                print(f"🚧 GATE: captured + aborted {req.method} {req.url}")
            except Exception as e:
                self.captured.append({"error": str(e), "ts": datetime.now().isoformat()})
            finally:
                try:
                    route.abort("blocked by capture gate")
                except Exception:
                    pass
        else:
            try:
                route.continue_()
            except Exception:
                pass

    def arm(self):
        self.armed = True

    def disarm(self):
        self.armed = False


# ── Report generation ────────────────────────────────────
def _mask_url_query(url: str) -> str:
    return re.sub(r"([?&])(token|access|code)[^=&]*=[^&]*", r"\1σ=<masked>", url, flags=re.I)


def _endpoint_template(url: str) -> str:
    """Normalize a URL to a path template (no query, masked ids)."""
    m = re.search(r"https?://[^/]+(/.*)", url)
    path = m.group(1) if m else url
    path = path.split("?")[0]
    path = re.sub(r"/[0-9a-f-]{8,}(/|$)", r"/{id}\1", path)
    return path


def generate_report(records: list[dict], gate: list[dict], out_path: str):
    reqs = [r for r in records if r.get("type") == "request"]
    resps = [r for r in records if r.get("type") == "response"]

    lines = ["# EasyTrader API Capture Report", ""]
    lines.append(f"_Generated: {datetime.now().isoformat()} — auto tool, zero-submit run._")
    lines.append("")

    # 1. Authorization header shape (Playwright lowercases header names)
    lines.append("## Auth header")
    distinct_auth = sorted(
        {r["headers"]["authorization"] for r in reqs if "authorization" in r["headers"]}
    )
    if distinct_auth:
        lines.append(f"- Distinct values captured: {len(distinct_auth)}")
        for d in distinct_auth[:8]:
            lines.append(f"  - `{d}`")
    else:
        lines.append("- ⚠️ No Authorization header captured")
    lines.append("")

    # 2. POST/PUT inventory (this is where the order endpoint must appear)
    lines.append("## POST/PUT requests (all hosts, capture-filtered)")
    posts = [r for r in reqs if r["method"] in ("POST", "PUT", "PATCH")]
    for r in posts:
        lines.append(f"- `{r['ts']}` {r['method']} `{_mask_url_query(r['url'])}`")
        if r.get("body"):
            lines.append(f"  - body: `{r['body'][:300]}`")
    if not posts:
        lines.append("- none")
    lines.append("")

    # 3. Gate captures — the actual submit request(s)
    lines.append("## SafeSubmitGate captures (armed at submit click, all aborted)")
    real = [c for c in gate if c.get("method")]
    noise = [c for c in gate if not c.get("method")]
    if real:
        for c in real:
            lines.append(f"### {c['method']} {c['url']}")
            lines.append("```http")
            for h in sorted(c.get("headers", {})):
                lines.append(f"{h}: {c['headers'][h]}")
            lines.append("```")
            lines.append("```json")
            lines.append(c.get("body") or (f"[binary body b64> {c.get('body_b64')}" if c.get("body_b64") else "(empty body)") or "(empty body)")
            lines.append("```")
    else:
        lines.append("⚠️ NOTHING was captured — the submit click produced no mutating request.")
    if noise:
        lines.append(f"_({len(noise)} non-parseable/noise record(s))_")
    lines.append("")

    # 4. Endpoint templates
    templates: dict[str, list[str]] = {}
    for r in reqs:
        templates.setdefault(_endpoint_template(r["url"]), []).append(r["method"])
    lines.append("## Endpoint templates seen (GET+POST, non-asset)")
    lines.append("")
    lines.append("| Path template | Methods | Calls |")
    lines.append("|---|---|---|")
    for t in sorted(templates):
        methods = sorted(set(templates[t]))
        lines.append(f"| `{t}` | {','.join(methods)} | {len(templates[t])} |")
    lines.append("")

    # 5. Claim-form-ish search candidates
    lines.append("## Symbol-search candidates (URL or POST body mentioning search/symbol)")
    for r in reqs:
        u = r["url"]
        b = r.get("body") or ""
        if re.search(r"search|suggestion|symbol|lookup|query", u, re.I) or (
            r["method"] == "POST" and re.search(r"سفوسearch|symbol|search", b, re.I)
        ):
            lines.append(f"- {r['method']} `{_mask_url_query(u)}`")
            if b:
                lines.append(f"  - body: `{b[:200]}`")
    lines.append("")

    # 6. JSON response samples
    lines.append("## JSON response samples (≤4k bodies)")
    seen = set()
    shown = 0
    for r in resps:
        key = _endpoint_template(r["url"])
        if key in seen or not r.get("body") or r.get("body_error"):
            continue
        seen.add(key)
        if shown >= 30:
            lines.append("_…truncated: too many samples_")
            break
        shown += 1
        lines.append(f"### {r.get('status', '?')} `{_mask_url_query(r['url'])}`")
        lines.append("```json")
        lines.append((r.get("body") or "")[:1500])
        lines.append("```")
        lines.append("")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ── Orchestration ────────────────────────────────────────
def _open_buy_ticket(page: Page, symbol: str, logger) -> str:
    from order import _select_symbol, _dismiss_layout_overlay

    _select_symbol(page, symbol, DEFAULT_TIMEOUT)
    page.wait_for_timeout(800)
    _dismiss_layout_overlay(page)
    page.locator(locators.ORDER_BUY_BUTTON).first.click(timeout=DEFAULT_TIMEOUT * 1000)
    header = page.locator(locators.ORDER_FORM_HEADER_SYMBOL).first
    header.wait_for(state="visible", timeout=DEFAULT_TIMEOUT * 1000)
    opened_for = header.inner_text(timeout=2000).strip()
    logger.info("Ticket open for %r", opened_for)
    return opened_for


def _visible_notification(page: Page) -> str | None:
    try:
        return page.locator("app-notify div").first.inner_text(timeout=500).strip()
    except Exception as e:
        return f"(lookup failed: {e.__class__.__name__})"


def main(argv=None):
    import logging

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logger = logging.getLogger("capture")

    parser = argparse.ArgumentParser(description="EasyTrader endpoint capture (safe, zero-submit)")
    parser.add_argument("--symbol", "-s", default="فولاد")
    parser.add_argument("--quantity", "-q", default="1")
    parser.add_argument("--with-draft", action="store_true",
                        help="also click the draft button under the gate (real draft must NOT happen "
                             "— the gate aborts it too; only include this flag if you accept it)")
    args = parser.parse_args(argv)

    if not USERNAME or not PASSWORD:
        logger.error("Set MOFID_USER/MOFID_PASS in .env first.")
        return 1

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    ndjson_path = f"artifacts/network-{ts}.ndjson"
    gate_path = f"artifacts/gate-{ts}.ndjson"
    report_path = "artifacts/api-report.md"

    from broker import login, dismiss_announcement
    from order import _fill_verified

    logger.info("=" * 60)
    logger.info("Endpoint capture starting — symbol=%s — submit is GATED/ABORTED", args.symbol)
    logger.info("=" * 60)

    try:
        page = initialize_driver()
        recorder = TrafficRecorder(page)
        recorder.attach()

        if not login(page):
            logger.error("Login failed.")
            return 1
        dismiss_announcement(page)

        # ── UI drive: symbol → ticket ──
        opened_for = _open_buy_ticket(page, args.symbol, logger)
        if args.symbol not in opened_for:
            logger.warning("Ticket header mismatch — aborting for safety.")
            return 1

        # Fill quantity + a real ceiling price so the submit payload is valid
        from order import _read_number
        try:
            qty_input = page.locator(locators.QUANTITY_INPUT).first
            qty_input.wait_for(state="visible", timeout=DEFAULT_TIMEOUT * 1000)
            page.wait_for_timeout(700)
            _fill_verified(page, qty_input, int(args.quantity))
            price_input = page.locator(locators.PRICE_INPUT).first
            page.wait_for_timeout(300)
            chip = page.locator(locators.MAX_PRICE_CHIP).first
            chip.click(timeout=DEFAULT_TIMEOUT * 1000)
            page.wait_for_timeout(500)
            ceiling = _read_number(price_input.input_value(timeout=2000))
            if ceiling <= 0:
                raise RuntimeError("Ceiling chip did not populate the price field")
            logger.info("Fields filled (qty=%s, price=ceiling %s).", args.quantity, ceiling)
        except Exception as e:
            logger.warning("Could not fill fields (%s) — submit may be blocked by validation.", e)

        # ── Arm the gate right before the click ──
        gate = SafeSubmitGate(page)
        page.route("**/*", gate._handler)
        gate.disarm()

        submit = page.locator(locators.ORDER_FORM_BUY).first
        try:
            submit.wait_for(state="visible", timeout=5000)
        except PlaywrightTimeoutError as e:
            logger.error("Submit button not visible: %s", e)

        gate.arm()
        try:
            submit.click(timeout=3000)
            logger.info("Submit clicked (under gate).")
        except PlaywrightTimeoutError as e:
            logger.warning("Submit click failed: %s", e)
        page.wait_for_timeout(4000)
        logger.info("Visible notification after submit: %r", _visible_notification(page))
        page.screenshot(path=f"artifacts/after-submit-{ts}.png")
        page.wait_for_timeout(1500)

        # ── Dump gate + ndjson + report ──
        with open(gate_path, "w", encoding="utf-8") as f:
            for c in gate.captured:
                f.write(json.dumps(c, ensure_ascii=False) + "\n")
        logger.info("Gate captured %d mutating request(s) → %s", len(gate.captured), gate_path)

        recorder.dump(ndjson_path)
        logger.info("Captured %d network records → %s", len(recorder.records), ndjson_path)

        gate.disarm()
        generate_report(recorder.records, gate.captured, report_path)
        logger.info("Report written → %s", report_path)
        return 0

    finally:
        cleanup_all()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
