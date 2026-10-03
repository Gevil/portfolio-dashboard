#!/usr/bin/env python3
"""Capture docs media from the DEMO instance only (http://127.0.0.1:8699, fictional data).
Output: $MEDIA_DIR/raw (default /tmp/pd-media/raw). Run tools/demo/run.sh instead of calling directly."""
import os, pathlib, sys, time
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8699"
OUT = pathlib.Path(os.environ.get("MEDIA_DIR", "/tmp/pd-media")) / "raw"; OUT.mkdir(parents=True, exist_ok=True)
CRED = {"origin": BASE, "username": "demo", "password": "demo-pass"}
log = []


def step(name, fn):
    try:
        fn(); log.append(("ok", name))
    except Exception as e:  # optional steps must not abort the run
        log.append(("SKIP", f"{name}: {str(e)[:120]}"))


def ready(page):
    page.goto(BASE, wait_until="networkidle")
    page.wait_for_selector(".pos-row", timeout=40000)
    page.wait_for_timeout(2500)


def tab(page, view, wait=2500):
    page.click(f'.tab[data-view="{view}"]')
    page.wait_for_timeout(wait)


def close_dialog(page):
    page.keyboard.press("Escape"); page.wait_for_timeout(500)


def select_holding(page, idx=0):
    page.locator(".pos-row .pos-btn").nth(idx).click()
    page.wait_for_selector(".dt-pane .dt-chart-host canvas.ch-canvas-price", timeout=25000)
    page.wait_for_timeout(3000)


with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)

    # ---------------- desktop screenshots (dark + light)
    for scheme in ("dark", "light"):
        ctx = b.new_context(viewport={"width": 1440, "height": 900}, color_scheme=scheme, http_credentials=CRED, device_scale_factor=1)
        pg = ctx.new_page(); ready(pg)
        pg.screenshot(path=OUT / f"overview-{scheme}.png")
        if scheme == "dark":
            step("allocation", lambda: (pg.locator('[data-mode="allocation"]').click(), pg.wait_for_timeout(1200), pg.screenshot(path=OUT / "allocation-dark.png"), pg.locator('[data-mode="table"]').click(), pg.wait_for_timeout(600)))
            step("detail", lambda: (select_holding(pg, 0), pg.screenshot(path=OUT / "detail-dark.png")))
            step("detail-range", lambda: (pg.click('.dt-chart-tools button[data-range="3M"]'), pg.wait_for_timeout(2500), pg.screenshot(path=OUT / "detail-3m-dark.png")))
            step("detail-scroll", lambda: (pg.evaluate("document.querySelector('.dt-pane').scrollIntoView()"), pg.mouse.wheel(0, 900), pg.wait_for_timeout(1200), pg.screenshot(path=OUT / "detail-analysis-dark.png")))
            step("alerts", lambda: (pg.click("#btn-alerts"), pg.wait_for_selector("[role=dialog]:visible"), pg.wait_for_timeout(1500), pg.screenshot(path=OUT / "alert-center-dark.png"), close_dialog(pg)))
            step("settings-watchlist", lambda: (pg.click("#btn-settings"), pg.wait_for_selector("[role=dialog]:visible"), pg.wait_for_timeout(1200), pg.screenshot(path=OUT / "settings-watchlist-dark.png")))
            step("settings-positions", lambda: (pg.get_by_role("tab", name="Positions").click(), pg.wait_for_timeout(900), pg.screenshot(path=OUT / "settings-positions-dark.png")))
            step("settings-rules", lambda: (pg.get_by_role("tab", name="Alert rules").click(), pg.wait_for_timeout(900), pg.screenshot(path=OUT / "settings-rules-dark.png"), close_dialog(pg)))
            for view in ("digest", "market", "aiops"):
                step(view, lambda v=view: (tab(pg, v, 4500), pg.screenshot(path=OUT / f"{v}-dark.png")))
            step("digest-full", lambda: (tab(pg, "digest", 3500), pg.screenshot(path=OUT / "digest-full-dark.png", full_page=True)))
            step("market-full", lambda: (tab(pg, "market", 4500), pg.screenshot(path=OUT / "market-full-dark.png", full_page=True)))
            step("aiops-full", lambda: (tab(pg, "aiops", 4500), pg.screenshot(path=OUT / "aiops-full-dark.png", full_page=True)))
            step("report", lambda: (tab(pg, "digest", 3000), pg.get_by_role("button", name="Report").first.click(), pg.wait_for_selector("[role=dialog]:visible"), pg.wait_for_timeout(2000), pg.screenshot(path=OUT / "report-dark.png"), close_dialog(pg)))
        ctx.close()

    # ---------------- mobile
    ctx = b.new_context(viewport={"width": 390, "height": 844}, color_scheme="dark", http_credentials=CRED, device_scale_factor=2, is_mobile=True, has_touch=True)
    pg = ctx.new_page(); ready(pg)
    pg.screenshot(path=OUT / "mobile-overview.png")
    step("mobile-detail", lambda: (pg.locator(".pos-row .pos-btn").first.click(), pg.wait_for_selector("[role=dialog] .dt-pane", timeout=20000), pg.wait_for_timeout(3000), pg.screenshot(path=OUT / "mobile-detail.png")))
    ctx.close()

    # ---------------- walkthrough video (1280x800)
    vid_dir = OUT / "video"; vid_dir.mkdir(exist_ok=True)
    ctx = b.new_context(viewport={"width": 1280, "height": 800}, color_scheme="dark", http_credentials=CRED, record_video_dir=str(vid_dir), record_video_size={"width": 1280, "height": 800})
    pg = ctx.new_page(); ready(pg)
    pg.wait_for_timeout(2500)
    step("v-sort", lambda: (pg.locator('[data-mode="allocation"]').click(), pg.wait_for_timeout(2200), pg.locator('[data-mode="table"]').click(), pg.wait_for_timeout(1000)))
    step("v-detail", lambda: (select_holding(pg, 0), pg.wait_for_timeout(1500)))
    for r in ("1W", "3M", "1Y"):
        step(f"v-range-{r}", lambda r=r: (pg.click(f'.dt-chart-tools button[data-range="{r}"]'), pg.wait_for_timeout(2300)))
    step("v-ema", lambda: (pg.locator('[data-overlay="ema20"]').click(), pg.wait_for_timeout(1500), pg.locator('[data-overlay="rsi14"]').click(), pg.wait_for_timeout(2000)))
    step("v-alerts", lambda: (pg.click("#btn-alerts"), pg.wait_for_selector("[role=dialog]:visible"), pg.wait_for_timeout(3500), close_dialog(pg)))
    for view in ("digest", "market", "aiops"):
        step(f"v-{view}", lambda v=view: (tab(pg, v, 3800), pg.mouse.wheel(0, 700), pg.wait_for_timeout(1800), pg.mouse.wheel(0, -700), pg.wait_for_timeout(500)))
    step("v-settings", lambda: (tab(pg, "overview", 1500), pg.click("#btn-settings"), pg.wait_for_selector("[role=dialog]:visible"), pg.wait_for_timeout(2000), pg.get_by_role("tab", name="Positions").click(), pg.wait_for_timeout(2000), pg.get_by_role("tab", name="Appearance").click(), pg.wait_for_timeout(1200), pg.get_by_label("Light", exact=True).check(), pg.wait_for_timeout(2500), close_dialog(pg)))
    pg.wait_for_timeout(1500)
    ctx.close()  # flushes the video
    b.close()

vids = sorted((OUT / "video").glob("*.webm"))
print("video:", [v.name for v in vids])
for s, m in log:
    print(f"{s:5s} {m}")
