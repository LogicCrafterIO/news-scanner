"""Headless CLI: print the macro desk (yields, curve, credit, commodities, calendar).

Usage:
    .venv/bin/python scripts/macro.py           # formatted tape
    .venv/bin/python scripts/macro.py --json    # raw snapshot (for scripts)
    .venv/bin/python scripts/macro.py --force   # bypass the in-process caches
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import macro                       # noqa: E402
from app.sources import Sources             # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser(description="Print the macro tape used by the desk note.")
    ap.add_argument("--json", action="store_true", help="print the full snapshot as JSON")
    ap.add_argument("--force", action="store_true", help="ignore cached series")
    args = ap.parse_args()

    async with Sources() as src:
        snapshot = await macro.fetch(src, force=args.force)
    if args.json:
        print(macro.as_json(snapshot))
    else:
        print(macro.render_text(snapshot))
        print("\n" + "-" * 78)
        print("desk-note line: " + (snapshot.get("line") or "-"))


if __name__ == "__main__":
    asyncio.run(main())
