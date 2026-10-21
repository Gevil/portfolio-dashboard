"""Playwright integration tests against the running dashboard (localhost:8601).

Run host-side with the playwright venv (service must be up):
  python -m pytest tests/integration -v      (a Python env with `playwright` installed)
Point at another instance (e.g. a mock) with DASHBOARD_BASE=http://127.0.0.1:PORT.
"""

import base64
import datetime
import json
import os
import pathlib
import re
import time
import urllib.error
import urllib.request

import pytest
from playwright.sync_api import sync_playwright


def _creds():
    """Basic-auth creds: env first, then the bind-mounted env.secrets file.
    The service stays fail-open without them (dev mode)."""
    user = os.getenv("DASHBOARD_USER")
    pw = os.getenv("DASHBOARD_PASS")
    if not (user and pw):
        secrets = pathlib.Path(__file__).resolve().parents[2] / "env.secrets"
        try:
            for line in secrets.read_text().splitlines():
                if line.startswith("DASHBOARD_USER="):
                    user = user or line.split("=", 1)[1].strip()
                elif line.startswith("DASHBOARD_PASS="):
                    pw = pw or line.split("=", 1)[1].strip()
        except OSError:
            pass
    return user, pw


USER, PASS = _creds()
AUTH = (base64.b64encode(f"{USER}:{PASS}".encode()).decode()
        if USER and PASS else None)

BASE = os.getenv("DASHBOARD_BASE", "http://localhost:8601")
RANGES = ["1D", "1W", "1M", "3M", "1Y"]
MOBILE = (390, 844)
LAPTOP = (1366, 768)
DESKTOP = (1600, 1000)

def api(path):
    req = urllib.request.Request(BASE + path)
    if AUTH:
        req.add_header("Authorization", "Basic " + AUTH)
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status, json.load(r)


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        yield b
        b.close()


def new_page(browser, size, scheme="dark"):
    w, h = size
    creds = ({"origin": BASE, "username": USER, "password": PASS}
             if USER and PASS else None)
    ctx = browser.new_context(viewport={"width": w, "height": h},
                              device_scale_factor=1,
                              color_scheme=scheme,
                              http_credentials=creds)
    return ctx.new_page()



# ---------------------------------------------------------------- API level

def test_health():
    with urllib.request.urlopen(BASE + "/health", timeout=10) as r:
        assert r.status == 200


@pytest.mark.parametrize("rng", RANGES)
def test_api_history_shape(rng):
    status, body = api(f"/api/history/ASML?range={rng}")
    assert status == 200
    pts = body["data"]
    assert len(pts) >= 5, f"{rng}: expected a populated series"
    ts = [p["t"] for p in pts]
    assert ts == sorted(ts), f"{rng}: timestamps not ascending"
    assert len(set(ts)) == len(ts), f"{rng}: duplicate timestamps"
    if rng in ("1M", "3M", "1Y"):
        days = {
            datetime.datetime.fromtimestamp(t, tz=datetime.timezone.utc).date()
            for t in ts
        }
        assert len(days) == len(ts), f"{rng}: more than one bar per UTC day"
    assert time.time() - ts[-1] < 5 * 86400, f"{rng}: last bar stale"


def test_api_config_is_eur_native():
    status, cfg = api("/api/config")
    assert status == 200
    assert "displayCurrency" not in cfg
    for pos in cfg["portfolio"].values():
        assert {"shares", "investedAmount"} <= set(pos) <= {"shares", "investedAmount", "lots"}


def test_api_portfolio_shape():
    status, p = api("/api/portfolio")
    assert status == 200
    assert p["currency"] == "EUR"
    totals = p["totals"]
    assert totals["valueEur"] > 0
    assert set(totals["costMissing"]) <= {r["id"] for r in p["positions"]}
    for r in p["positions"]:
        for key in ("id", "shares", "priceEur", "valueEur", "weightPct",
                    "priceAsOf", "priceSource", "stale"):
            assert key in r, f"{r['id']}: missing {key}"
        if r["id"] in totals["costMissing"]:
            assert r["pnlEur"] is None and r["investedEur"] is None
    weights = [r["weightPct"] for r in p["positions"]
               if r["weightPct"] is not None]
    assert abs(sum(weights) - 100) < 0.1
    assert p["benchmark"]["id"] == "GSPC"


@pytest.mark.parametrize("rng", ["1M", "3M", "6M", "1Y"])
def test_api_portfolio_history_shape(rng):
    status, h = api(f"/api/portfolio/history?range={rng}")
    assert status == 200 and h["range"] == rng
    assert h["benchmark"]["currency"] == "EUR"
    ts = [p["t"] for p in h["points"]]
    assert ts == sorted(ts) and len(ts) >= 5
    for series in h["indexed"].values():
        assert series and series[0]["pct"] == 0.0


def test_api_portfolio_history_rejects_bad_range():
    try:
        api("/api/portfolio/history?range=5Y")
    except urllib.error.HTTPError as e:
        assert e.code == 400
    else:
        raise AssertionError("expected HTTP 400")


