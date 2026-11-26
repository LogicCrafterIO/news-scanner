"""FastAPI app: REST API, SSE live stream, static dashboard."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import config as C
from . import technical
from .db import db
from .industry import resolver
from .llm import LLM, usage_snapshot
from .scanner import scanner
from .sources import Sources
from .util import clean_text, iso, normalize_side

PRESETS: dict[str, list[str]] = {
    "Magnificent 7": ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA"],
    "AI / semis": ["NVDA", "AMD", "AVGO", "MU", "TSM", "ASML", "ARM", "MRVL"],
    "Banks": ["JPM", "BAC", "GS", "MS", "C", "WFC", "SCHW"],
    "Energy": ["XOM", "CVX", "COP", "SLB", "EOG", "OXY"],
    "Health care": ["LLY", "UNH", "JNJ", "MRK", "ABBV", "PFE", "AMGN"],
    "US indices": ["SPY", "QQQ", "IWM", "DIA"],
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    scanner.hydrate_brief()          # restore the last desk note from SQLite
    scanner.hydrate_macro()          # restore the last macro tape from cache
    scanner.hydrate_technicals()     # restore price-action snapshots after a restart
    scanner.start_scheduler()
    # the macro tape should be there on first paint, even with an empty watchlist
    asyncio.create_task(_initial_macro())
    # first paint: if the user already has a watchlist, kick off a scan
    if db.list_positions():
        asyncio.create_task(_delayed_scan(4))
    yield
    scanner.stop_scheduler()


async def _initial_macro() -> None:
    if scanner.macro or db.setting("macro_enabled", "1") != "1":
        return
    try:
        async with Sources() as src:
            await scanner.refresh_macro(src)
    except Exception:  # noqa: BLE001  (the macro desk is optional; never block boot)
        pass


async def _delayed_scan(delay: float) -> None:
    await asyncio.sleep(delay)
    if not scanner.scanning:
        await scanner.scan(trigger="startup")


app = FastAPI(title="News Scanner", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(C.STATIC_DIR)), name="static")


# --------------------------------------------------------------------------- #
# pages
# --------------------------------------------------------------------------- #
@app.get("/")
async def index() -> FileResponse:
    return FileResponse(str(C.STATIC_DIR / "index.html"))


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #
@app.get("/api/state")
async def state() -> dict:
    settings = db.settings()
    positions = db.list_positions()
    out_positions = []
    for p in positions:
        sec = db.get_security(p["ticker"]) or {}
        out_positions.append({
            **p,
            "side": p.get("side"),
            "name": sec.get("name") or p["ticker"],
            "sector": sec.get("sector"), "sector_key": sec.get("sector_key"),
            "sub_industry": sec.get("sub_industry"),
            "peers": sec.get("peers") or [],
            "source": sec.get("source"),
            "quote": scanner.quotes.get(p["ticker"].upper()),
            "tech": technical.light(scanner.technicals[p["ticker"].upper()])
                    if p["ticker"].upper() in scanner.technicals else None,
        })
    sides = {p["ticker"].upper(): p.get("side") for p in positions}
    kinds = {p["ticker"].upper(): p.get("kind") for p in positions}
    techs = {t: s for t, s in scanner.technicals.items() if t in sides}
    return {
        "settings": settings,
        "positions": out_positions,
        "sectors": [{"key": s["key"], "name": s["name"], "etf": s["etf"], "color": s["color"]}
                    for s in C.SECTORS],
        "topics": [{"key": t["key"], "name": t["name"]} for t in C.MACRO_TOPICS],
        "scanner": {
            "scanning": scanner.scanning,
            "last_scan_at": scanner.last_scan_at,
            "last_stats": scanner.last_stats,
            "progress": scanner.progress,
            "brief": scanner.brief,
            "brief_at": scanner.brief_at,
            "brief_count": db.count_briefs(),
            "quotes_at": scanner.quotes_at,
            "source_errors": scanner.source_errors[:20],
            "tech_scanning": scanner.tech_scanning,
        },
        "counts": db.counts_by_sector(),
        "topic_counts": db.counts_by_topic(),
        "etfs": scanner.etf_quotes,
        "quotes": scanner.quotes,
        "quotes_at": scanner.quotes_at,
        "macro": scanner.macro,
        "macro_at": scanner.macro_at,
        "technicals": {t: technical.light(s) for t, s in techs.items()},
        "tech_summary": technical.book_summary(techs, sides, kinds) if techs else {},
        "tech_at": scanner.tech_at,
        "db": db.stats(),
        "llm_usage": usage_snapshot(),
        "server_time": iso(),
        "presets": PRESETS,
        "gics_index_rows": db.gics_count(),
    }


@app.get("/api/health")
async def health() -> dict:
    return {"sources": db.health(), "runs": db.last_runs(5)}


# --------------------------------------------------------------------------- #
# positions / watchlist
# --------------------------------------------------------------------------- #
@app.post("/api/positions")
async def add_positions(payload: dict) -> dict:
    tickers = payload.get("tickers") or payload.get("ticker") or []
    if isinstance(tickers, str):
        tickers = [t for t in tickers.replace(",", " ").split() if t]
    items = payload.get("items") or None
    kind = payload.get("kind", "watch")
    if not isinstance(tickers, list):
        raise HTTPException(400, "tickers must be a list or string")
    added = []
    entries: list[dict] = []
    if items:
        entries = items
    else:
        for t in tickers:
            if isinstance(t, dict):
                entries.append(t)
            else:
                entries.append({"ticker": str(t)})
    for e in entries:
        ticker = str(e.get("ticker", "")).strip().upper()
        if not ticker:
            continue
        if len(ticker) > 10 or not all(ch.isalnum() or ch in ".-^" for ch in ticker):
            continue
        db.upsert_position(
            ticker, kind=e.get("kind", kind), shares=e.get("shares"),
            avg_cost=e.get("avg_cost"), note=e.get("note"), ir_feed=e.get("ir_feed"),
            deep=1 if e.get("deep") else 0, side=e.get("side", payload.get("side")),
        )
        added.append(ticker)
    if added:
        asyncio.create_task(_resolve_only(added))
        asyncio.create_task(_auto_scan_if_idle())
        if scanner.technicals:
            scanner.publish_technicals(_sides())
    return {"ok": True, "added": added}


def _sides() -> dict[str, str | None]:
    return {p["ticker"].upper(): p.get("side") for p in db.list_positions()}


async def _resolve_only(tickers: list[str]) -> None:
    async with Sources() as src, LLM() as llm:
        await resolver.ensure_index(src)
        await resolver.resolve_many(src, llm if llm.enabled else None, tickers)


async def _auto_scan_if_idle() -> None:
    await asyncio.sleep(2)
    if not scanner.scanning:
        await scanner.scan(trigger="watchlist")


@app.patch("/api/positions/{ticker}")
async def patch_position(ticker: str, payload: dict) -> dict:
    pos = {p["ticker"]: p for p in db.list_positions()}.get(ticker.upper())
    if not pos:
        raise HTTPException(404, "not in watchlist")
    db.upsert_position(
        ticker, kind=payload.get("kind", pos["kind"]),
        shares=payload.get("shares", pos["shares"]),
        avg_cost=payload.get("avg_cost", pos["avg_cost"]),
        note=payload.get("note", pos["note"]),
        ir_feed=payload.get("ir_feed", pos["ir_feed"]),
        deep=1 if payload.get("deep", pos.get("deep")) else 0,
    )
    # side is explicitly settable/clearable, which COALESCE in upsert_position forbids
    if "side" in payload:
        db.set_position_side(ticker, normalize_side(payload.get("side")))
    if payload.get("refresh_industry"):
        asyncio.create_task(_resolve_only([ticker.upper()]))
    if scanner.technicals:
        scanner.publish_technicals(_sides())
    return {"ok": True, "position": db.list_positions()}


@app.delete("/api/positions/{ticker}")
async def delete_position(ticker: str) -> dict:
    db.delete_position(ticker)
    db.delete_technical(ticker)
    scanner.technicals.pop(ticker.upper(), None)
    if scanner.technicals:
        scanner.publish_technicals(_sides())
    return {"ok": True}


# --------------------------------------------------------------------------- #
# feed
# --------------------------------------------------------------------------- #
@app.get("/api/items")
async def items(
    scope: str | None = None,
    sector: str | None = None,
    topic: str | None = None,
    ticker: str | None = None,
    q: str | None = None,
    limit: int = Query(120, ge=1, le=500),
    since_id: int | None = None,
    min_relevance: float = 0.0,
    hours: float | None = None,
    include_dups: bool = False,
    sort: str = Query("published", pattern="^(published|published_asc|added|relevance|smart)$"),
) -> dict:
    max_hours = hours or float(db.setting("item_max_age_hours", "48"))
    rows = db.list_items(limit=limit, scope=scope, sector=sector, topic=topic, ticker=ticker,
                         query=q, since_id=since_id, min_relevance=min_relevance,
                         include_dups=include_dups, max_age_hours=max_hours, sort=sort)
    return {
        "items": [scanner.to_public(r) for r in rows],
        "counts": db.counts_by_sector(),
        "server_time": iso(),
        "total": db.stats(),
    }


@app.get("/api/items/{item_id}")
async def item_detail(item_id: int) -> dict:
    it = db.get_item(item_id)
    if not it:
        raise HTTPException(404, "not found")
    return scanner.to_public(it)


# --------------------------------------------------------------------------- #
# securities
# --------------------------------------------------------------------------- #
@app.get("/api/security/{ticker}")
async def security(ticker: str, refresh: bool = False) -> dict:
    ticker = ticker.upper()
    async with Sources() as src, LLM() as llm:
        await resolver.ensure_index(src)
        sec = await resolver.resolve(src, llm if llm.enabled else None, ticker, force=refresh)
        if refresh or ticker not in scanner.quotes:
            q = await src.quote(ticker)
            if q:
                scanner.quotes[ticker] = q
        snap = scanner.technicals.get(ticker) or db.get_technical(ticker)
        tracked = any(p["ticker"].upper() == ticker for p in db.list_positions())
        if (not snap or refresh) and tracked and db.setting("tech_enabled", "1") == "1":
            # first look at a name before the scheduler has run: compute it now
            try:
                info = await src.stockanalysis_history(
                    ticker, bars=max(60, int(db.setting("tech_bars", "260"))))
                snap = technical.analyse(ticker, info["bars"], quote=scanner.quotes.get(ticker),
                                         name=sec.get("name"))
                db.upsert_technical(ticker, snap)
                scanner.technicals[ticker] = snap
            except Exception:  # noqa: BLE001  (the drawer still renders without it)
                snap = snap or None
    news = db.items_for_ticker(ticker, limit=40)
    return {"security": sec, "quote": scanner.quotes.get(ticker), "technical": snap, "items": [
        scanner.to_public(i) for i in news
    ]}


# --------------------------------------------------------------------------- #
# technical scanner
# --------------------------------------------------------------------------- #
@app.get("/api/technicals")
async def technicals(side: str | None = None, kind: str | None = None) -> dict:
    positions = {p["ticker"].upper(): p for p in db.list_positions()}
    snaps = {t: s for t, s in scanner.technicals.items() if t in positions}
    if not snaps:
        snaps = {s["ticker"]: s for s in db.list_technicals() if s["ticker"] in positions}
    sides = {t: p.get("side") for t, p in positions.items()}
    rows = []
    for t, snap in snaps.items():
        pos = positions[t]
        if side in ("long", "short") and sides.get(t) != side:
            continue
        if kind in ("position", "watch") and pos.get("kind") != kind:
            continue
        rows.append({**snap, "kind": pos.get("kind"), "side": sides.get(t),
                     "sector": (db.get_security(t) or {}).get("sector"),
                     "sector_key": (db.get_security(t) or {}).get("sector_key")})
    order = {"position": 0, "watch": 1}
    rows.sort(key=lambda r: (order.get(r.get("kind"), 2), r.get("ticker") or ""))
    return {
        "technicals": rows,
        "summary": technical.book_summary(
            snaps, sides, {t: p.get("kind") for t, p in positions.items()}),
        "at": scanner.tech_at,
        "sides": sides,
        "scanner": {"tech_scanning": scanner.tech_scanning},
    }


@app.get("/api/technicals/{ticker}")
async def technical_detail(ticker: str) -> dict:
    ticker = ticker.upper()
    snap = scanner.technicals.get(ticker) or db.get_technical(ticker)
    if not snap:
        raise HTTPException(404, "no technical snapshot for this ticker yet")
    pos = {p["ticker"].upper(): p for p in db.list_positions()}.get(ticker)
    return {"technical": snap, "side": pos.get("side") if pos else None}


@app.post("/api/technicals/scan")
async def technical_scan(force: bool = True) -> dict:
    if scanner.tech_scanning:
        return {"ok": False, "message": "technical scan already running"}
    if not db.list_positions():
        return {"ok": False, "message": "add tickers first"}
    asyncio.create_task(_tech_scan(force))
    return {"ok": True, "message": "technical scan started"}


async def _tech_scan(force: bool = True) -> None:
    positions = db.list_positions()
    tickers = [p["ticker"].upper() for p in positions]
    sides = _sides()
    if not tickers:
        return
    try:
        async with Sources() as src, LLM() as llm:
            await scanner.refresh_quotes(src, tickers)
            await scanner.refresh_technicals(src, tickers, sides, force=force)
            if llm.enabled and db.setting("tech_llm", "1") == "1":
                await scanner.enrich_technicals(llm, sides)
    except Exception as exc:  # noqa: BLE001
        scanner.emit({"type": "tech_error", "error": f"{type(exc).__name__}: {exc}"[:200]})


@app.get("/api/search")
async def search_tickers(q: str, limit: int = 12) -> dict:
    q = clean_text(q, 40)
    if not q:
        return {"results": []}
    results = db.gics_search(q, limit)
    if not results:
        async with Sources() as src:
            prof = await src.nasdaq_profile(q.upper())
        if prof:
            results = [{"ticker": q.upper(), "name": prof.get("name"),
                        "sector": prof.get("sector"), "sub_industry": prof.get("industry")}]
    return {"results": results}


@app.post("/api/industry/rebuild")
async def rebuild_industry() -> dict:
    async with Sources() as src:
        n = await resolver.ensure_index(src, force=True)
    return {"ok": True, "rows": n}


# --------------------------------------------------------------------------- #
# scans / settings / llm
# --------------------------------------------------------------------------- #
@app.post("/api/scan")
async def trigger_scan() -> dict:
    if scanner.scanning:
        return {"ok": False, "message": "scan already running", "progress": scanner.progress}
    asyncio.create_task(scanner.scan(trigger="manual"))
    return {"ok": True, "message": "scan started"}


@app.get("/api/settings")
async def get_settings() -> dict:
    return db.settings()


@app.post("/api/settings")
async def set_settings(payload: dict) -> dict:
    allowed = set(C.DEFAULTS.keys()) | {"llm_brief", "discovery_per_scan"}
    updates = {k: v for k, v in payload.items() if k in allowed}
    return db.set_settings(updates)


@app.post("/api/llm/test")
async def llm_test() -> dict:
    async with LLM() as llm:
        models = await llm.list_models()
        reply = await llm.chat(
            [{"role": "user", "content": "Reply with exactly: OK"}], json_mode=False
        ) if llm.key else None
    return {
        "key_present": bool(C.OLLAMA_API_KEY),
        "base_url": C.OLLAMA_BASE_URL,
        "fast_model": db.setting("llm_model_fast"),
        "deep_model": db.setting("llm_model_deep"),
        "models_available": models[:40],
        "reply": (reply or "")[:200],
        "last_error": scanner and None,
    }


@app.post("/api/positions/{ticker}/ir")
async def discover_ir(ticker: str) -> dict:
    ticker = ticker.upper()
    pos = {p["ticker"]: p for p in db.list_positions()}.get(ticker)
    if not pos:
        raise HTTPException(404, "not in watchlist")
    sec = db.get_security(ticker) or {}
    async with Sources() as src, LLM() as llm:
        feed = await resolver.discover_ir_feed(src, llm, ticker, sec.get("name"))
    if feed:
        db.upsert_position(ticker, kind=pos["kind"], ir_feed=feed, deep=1)
    return {"ok": bool(feed), "feed_url": feed}


# --------------------------------------------------------------------------- #
# macro desk
# --------------------------------------------------------------------------- #
@app.get("/api/macro")
async def macro_snapshot(refresh: bool = False) -> dict:
    """US yields and curve, credit, dollar, commodities, economy and calendar."""
    if refresh or not scanner.macro:
        async with Sources() as src:
            await scanner.refresh_macro(src, force=refresh)
    return {"macro": scanner.macro, "at": scanner.macro_at}


# --------------------------------------------------------------------------- #
# briefs
# --------------------------------------------------------------------------- #
@app.get("/api/brief")
async def get_brief() -> dict:
    return {"brief": scanner.brief, "at": scanner.brief_at}


@app.post("/api/brief")
async def make_brief(force: bool = False) -> dict:
    """Regenerate the desk note on demand (force=true ignores the rate limit)."""
    if scanner.brief and not force:
        return {"brief": scanner.brief, "at": scanner.brief_at, "cached": True,
                "count": db.count_briefs()}
    # a manual regenerate before the first scan should still be macro-aware
    if not scanner.macro and db.setting("macro_enabled", "1") == "1":
        async with Sources() as src:
            await scanner.refresh_macro(src)
    async with LLM() as llm:
        text = await scanner.write_brief(llm, force=force, trigger="manual")
    return {"brief": text or scanner.brief, "at": scanner.brief_at, "cached": False,
            "count": db.count_briefs()}


@app.get("/api/briefs")
async def brief_history(limit: int = Query(50, ge=1, le=500),
                        before: str | None = None) -> dict:
    """Archived desk notes, newest first. `before` pages back by created_at."""
    rows = db.list_briefs(limit=limit, before=before)
    return {"briefs": rows, "count": db.count_briefs(),
            "latest_id": rows[0]["id"] if rows else None}


@app.get("/api/briefs/{brief_id}")
async def brief_by_id(brief_id: int) -> dict:
    row = db.get_brief(brief_id)
    if not row:
        raise HTTPException(404, "desk note not found")
    return row


# --------------------------------------------------------------------------- #
# SSE
# --------------------------------------------------------------------------- #
@app.get("/api/stream")
async def stream(request: Request) -> StreamingResponse:
    queue = scanner.subscribe()

    async def gen():
        try:
            hello = {
                "type": "hello",
                "scanning": scanner.scanning,
                "progress": scanner.progress,
                "last_scan_at": scanner.last_scan_at,
                "server_time": iso(),
            }
            yield f"data: {json.dumps(hello)}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {json.dumps(ev, default=str)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            scanner.unsubscribe(queue)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive",
    })


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception) -> JSONResponse:  # pragma: no cover
    return JSONResponse(status_code=500, content={"error": f"{type(exc).__name__}: {exc}"})
