"""UI smoke test: drives the dashboard in headless Chromium and prints DOM/layout facts.

Dev helper — not part of the app. Run:
    .venv/bin/python scripts/ui_check.py [url]
Set BROWSER_PATH=/usr/bin/brave-browser (or another Chromium) if Playwright's
own browsers are not installed.
"""
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

out = Path("data/shots")
out.mkdir(parents=True, exist_ok=True)
url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8787"
console: list[str] = []

PROBE = """() => {
  const box = (sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return {w: Math.round(r.width), h: Math.round(r.height), top: Math.round(r.top)};
  };
  const overflowX = document.documentElement.scrollWidth > window.innerWidth + 2;
  return {
    items: document.querySelectorAll('.item').length,
    tiles: document.querySelectorAll('.sector-tile').length,
    rows: document.querySelectorAll('.trow').length,
    tapeItems: document.querySelectorAll('.tape-item').length,
    briefLen: (document.querySelector('#brief-body')?.textContent || '').length,
    briefMacro: /(10Y|2Y|HY OAS|VIX)/.test(document.querySelector('#brief-body')?.textContent || ''),
    macroVisible: !document.querySelector('#macro-panel').hidden,
    macroMetrics: document.querySelectorAll('.macro-item').length,
    macroGroups: [...document.querySelectorAll('.mg-label')].map(e => e.textContent),
    calendar: [...document.querySelectorAll('.macro-group .macro-empty')].map(e => e.textContent),
    presets: document.querySelectorAll('.preset').length,
    feedCount: document.querySelector('#feed-count')?.textContent,
    status: document.querySelector('#status-main')?.textContent,
    statusSub: document.querySelector('#status-sub')?.textContent,
    firstTitle: (document.querySelector('.item-title')?.textContent || '').slice(0, 80),
    chipsOnFirst: document.querySelectorAll('.item .chip').length,
    layout: {
      topbar: box('.topbar'), rail: box('.rail'), feed: box('.feed-col'),
      sectorGrid: box('.sector-grid'), filters: box('.filters'), firstItem: box('.item'),
    },
    overflowX,
    emptyState: !!document.querySelector('#empty-state:not([hidden])'),
    visible: getComputedStyle(document.querySelector('.app')).display,
  };
}"""

with sync_playwright() as p:
    exe = os.environ.get("BROWSER_PATH")
    b = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
    pg = b.new_page(viewport={"width": 1680, "height": 1080})
    pg.on("console", lambda m: console.append(f"{m.type}: {m.text[:220]}"))
    pg.on("pageerror", lambda e: console.append(f"PAGEERROR: {e}"))
    pg.goto(url, wait_until="networkidle", timeout=60000)
    pg.wait_for_timeout(3000)
    print("=== initial ===")
    print(pg.evaluate(PROBE))
    pg.screenshot(path=str(out / "main.png"))

    print("\n=== ticker drawer ===")
    try:
        pg.click(".trow", timeout=6000)
        pg.wait_for_timeout(3500)
        print(pg.evaluate("""() => ({
          open: document.querySelector('#ticker-modal')?.open,
          ticker: document.querySelector('#tm-ticker')?.textContent,
          name: document.querySelector('#tm-name')?.textContent,
          kv: document.querySelectorAll('#tm-kv .kv').length,
          peers: [...document.querySelectorAll('.peer-chip .psym')].map(e=>e.textContent),
          news: document.querySelectorAll('.mini-item').length,
          editFields: document.querySelectorAll('#tm-edit .kv').length,
          quote: document.querySelector('#tm-quote')?.textContent?.trim(),
        })"""))
        pg.screenshot(path=str(out / "drawer.png"))
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(400)
    except Exception as exc:  # noqa: BLE001
        print("drawer fail:", exc)

    print("\n=== settings ===")
    try:
        pg.click("#btn-settings", timeout=6000)
        pg.wait_for_timeout(2500)
        print(pg.evaluate("""() => ({
          open: document.querySelector('#settings-modal')?.open,
          fields: document.querySelectorAll('#settings-grid .setting').length,
          health: document.querySelectorAll('.health-row').length,
          models: [document.querySelector('#set-llm_model_fast')?.value,
                   document.querySelector('#set-llm_model_deep')?.value],
        })"""))
        pg.screenshot(path=str(out / "settings.png"))
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(400)
    except Exception as exc:  # noqa: BLE001
        print("settings fail:", exc)

    print("\n=== sector filter ===")
    try:
        pg.click(".sector-tile", timeout=6000)
        pg.wait_for_timeout(2500)
        print(pg.evaluate("""() => ({
          activeTiles: document.querySelectorAll('.sector-tile.active').length,
          items: document.querySelectorAll('.item').length,
          allHaveSector: [...document.querySelectorAll('.item')]
            .every(i => i.querySelector('.chip.sec')),
        })"""))
        pg.screenshot(path=str(out / "sector-filter.png"))
    except Exception as exc:  # noqa: BLE001
        print("sector fail:", exc)

    print("\n=== search + relevance filter ===")
    try:
        pg.fill("#search", "nvidia")
        pg.wait_for_timeout(1800)
        n = pg.evaluate("document.querySelectorAll('.item').length")
        titles = pg.evaluate("[...document.querySelectorAll('.item-title')].slice(0,3).map(e=>e.textContent.slice(0,60))")
        print({"searchItems": n, "titles": titles})
        pg.fill("#search", "")
        pg.wait_for_timeout(1200)
        pg.eval_on_selector("#min-rel", "el => { el.value = 60; el.dispatchEvent(new Event('input')); el.dispatchEvent(new Event('change')); }")
        pg.wait_for_timeout(1800)
        rels = pg.evaluate("[...document.querySelectorAll('.rel-badge')].map(e=>+e.textContent)")
        print({"afterRel60": len(rels), "minRel": min(rels) if rels else None})
    except Exception as exc:  # noqa: BLE001
        print("filter fail:", exc)

    print("\n=== responsive (390px) ===")
    try:
        pg.set_viewport_size({"width": 390, "height": 844})
        pg.wait_for_timeout(1200)
        print(pg.evaluate("""() => ({
          overflowX: document.documentElement.scrollWidth > window.innerWidth + 2,
          tilesPerRow: getComputedStyle(document.querySelector('.sector-grid')).gridTemplateColumns.split(' ').length,
          macroVisible: !document.querySelector('#macro-panel').hidden,
          macroWidth: Math.round(document.querySelector('#macro-panel').getBoundingClientRect().width),
        })"""))
    except Exception as exc:  # noqa: BLE001
        print("responsive fail:", exc)

    b.close()

print("\n=== console messages ===")
for line in console[:30]:
    print(" ", line)
print("done")
