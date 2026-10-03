import os, pathlib
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8699"
OUT = pathlib.Path(os.environ.get("MEDIA_DIR", "/tmp/pd-media")) / "raw"
CRED = {"origin": BASE, "username": "demo", "password": "demo-pass"}
with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    ctx = b.new_context(viewport={"width": 1440, "height": 1300}, color_scheme="dark", http_credentials=CRED)
    pg = ctx.new_page()
    pg.goto(BASE, wait_until="networkidle")
    pg.wait_for_selector(".pos-row", timeout=40000)
    pg.locator(".pos-row .pos-btn").first.click()
    pg.wait_for_selector(".dt-pane .dt-chart-host canvas.ch-canvas-price", timeout=25000)
    pg.wait_for_timeout(2500)
    pg.click('.dt-chart-tools button[data-range="3M"]')
    pg.locator('[data-overlay="ema20"]').click()
    pg.wait_for_timeout(3500)
    pg.screenshot(path=OUT / "detail-tall-dark.png")
    b.close()
print("ok")