# ---------------------------------------------------------------- page level
#
# The UI is a set of ES modules (static/js) rendered into #view-overview / secondary views; the
# old scripts.js globals (chartData, #price-chart, .duet, .left-panel) no longer exist.

def wait_js(page, fn_src, timeout=10):
    """Poll a JS arrow function until truthy. Not page.wait_for_function: with the production CSP
    (no 'unsafe-eval') its string predicates are blocked once they have to poll."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if page.evaluate(fn_src):
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"timed out waiting for: {fn_src}")


CHART_INK_JS = """() => {
    const c = document.querySelector('.dt-chart-host canvas.ch-canvas-price');
    if (!c || !c.width) return -1;
    const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
    let n = 0;
    for (let i = 0; i < d.length; i += 4) {
        const mx = Math.max(d[i], d[i + 1], d[i + 2]), mn = Math.min(d[i], d[i + 1], d[i + 2]);
        if (d[i + 3] > 0 && mx - mn > 45) n++;   // saturated pixels = the price line / fill, not grey text
    }
    return n;
}"""


class Collector:
    """Page errors and console errors of one page. Every failed request logs a console error, so a
    404/500 from any endpoint the UI calls fails the test that uses this."""

    def __init__(self, page):
        self.errors = []
        page.on("pageerror", lambda e: self.errors.append("pageerror: " + str(e)))
        page.on("console", lambda m: self.errors.append("console: " + m.text)
                if m.type == "error" else None)


def open_overview(browser, size, scheme="dark"):
    page = new_page(browser, size, scheme)
    page.collector = Collector(page)
    page.goto(BASE, wait_until="networkidle")
    page.wait_for_selector(".pos-row", timeout=30000)
    return page


def select_first_holding(page):
    page.locator(".pos-row .pos-btn").first.click()
    page.wait_for_selector(".dt-pane .dt-chart-host canvas.ch-canvas-price", timeout=20000)
    wait_js(page, "() => document.querySelector('.dt-chart-host .state-loading') === null", timeout=30)


def test_no_page_or_console_errors(browser):
    page = open_overview(browser, DESKTOP)
    select_first_holding(page)
    assert not page.collector.errors, page.collector.errors
    page.close()


def test_index_html_is_csp_and_xss_hardened():
    html = (pathlib.Path(__file__).resolve().parents[2] / "static" / "index.html").read_text()
    assert not re.search(r"\sstyle\s*=", html), "inline style attribute (blocked by a strict CSP)"
    assert not re.search(r"<style[\s>]", html)
    assert not re.search(r"\son[a-z]+\s*=", html), "inline event handler"
    scripts = re.findall(r"<script\b[^>]*>", html)
    assert all("src=" in s for s in scripts), "inline <script> (blocked by CSP)"
    srcs = [re.search(r'src="([^"?]+)', s).group(1) for s in scripts]
    assert srcs.index("/static/dompurify.min.js") < srcs.index("/static/marked.min.js") \
        < srcs.index("/static/js/main.js"), f"load order: {srcs}"


def test_old_monolith_assets_are_gone():
    for path in ("/static/scripts.js", "/static/styles.css", "/static/market.css"):
        try:
            api(path)
        except urllib.error.HTTPError as e:
            assert e.code == 404, (path, e.code)
        except json.JSONDecodeError:
            raise AssertionError(f"{path} still served")
        else:
            raise AssertionError(f"{path} still served")


def test_markdown_sanitised_and_fails_closed(browser):
    probe = """async () => {
        const m = await import('/static/js/markdown.js');
        const html = m.renderMarkdown('# T\\n\\n<img src=x onerror="window.__x=1"> [bad](javascript:alert(1)) **b**');
        const d = document.createElement('div'); d.innerHTML = html;
        return { html, bad: d.querySelectorAll('img,script,style').length,
                 on: [...d.querySelectorAll('*')].some(n => [...n.attributes].some(a => /^on/i.test(a.name))),
                 js: [...d.querySelectorAll('a')].some(a => /^javascript:/i.test(a.getAttribute('href') || '')),
                 x: window.__x || 0, bold: !!d.querySelector('strong') };
    }"""
    page = new_page(browser, LAPTOP)
    page.goto(BASE, wait_until="networkidle")
    assert page.evaluate("() => typeof DOMPurify === 'function' && typeof DOMPurify.sanitize === 'function'")
    r = page.evaluate(probe)
    assert r["bad"] == 0 and not r["on"] and not r["js"] and r["x"] == 0 and r["bold"], r
    page.close()

    # DOMPurify blocked: the text must come out escaped, never as raw marked output.
    page = new_page(browser, LAPTOP)
    page.route("**/dompurify*", lambda route: route.abort())
    page.goto(BASE, wait_until="networkidle")
    r = page.evaluate(probe)
    assert r["bad"] == 0 and not r["on"] and r["x"] == 0 and not r["bold"], r
    assert "&lt;img" in r["html"], r["html"]
    page.close()


def test_hero_matches_api_and_uses_24h_clock(browser):
    _, p = api("/api/portfolio")
    page = open_overview(browser, DESKTOP)
    page.wait_for_selector(".stat-hero")
    shown = float(re.sub(r"[^0-9.]", "", page.inner_text(".stat-hero")))
    api_val = p["totals"]["valueEur"]
    assert abs(shown - api_val) <= max(1.0, api_val * 0.01), (shown, api_val)  # live ticks may move it
    assert len(page.locator(".pos-row").all()) == len(p["positions"])
    body = page.inner_text("body")
    assert not re.search(r"\b\d{1,2}:\d{2}\s?(AM|PM)\b", body, re.I), "12-hour clock on screen"
    assert not re.search(r"\b(null|undefined|NaN)\b", body), "placeholder junk rendered"
    page.close()


@pytest.mark.parametrize("rng", RANGES)
def test_detail_range_click_redraws_chart(browser, rng):
    page = open_overview(browser, DESKTOP)
    select_first_holding(page)
    page.click(f'.dt-chart-tools button[data-range="{rng}"]')
    wait_js(page, f"() => document.querySelector('.dt-chart-tools button[data-range=\"{rng}\"]')"
                  ".getAttribute('aria-pressed') === 'true'")
    wait_js(page, "() => document.querySelector('.dt-chart-host .state-loading') === null", timeout=30)
    page.wait_for_timeout(500)
    ink = page.evaluate(CHART_INK_JS)
    assert ink > 300, f"{rng}: price line not drawn (ink pixels={ink})"
    page.close()


def test_chart_stays_inside_its_card(browser):
    """Regression: the chart host had a fixed height shorter than canvas + legend."""
    page = open_overview(browser, DESKTOP)
    select_first_holding(page)
    page.wait_for_timeout(500)
    overflow = page.evaluate("""() => {
        const host = document.querySelector('.dt-chart-host');
        const card = host.closest('.dt-card, section, article') || host.parentElement;
        const bottom = Math.max(...[...host.children].map(c => c.getBoundingClientRect().bottom));
        return bottom - card.getBoundingClientRect().bottom;
    }""")
    assert overflow <= 1, f"chart content overflows its card by {overflow}px"
    page.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_theme_follows_system_and_can_be_overridden(browser, scheme):
    page = open_overview(browser, LAPTOP, scheme)
    bg = lambda: page.evaluate("getComputedStyle(document.body).backgroundColor")  # noqa: E731
    first = bg()
    page.click("#btn-settings")
    page.click('role=tab[name="Appearance"]')
    other = "Dark" if scheme == "light" else "Light"
    page.get_by_label(other, exact=True).check()
    wait_js(page, "() => document.documentElement.getAttribute('data-theme') !== null")
    assert bg() != first, "theme override did not change the page background"
    assert page.evaluate("localStorage.getItem('pd.theme')") == other.lower()
    page.reload(wait_until="networkidle")
    assert page.evaluate("document.documentElement.getAttribute('data-theme')") == other.lower(), \
        "saved theme must apply before first paint"
    page.close()


def test_secondary_views_mount_without_errors(browser):
    page = open_overview(browser, LAPTOP)
    for view in ("digest", "market", "aiops", "overview"):
        page.click(f'.tab[data-view="{view}"]')
        wait_js(page, f"() => !document.getElementById('view-{view}').hidden")
        page.wait_for_timeout(1500)
        if view != "overview":
            assert page.evaluate("location.hash") == f"#{view}", view
        assert page.locator(f"#view-{view}").inner_text().strip(), f"{view} rendered nothing"
    assert not page.collector.errors, page.collector.errors
    page.close()


def test_settings_alerts_chat_open_close_with_escape_and_focus_return(browser):
    page = open_overview(browser, LAPTOP)
    for btn in ("#btn-settings", "#btn-alerts", "#btn-chat"):
        page.click(btn)
        page.wait_for_selector("[role=dialog]:visible", timeout=10000)
        page.keyboard.press("Escape")
        wait_js(page, "() => [...document.querySelectorAll('[role=dialog]')]"
                      ".every(d => d.hidden || d.getClientRects().length === 0)")
        assert page.evaluate("document.activeElement && document.activeElement.id") == btn[1:], \
            f"focus not returned to {btn}"
    page.close()


def test_mobile_no_overflow_and_hero_in_first_viewport(browser):
    page = open_overview(browser, MOBILE)
    sw = page.evaluate("document.documentElement.scrollWidth")
    assert sw <= MOBILE[0], f"horizontal overflow: scrollWidth={sw}"
    hero = page.locator(".stat-hero").bounding_box()
    assert hero is not None and hero["y"] < MOBILE[1], "portfolio value not in first mobile viewport"
    tabs = page.locator("#tabs").bounding_box()
    assert tabs is not None and tabs["y"] + tabs["height"] <= MOBILE[1] + 1, "tab bar not pinned to the bottom"
    for sel in ("#statusbar .sb-actions > *", "#statusbar .sb-chips > *"):
        els = page.locator(sel)
        for i in range(els.count()):
            b = els.nth(i).bounding_box()
            if b is not None:
                assert b["x"] >= -1 and b["x"] + b["width"] <= MOBILE[0] + 1, f"{sel} clipped"
    page.locator(".pos-row .pos-btn").first.click()   # drawer on phones
    page.wait_for_selector("[role=dialog] .dt-pane", timeout=15000)
    assert page.evaluate("document.documentElement.scrollWidth") <= MOBILE[0], "drawer causes overflow"
    page.close()


# ---------------------------------------------------------------- wave 2: routes, status chip, panels
#
# DOM contract (static/js): #since-strip (#since-toggle aria-expanded, #since-dismiss, #since-body),
# #attn-card > #attn-list > .attn-card (.attn-sev chip, .attn-open button, #attn-more), #perf-card
# (#perf-range, #perf-bench-toggle, #perf-kpis, #perf-basis-warning + #perf-basis-fix), #risk-card
# (#risk-toggle aria-expanded, #risk-body, .rk-correlation), #policy-card (.pl-drift | #policy-set-targets),
# status bar #sb-data (quote-age chip) + #sb-sse (stream dot), detail cards .dt-advice / .dt-attr / .dt-lots.
# Routes: #overview #digest #market #aiops #detail/<ID> #alerts #alerts/<id>; unknown -> #overview.

def first_symbol(page):
    return page.locator(".pos-row .pos-btn").first.get_attribute("data-symbol")


def test_detail_route_opens_pane_and_close_restores_overview(browser):
    page = open_overview(browser, DESKTOP)
    sym = first_symbol(page)
    page.goto(f"{BASE}/#detail/{sym}", wait_until="networkidle")
    page.wait_for_selector(".ov-dock:not([hidden]) .dt-pane .dt-title", timeout=20000)
    assert page.locator(f'.pos-row.is-selected[data-symbol="{sym}"]').count() == 1
    page.click(".dt-close")
    wait_js(page, "() => location.hash === '#overview'")
    wait_js(page, "() => document.querySelector('.ov-dock').hidden")
    page.close()


def test_selecting_a_holding_writes_hash_and_back_closes_it(browser):
    page = open_overview(browser, DESKTOP)
    sym = first_symbol(page)
    page.locator(".pos-row .pos-btn").first.click()
    wait_js(page, f"() => location.hash === '#detail/{sym}'")
    page.wait_for_selector(".ov-dock:not([hidden])", timeout=10000)
    page.go_back()
    wait_js(page, "() => !location.hash.startsWith('#detail/')")
    wait_js(page, "() => document.querySelector('.ov-dock').hidden")
    page.close()


def test_unknown_hash_falls_back_to_overview(browser):
    page = open_overview(browser, LAPTOP)
    page.goto(f"{BASE}/#no-such-route", wait_until="networkidle")
    page.wait_for_selector(".pos-row", timeout=30000)
    wait_js(page, "() => location.hash === '#overview'")
    assert page.evaluate("!document.getElementById('view-overview').hidden")
    page.close()


def test_alerts_route_opens_drawer_and_closing_restores_overview(browser):
    page = open_overview(browser, LAPTOP)
    page.goto(f"{BASE}/#alerts", wait_until="networkidle")
    page.wait_for_selector("[role=dialog]:visible", timeout=15000)
    page.keyboard.press("Escape")
    wait_js(page, "() => location.hash === '#overview'")
    assert not page.collector.errors, page.collector.errors
    page.close()


def test_quote_age_chip_states_and_separate_stream_dot(browser):
    page = open_overview(browser, LAPTOP)
    wait_js(page, "() => !/Data|Loading/.test(document.querySelector('#sb-data .sb-data-text').textContent)", timeout=20)
    text = page.inner_text("#sb-data .sb-data-text")
    assert re.fullmatch(r"Live · \d+s|Delayed ~15 min \(Yahoo EU\)|Stale · \d+ min|Closed · last .+|"
                        r"Refresh failing · .+|No quotes yet", text), text
    assert "\n" in (page.get_attribute("#sb-data", "title") or ""), "tooltip should list each holding"
    assert (page.get_attribute("#sb-sse", "aria-label") or "").startswith("Live stream"), "stream dot missing"
    page.close()


def test_since_strip_collapses_and_dismisses(browser):
    page = open_overview(browser, LAPTOP)
    page.wait_for_selector("#since-strip", timeout=20000)
    assert page.get_attribute("#since-toggle", "aria-expanded") == "true"
    page.click("#since-toggle")
    wait_js(page, "() => document.getElementById('since-body').hidden")
    assert page.get_attribute("#since-toggle", "aria-expanded") == "false"
    page.click("#since-toggle")
    wait_js(page, "() => !document.getElementById('since-body').hidden")
    page.click("#since-dismiss")
    wait_js(page, "() => !document.getElementById('since-strip')")
    page.close()


def test_attention_cards_are_structured_and_capped_at_three(browser):
    page = open_overview(browser, LAPTOP)
    page.wait_for_selector("#attn-card", timeout=20000)
    cards = page.locator("#attn-list .attn-card")
    n = cards.count()
    if n == 0:
        assert "Nothing needs attention" in page.inner_text("#attn-card") or "Waiting" in page.inner_text("#attn-card")
    else:
        assert n <= 3 or page.locator("#attn-more").count() == 1
        for i in range(n):
            c = cards.nth(i)
            assert c.locator(".attn-sev").count() == 1 and c.locator(".attn-open").count() == 1
        if page.locator("#attn-more").count():
            assert n == 3 and page.get_attribute("#attn-more", "aria-expanded") == "false"
            page.click("#attn-more")
            wait_js(page, "() => document.getElementById('attn-more').getAttribute('aria-expanded') === 'true'")
            assert page.locator("#attn-list .attn-card").count() > 3
    page.close()


def test_performance_card_ranges_twr_xirr_and_basis_warning(browser):
    _, perf = api("/api/portfolio/performance?range=ALL")
    page = open_overview(browser, DESKTOP)
    page.wait_for_selector("#perf-card", timeout=20000)
    labels = page.locator("#perf-range button").all_inner_texts()
    assert labels == ["1M", "3M", "6M", "1Y", "ALL"], labels
    page.click('#perf-range button[data-range="ALL"]')
    page.wait_for_selector("#perf-kpis", timeout=30000)
    wait_js(page, "() => document.querySelector('#perf-range button[data-range=\"ALL\"]').getAttribute('aria-pressed') === 'true'")
    kpis = page.inner_text("#perf-kpis")
    assert "TWR" in kpis
    assert ("XIRR" in kpis) == (perf.get("xirrPct") is not None), kpis
    warn = page.locator("#perf-basis-warning")
    if perf.get("basis") == "current_shares":
        assert warn.count() == 1
        assert "History applies today's share counts to the whole window" in warn.inner_text()
        page.click("#perf-basis-fix")
        page.wait_for_selector("[role=dialog]:visible", timeout=10000)
    else:
        assert warn.count() == 0
    page.close()


def test_risk_panel_collapses_and_policy_card_renders(browser):
    _, policy = api("/api/policy")
    page = open_overview(browser, DESKTOP)
    page.wait_for_selector("#risk-card .rk-correlation, #risk-card .state", timeout=30000)
    assert page.get_attribute("#risk-toggle", "aria-expanded") == "true"
    page.click("#risk-toggle")
    wait_js(page, "() => document.getElementById('risk-body').hidden")
    assert page.get_attribute("#risk-toggle", "aria-expanded") == "false"
    page.wait_for_selector("#policy-card .pl-drift, #policy-card #policy-set-targets", timeout=30000)
    assert (page.locator("#policy-set-targets").count() == 1) == (not policy.get("configured")), policy.get("configured")
    if not policy.get("configured"):
        page.click("#policy-set-targets")
        page.wait_for_selector("[role=dialog]:visible", timeout=10000)
    page.close()


def test_detail_attribution_lots_and_advice_blocks(browser):
    page = open_overview(browser, DESKTOP)
    sym = first_symbol(page)
    _, cfg = api("/api/config")
    _, advice = api("/api/advice")
    page.locator(".pos-row .pos-btn").first.click()
    page.wait_for_selector(".dt-pane .dt-attr:not([hidden]) .dt-body", timeout=20000)
    wait_js(page, "() => !document.querySelector('.dt-attr .state-loading')", timeout=30)
    attr_text = page.inner_text(".dt-attr")
    _, attr = api("/api/portfolio/attribution?range=3M")
    row = next(p for p in attr["positions"] if p["id"] == sym)
    if row["exposureCurrency"] == "EUR":
        assert "EUR asset, no FX effect" in attr_text
    else:
        assert "+ FX" in attr_text
    has_lots = bool((cfg.get("portfolio", {}).get(sym) or {}).get("lots"))
    assert (page.locator(".dt-lots:not([hidden])").count() == 1) == has_lots
    if sym in advice:
        pub = (advice[sym].get("published") or {}).get("rating") or advice[sym].get("rating")
        assert pub in page.inner_text(".dt-advice")
    else:
        assert page.locator(".dt-advice:not([hidden])").count() == 0
    page.close()


def test_mobile_no_overflow_with_strip_cards_and_panels(browser):
    page = open_overview(browser, MOBILE)
    page.wait_for_selector("#risk-card", timeout=30000)
    page.wait_for_selector("#policy-card", timeout=30000)
    page.wait_for_timeout(1500)
    assert page.evaluate("document.documentElement.scrollWidth") <= MOBILE[0], "horizontal overflow with new panels"
    page.close()


def test_alerts_id_route_focuses_the_row(browser):
    _, alerts = api("/api/alerts?limit=200")
    if not alerts["items"]:
        pytest.skip("no alerts stored")
    aid = alerts["items"][0]["id"]
    page = open_overview(browser, LAPTOP)
    page.goto(f"{BASE}/#alerts/{aid}", wait_until="networkidle")
    page.wait_for_selector(f'li.ac-item[data-id="{aid}"].is-focus', timeout=15000)
    page.keyboard.press("Escape")
    wait_js(page, "() => location.hash === '#overview'")
    page.close()


# ---------------------------------------------------------------- wave 3: account context (Real/Practice)
#
# DOM contract (static/js, plan §4.3 / contract §6): #book-switch > .book-btn[data-book] (aria-pressed),
# #book-banner[role=status] > #book-banner-text (not dismissible, hidden while book == 'real'),
# #agent-card (practice-only; renders an explanatory state when /api/paper/status says disabled).
# Persistence: localStorage 'pd.book'; '#book=<id>' in the hash overrides it for the current load only.

def _book_pressed(page, book):
    return page.get_attribute(f'#book-switch .book-btn[data-book="{book}"]', "aria-pressed") == "true"


def test_book_switch_present(browser):
    page = open_overview(browser, LAPTOP)
    labels = page.locator("#book-switch .book-btn").all_inner_texts()
    assert labels == ["Real", "Practice"], labels
    assert _book_pressed(page, "real") and not _book_pressed(page, "practice")
    assert page.get_attribute("#book-banner", "hidden") is not None, "banner must start hidden in Real"
    page.close()


def test_selecting_practice_sets_state_banner_and_requests_bucket(browser):
    page = open_overview(browser, LAPTOP)
    with page.expect_request(lambda r: "/api/portfolio" in r.url and "book=practice" in r.url):
        page.click('#book-switch .book-btn[data-book="practice"]')
    wait_js(page, "() => document.documentElement.getAttribute('data-book') === 'practice'")
    assert _book_pressed(page, "practice") and not _book_pressed(page, "real")
    page.wait_for_selector("#book-banner:not([hidden])", timeout=10000)
    assert page.get_attribute("#book-banner", "role") == "status"
    assert page.inner_text("#book-banner-text").strip(), "banner must show explanatory text"
    assert page.evaluate("localStorage.getItem('pd.book')") == "practice"
    assert "[Practice]" in page.title()
    # Tolerate paper_disabled (503): assert the explanatory state rather than requiring live bucket data.
    status, paper = api("/api/paper/status")
    assert status == 200
    if paper.get("enabled") is False:
        page.wait_for_selector("#agent-card .state-empty, #agent-card .state-error", timeout=15000)
    else:
        page.wait_for_selector("#agent-card:not([hidden]) .agent-row", timeout=15000)
    page.close()


def test_practice_agent_card_has_mode_control_and_orders(browser):
    page = open_overview(browser, LAPTOP)
    page.click('#book-switch .book-btn[data-book="practice"]')
    wait_js(page, "() => document.documentElement.getAttribute('data-book') === 'practice'")
    status, paper = api("/api/paper/status")
    assert status == 200
    if paper.get("enabled") is False:
        # paper_disabled: the card explains itself and renders no controls.
        page.wait_for_selector("#agent-card .state-empty, #agent-card .state-error", timeout=15000)
        assert page.locator("#agent-mode").count() == 0
    else:
        page.wait_for_selector("#agent-card:not([hidden]) #agent-mode", timeout=15000)
        buttons = page.locator("#agent-mode .agent-mode-btn")
        assert buttons.all_inner_texts() == ["Off", "Shadow", "Demo"]
        pressed = page.locator('#agent-mode .agent-mode-btn[aria-pressed="true"]')
        assert pressed.count() == 1
        assert page.locator("#agent-halt, #agent-resume").count() == 1
        # Orders: either the table, the empty state, or an explicit error state (never silent).
        page.wait_for_selector(
            "#agent-orders #agent-orders-table, #agent-orders .state-empty, #agent-orders .state-error",
            timeout=15000)
    page.close()


def test_practice_agent_card_has_decisions_section(browser):
    page = open_overview(browser, LAPTOP)
    page.click('#book-switch .book-btn[data-book="practice"]')
    wait_js(page, "() => document.documentElement.getAttribute('data-book') === 'practice'")
    status, paper = api("/api/paper/status")
    assert status == 200
    if paper.get("enabled") is False:
        # paper_disabled: the card explains itself and renders no Decisions section.
        page.wait_for_selector("#agent-card .state-empty, #agent-card .state-error", timeout=15000)
        assert page.locator("#agent-decisions").count() == 0
    else:
        page.wait_for_selector("#agent-card:not([hidden]) #agent-decisions", timeout=15000)
        assert page.locator("#agent-decisions-title").inner_text() == "Decisions"
        assert page.locator("#agent-decide").count() == 1
        # Today block (with its empty text), or an explicit error state (never silent).
        page.wait_for_selector(
            "#agent-decisions #agent-decisions-today, #agent-decisions .state-error",
            timeout=15000)
    page.close()


def test_practice_perf_kpi_strip_and_decision_quality(browser):
    page = open_overview(browser, LAPTOP)
    page.click('#book-switch .book-btn[data-book="practice"]')
    wait_js(page, "() => document.documentElement.getAttribute('data-book') === 'practice'")
    status, paper = api("/api/paper/status")
    assert status == 200
    if paper.get("enabled") is False:
        # paper_disabled: no strip, no Decision quality section; the cards explain themselves.
        page.wait_for_selector("#agent-card .state-empty, #agent-card .state-error", timeout=15000)
        assert page.locator("#agent-grading").count() == 0
    else:
        page.wait_for_selector("#perf-practice-kpis", timeout=30000)
        # Metrics may be missing (no season / too few sessions): then the strip explains itself.
        page.wait_for_selector("#perf-practice-kpis #perf-verdict, #perf-practice-kpis #perf-practice-note", timeout=15000)
        if page.locator("#perf-verdict").count():
            text = page.text_content("#perf-practice-kpis") or ""   # CSS uppercases labels; compare the DOM text
            for label in ("Excess vs buy & hold", "Max drawdown", "Tracking error", "Information ratio", "Sharpe", "Sortino", "Beta vs buy & hold", "Turnover", "Cost drag", "Sessions"):
                assert label in text, text
            assert page.inner_text("#perf-verdict").strip(), "verdict chip needs text"
            assert page.locator("#perf-verdict [aria-hidden='true']").count() == 1, "verdict chip needs an icon"
        page.wait_for_selector("#agent-card:not([hidden]) #agent-grading", timeout=15000)
        assert page.locator("#agent-grading-title").inner_text() == "Decision quality"
        # Table, empty state, or an explicit error (never silent).
        page.wait_for_selector(
            "#agent-grading #agent-grading-table, #agent-grading .state-empty, #agent-grading .state-error",
            timeout=15000)
    page.close()


def _bucket_pressed(page, bucket):
    return page.get_attribute(f'#bucket-switch .bucket-btn[data-bucket="{bucket}"]', "aria-pressed") == "true"


def test_practice_bucket_switch_present_and_persisted(browser):
    page = open_overview(browser, LAPTOP)
    page.click('#book-switch .book-btn[data-book="practice"]')
    wait_js(page, "() => document.documentElement.getAttribute('data-book') === 'practice'")
    page.wait_for_selector("#agent-card:not([hidden]) #bucket-switch", timeout=15000)
    assert page.locator("#bucket-switch .bucket-btn").all_inner_texts() == ["Daily", "Intraday"]
    assert page.get_attribute("#bucket-switch", "role") == "group"
    assert _bucket_pressed(page, "daily") and not _bucket_pressed(page, "intraday")
    # every Practice fetch carries the bucket (portfolio is the one the shell always makes)
    with page.expect_request(lambda r: "/api/portfolio" in r.url and "book=practice" in r.url and "bucket=intraday" in r.url):
        page.click('#bucket-switch .bucket-btn[data-bucket="intraday"]')
    assert _bucket_pressed(page, "intraday") and not _bucket_pressed(page, "daily")
    assert page.evaluate("localStorage.getItem('pd.bucket')") == "intraday"
    # the banner names the bucket
    wait_js(page, "() => /intraday/i.test(document.getElementById('book-banner-text').textContent)")
    status, paper = api("/api/paper/status")
    assert status == 200
    intraday = (paper.get("buckets") or {}).get("intraday") or {}
    if paper.get("enabled") is not False and intraday.get("enabled") is False:
        # disabled bucket: the card says so, with its mode, and renders no controls
        page.wait_for_selector("#agent-bucket-disabled", timeout=15000)
        text = page.inner_text("#agent-bucket-disabled")
        assert "Intraday bucket not enabled" in text, text
        assert page.locator("#agent-mode").count() == 0
    page.reload(wait_until="networkidle")
    page.wait_for_selector("#agent-card:not([hidden]) #bucket-switch", timeout=15000)
    assert _bucket_pressed(page, "intraday")
    page.click('#bucket-switch .bucket-btn[data-bucket="daily"]')
    assert _bucket_pressed(page, "daily")
    assert page.evaluate("localStorage.getItem('pd.bucket')") == "daily"
    page.close()


def test_practice_compare_card_and_briefings(browser):
    page = open_overview(browser, LAPTOP)
    page.click('#book-switch .book-btn[data-book="practice"]')
    wait_js(page, "() => document.documentElement.getAttribute('data-book') === 'practice'")
    status, paper = api("/api/paper/status")
    assert status == 200
    page.wait_for_selector("#compare-card:not([hidden])", timeout=15000)
    assert (page.text_content("#compare-title") or "").strip() == "Compare"   # CSS uppercases titles
    # Table, or an explicit empty/error state (paper_disabled answers 503): never silent.
    page.wait_for_selector("#compare-card #compare-table, #compare-card .state-empty, #compare-card .state-error", timeout=30000)
    if page.locator("#compare-table").count():
        heads = page.locator("#compare-table thead th").all_inner_texts()
        assert [h.split(" (")[0].strip().lower() for h in heads][-1] == "buy & hold", heads
        text = page.text_content("#compare-table") or ""
        for label in ("Return", "Excess vs buy & hold", "Excess, net of costs", "95% interval", "Verdict", "Max drawdown",
                      "Sharpe", "Sortino", "Beta vs buy & hold", "Turnover", "Cost drag", "Decisions", "Sessions"):
            assert label in text, text
    if paper.get("enabled") is False:
        assert page.locator("#agent-briefs").count() == 0
    else:
        page.wait_for_selector("#agent-card:not([hidden]) #agent-briefs", timeout=15000)
        assert page.locator("#agent-briefs-title").inner_text() == "Briefings"
        # List (newest first, at most 10), the empty state, or an explicit error.
        page.wait_for_selector("#agent-briefs #agent-briefs-list, #agent-briefs .state-empty, #agent-briefs .state-error", timeout=15000)
        if page.locator("#agent-briefs-list").count():
            dates = page.locator("#agent-briefs-list .agent-brief-item").evaluate_all("els => els.map(e => e.dataset.date)")
            assert 1 <= len(dates) <= 10, dates
            assert dates == sorted(dates, reverse=True), dates
    # Real never shows the Compare card
    page.goto(f"{BASE}/#book=real", wait_until="networkidle")
    wait_js(page, "() => document.documentElement.getAttribute('data-book') !== 'practice'")
    wait_js(page, "() => !document.querySelector('#compare-card:not([hidden])')")   # repaint is scheduled one frame later
    page.close()


def test_practice_forecaster_card_and_candidates_table(browser):
    page = open_overview(browser, LAPTOP)
    page.click('#book-switch .book-btn[data-book="practice"]')
    wait_js(page, "() => document.documentElement.getAttribute('data-book') === 'practice'")
    # Both cards exist in Practice whatever the backend state (no cohort / forecaster or paper disabled).
    page.wait_for_selector("#forecaster-card:not([hidden])", timeout=15000)
    page.wait_for_selector("#candidates-card:not([hidden])", timeout=15000)
    assert (page.text_content("#forecaster-title") or "").strip() == "Forecaster"   # CSS uppercases titles
    assert (page.text_content("#candidates-title") or "").strip() == "Candidates"
    page.wait_for_selector("#forecaster-card #fc-status", timeout=15000)
    # Experiment: tables, or an explicit empty/error state (never silent).
    page.wait_for_selector("#fc-experiment #fc-arms-table, #fc-experiment .state-empty, #fc-experiment .state-error", timeout=60000)
    if page.locator("#fc-arms-table").count():
        assert page.locator("#fc-comparisons-table tbody tr").count() == 8
        assert page.locator("#fc-legend dt").all_inner_texts() == ["R", "Q", "A", "B", "D", "E"]
        arms = page.locator("#fc-arms-table tbody th[scope=row]").all_inner_texts()
        assert {"R", "Q", "A", "B", "D", "E"} <= set(arms), arms
    # Candidates: table (at most 30 rows, sorted by p 20d), the empty state, or an explicit error.
    page.wait_for_selector("#candidates-body #candidates-table, #candidates-body .state-empty, #candidates-body .state-error", timeout=30000)
    if page.locator("#candidates-table").count():
        heads = [h.strip().lower() for h in page.locator("#candidates-table thead th").all_inner_texts()]
        assert heads == ["name", "sector", "p 5d", "p 20d", "spread", "source", "held"], heads
        rows = page.locator("#candidates-table tbody tr")
        assert 1 <= rows.count() <= 30
        p20 = [float(t.rstrip("%")) for t in rows.evaluate_all("els => els.map(e => e.children[3].textContent)") if t.strip() not in ("", "\u2013")]
        assert p20 == sorted(p20, reverse=True), p20
        page.locator("#candidates-table .cand-btn").first.click()
        page.wait_for_selector("#candidate-packet-text, #candidate-packet .state-error", timeout=15000)
    # Real never shows them
    page.goto(f"{BASE}/#book=real", wait_until="networkidle")
    wait_js(page, "() => document.documentElement.getAttribute('data-book') !== 'practice'")
    wait_js(page, "() => !document.querySelector('#forecaster-card:not([hidden]), #candidates-card:not([hidden])')")   # repaint is one frame later
    page.close()


def test_practice_reload_keeps_practice(browser):
    page = open_overview(browser, LAPTOP)
    page.click('#book-switch .book-btn[data-book="practice"]')
    wait_js(page, "() => document.documentElement.getAttribute('data-book') === 'practice'")
    page.reload(wait_until="networkidle")
    wait_js(page, "() => document.documentElement.getAttribute('data-book') === 'practice'")
    assert _book_pressed(page, "practice")
    assert page.locator("#book-banner:not([hidden])").count() == 1
    page.close()


def test_book_hash_real_overrides_and_returns_to_real(browser):
    page = open_overview(browser, LAPTOP)
    page.click('#book-switch .book-btn[data-book="practice"]')
    wait_js(page, "() => document.documentElement.getAttribute('data-book') === 'practice'")
    page.goto(f"{BASE}/#book=real", wait_until="networkidle")
    wait_js(page, "() => document.documentElement.getAttribute('data-book') !== 'practice'")
    assert _book_pressed(page, "real") and not _book_pressed(page, "practice")
    assert page.locator("#book-banner:not([hidden])").count() == 0
    assert "[Practice]" not in page.title()
    page.close()