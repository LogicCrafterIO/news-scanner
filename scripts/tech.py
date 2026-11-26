"""Headless technical scan for the watchlist + positions.

Dev helper — not part of the app. Run:
    .venv/bin/python scripts/tech.py            # computed read only
    .venv/bin/python scripts/tech.py --llm      # add the cached LLM second opinion
    .venv/bin/python scripts/tech.py --json     # machine-readable output
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import technical  # noqa: E402
from app.db import db  # noqa: E402
from app.llm import LLM  # noqa: E402
from app.scanner import scanner  # noqa: E402
from app.sources import Sources  # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", action="store_true", help="request the cached LLM second opinion")
    ap.add_argument("--json", action="store_true", dest="as_json")
    ap.add_argument("--force", action="store_true", help="ignore the bar cache")
    args = ap.parse_args()

    positions = db.list_positions()
    if not positions:
        print("No positions or watchlist names — add some in the UI first.")
        return
    tickers = [p["ticker"].upper() for p in positions]
    sides = {p["ticker"].upper(): p.get("side") for p in positions}
    async with Sources() as src:
        if args.llm:
            async with LLM() as llm:
                await scanner.refresh_quotes(src, tickers)
                await scanner.refresh_technicals(src, tickers, sides, force=args.force)
                await scanner.enrich_technicals(llm, sides)
        else:
            await scanner.refresh_quotes(src, tickers)
            await scanner.refresh_technicals(src, tickers, sides, force=args.force)

    summary = scanner.tech_summary
    rows = []
    for p in positions:
        snap = scanner.technicals.get(p["ticker"].upper())
        if not snap:
            continue
        side = p.get("side")
        ctx = (snap.get("context") or {}).get(side) if side else None
        rows.append({
            "ticker": p["ticker"], "kind": p["kind"], "side": side,
            "price": snap.get("price"), "chg": snap.get("change_pct"),
            "trend": snap.get("trend"), "setup": snap.get("setup"),
            "score": snap.get("score"), "rsi": (snap.get("momentum") or {}).get("rsi14"),
            "alignment": (ctx or {}).get("alignment"),
            "read": snap.get("read"), "headline": snap.get("headline"),
        })
    if args.as_json:
        print(json.dumps({"summary": summary, "technicals": rows}, indent=1))
        return

    print(f"\n{summary.get('headline', '')}\n")
    print(f"{'TICKER':7s} {'SIDE':5s} {'TREND':11s} {'SETUP':17s} {'SCORE':>5s} "
          f"{'RSI':>4s}  {'CONTEXT':10s}  HEADLINE")
    print("-" * 118)
    for r in rows:
        side = (r["side"] or "—")[:5]
        trend = (r["trend"] or "—")[:11]
        setup = (r["setup"] or "—")[:17]
        score = f"{r['score']:+.0f}" if r["score"] is not None else "—"
        rsi = f"{r['rsi']:.0f}" if r["rsi"] is not None else "—"
        ctx = (r["alignment"] or "—")[:10]
        print(f"{r['ticker']:7s} {side:5s} {trend:11s} {setup:17s} {score:>5s} "
              f"{rsi:>4s}  {ctx:10s}  {r['headline'] or ''}")
    conflicts = summary.get("conflicts") or []
    if conflicts:
        print("\nside conflicts:")
        for c in conflicts:
            print(f"  {c['ticker']} {c['side']}: {c['trend']} / {c['setup']} "
                  f"(score {c['score']:+.0f})")
    print(f"\nupdated {scanner.tech_at} · {summary.get('total', 0)} names")


if __name__ == "__main__":
    asyncio.run(main())
