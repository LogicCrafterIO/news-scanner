"""Capture UI screenshots for verification (dev helper)."""
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

out = Path("data/shots"); out.mkdir(parents=True, exist_ok=True)
url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8787"

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1680, "height": 1080}, device_scale_factor=1)
    pg.goto(url, wait_until="networkidle", timeout=60000)
    pg.wait_for_timeout(3000)
    pg.screenshot(path=str(out / "main.png"))
    print("errors:", pg.evaluate("window.__errs || []"))
    # open a ticker drawer
    try:
        pg.click(".trow", timeout=5000)
        pg.wait_for_timeout(3000)
        pg.screenshot(path=str(out / "drawer.png"))
        pg.keyboard.press("Escape")
    except Exception as e:
        print("drawer fail:", e)
    # settings
    try:
        pg.click("#btn-settings", timeout=5000)
        pg.wait_for_timeout(1500)
        pg.screenshot(path=str(out / "settings.png"))
        pg.keyboard.press("Escape")
    except Exception as e:
        print("settings fail:", e)
    # sector filter + rel filter
    try:
        pg.click(".sector-tile", timeout=5000)
        pg.wait_for_timeout(1800)
        pg.screenshot(path=str(out / "sector-filter.png"))
    except Exception as e:
        print("sector fail:", e)
    console = []
    pg.on("console", lambda m: console.append(f"{m.type}: {m.text}"))
    b.close()
print("done")
