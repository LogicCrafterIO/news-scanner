"""Headless CLI: add tickers and run one scan (handy for cron / testing).

Usage:
    .venv/bin/python scripts/scan.py                 # scan the saved watchlist
    .venv/bin/python scripts/scan.py AAPL NVDA MSFT  # add tickers, then scan
    .venv/bin/python scripts/scan.py --json          # machine-readable summary
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import db                      # noqa: E402
from app.scanner import scanner            # noqa: E402
from app.util import time_ago              # noqa: E402


async def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    as_json = "--json" in sys.argv
    if args:
        db.set_position_many(args, "watch")

    queue = scanner.subscribe()

    async def drain() -> None:
        while True:
            ev = await queue.get()
            if as_json:
                continue
            kind = ev.get("type")
            if kind == "progress":
                p = ev
                if p.get("total"):
                    print(f"  [{p.get('stage'):9s}] {p.get('done', 0):3d}/{p.get('total')} {p.get('message', '')}")
                else:
                    print(f"  [{p.get('stage'):9s}] {p.get('message', '')}")
            elif kind == "scan_done":
                print(f"  done in {ev['stats'].get('elapsed_ms', 0)} ms")
            elif kind == "scan_error":
                print(f"  ERROR: {ev.get('error')}")
            elif kind == "brief":
                print("\n" + "─" * 78 + "\nDESK NOTE\n" + ev["text"] + "\n" + "─" * 78)

    task = asyncio.create_task(drain())
    result = await scanner.scan(trigger="cli")
    task.cancel()

    stats = result.get("stats") or {}
    top = db.list_items(limit=12)
    counts = db.counts_by_sector()
    if as_json:
        print(json.dumps({"stats": stats, "counts": counts,
                          "items": [scanner.to_public(i) for i in top]}, indent=1)[:20000])
        return
    print(f"\nscan stats: {json.dumps(stats)}")
    print(f"sector counts: {json.dumps(counts)}")
    print(f"db: {json.dumps(db.stats())}\n")
    for it in top:
        print(f"[{int(it.get('relevance') or 0):3d}] {it.get('scope'):7s} {time_ago(it.get('published_at')):>8s}  "
              f"{(it.get('source') or '')[:22]:22s}  {it['title'][:96]}")
        if it.get("why"):
            print(f"        ↳ {it['why']}")
        if it.get("tickers") or it.get("sectors"):
            print(f"        {','.join(it.get('tickers') or [])} | {','.join(it.get('sectors') or [])}")


if __name__ == "__main__":
    asyncio.run(main())
