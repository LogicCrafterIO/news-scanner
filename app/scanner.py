"""Scan orchestration: fan-out over sources, dedupe/cluster, tag, score, LLM pass."""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import timedelta
from typing import Any

from . import config as C
from . import macro, technical
from .db import db
from .industry import resolver, sic_to_sector  # noqa: F401  (sic_to_sector re-exported)
from .llm import LLM, usage_snapshot
from .sources import FetchResult, Sources
from .util import (age_hours, canonical_url, clean_text, domain_of, iso, jaccard, now_utc,
                   parse_dt, sha1, strip_source_suffix, tokens)

SOURCE_TIER: dict[str, int] = {f["id"]: f["tier"] for f in C.DIRECT_FEEDS}
SOURCE_TIER.update({"sec": 1, "websearch": 2, "stocktwits": 4})

SCOPE_BASE = {"ticker": 52, "peer": 40, "sector": 34, "macro": 29, "market": 16}
SCOPE_PRIORITY = {"ticker": 0, "peer": 1, "sector": 2, "macro": 3, "market": 4}

_TICKER_RE = re.compile(r"(?:\$|\b)([A-Z]{1,5}(?:\.[A-Z]{1,2})?)\b")
_CASHTAG_RE = re.compile(r"\$([A-Za-z]{1,6})\b")

# Ticker symbols that are common English words: only trusted via cashtag or name match
AMBIGUOUS = {
    "A", "ALL", "AN", "ANY", "ARE", "AS", "AT", "BE", "BY", "CAN", "CAT", "CEO", "CFO", "COST",
    "DAY", "DID", "DO", "EAT", "EV", "FOR", "GET", "GO", "GOOD", "HAS", "HE", "HOT", "I", "IF",
    "IN", "IS", "IT", "KEY", "K", "LOVE", "M", "MAN", "ME", "MO", "MY", "NEW", "NOW", "OF", "ON",
    "ONE", "OR", "OUT", "PLAY", "PM", "SEE", "SO", "T", "TELL", "TO", "TWO", "UP", "US", "V",
    "WANT", "WAS", "WE", "WELL", "WHO", "WHY", "YOU", "AI", "AM", "AN", "ANY", "B", "C", "D",
    "F", "G", "H", "J", "L", "N", "O", "P", "Q", "R", "S", "U", "W", "X", "Y", "Z", "UK", "EU",
}

# Reputable market/news publishers: used to weight items and to keep LLM web-search
# discovery from surfacing content farms.
TRUSTED_DOMAINS = {
    "reuters.com", "bloomberg.com", "cnbc.com", "wsj.com", "ft.com", "barrons.com",
    "marketwatch.com", "nytimes.com", "washingtonpost.com", "apnews.com", "axios.com",
    "economist.com", "forbes.com", "fortune.com", "businessinsider.com", "yahoo.com",
    "finance.yahoo.com", "seekingalpha.com", "investing.com", "benzinga.com",
    "businesswire.com", "prnewswire.com", "globenewswire.com", "sec.gov", "nasdaq.com",
    "stocktitan.net", "morningstar.com", "zacks.com", "tipranks.com", "thestreet.com",
    "fool.com", "insider.com", "techcrunch.com", "theverge.com", "arstechnica.com",
    "cnbc.com", "foxbusiness.com", "bnnbloomberg.ca", "scmp.com", "nikkei.com",
    "asia.nikkei.com", "japantimes.co.jp", "ftchinese.com", "theinformation.com",
    "semianalysis.com", "lightreading.com", "datacenterdynamics.com", "utilitydive.com",
    "spglobal.com", "fitchratings.com", "moodys.com", "bis.org",
    "federalreserve.gov", "bls.gov", "bea.gov", "treasury.gov", "ecb.europa.eu", "imf.org",
    "bbc.co.uk", "bbc.com", "theguardian.com", "telegraph.co.uk", "independent.co.uk",
    "handelsblatt.com", "lesechos.fr", "boerse-frankfurt.de",
    "stockanalysis.com", "simplywall.st", "gurufocus.com", "marketbeat.com",
    "streetinsider.com", "investors.com", "kiplinger.com", "business-standard.com",
    "livemint.com", "economictimes.indiatimes.com", "moneycontrol.com",
    "thehindu.com", "straitstimes.com", "channelnewsasia.com", "afr.com", "smh.com.au",
}

SPAM_HINTS = ("gotrade", "gmteight", "ad-hoc-news", "kalkine", "edgex", "beijingpost",
              "insidermonkey", "coinpedia", "dailyhodl", "bitcoinist", "ambcrypto",
              "giftlegacy", "barchart-tr", "fintel-tr", "stocktitan-tr", "marketminute",
              "pennystocks", "stocknews-tr", "tipranks-tr")

# Evergreen SEO listicles ("here's what $1,000 would be worth…") are noise for a PM
EVERGREEN_RE = re.compile(
    r"(here'?s (what|why|how)|should you (buy|sell)|better buy|prediction:|\b\d+ (stocks|things|reasons)\b"
    r"|what a \$|\$[\d,]+ (invested|in)|could be worth|is it time to buy|millionaire|by 20\d\d"
    r"|top .{0,12}stocks? (to|for)|analysts? (say|expect)|price target|buy or sell|3\.\d%|dividend yield)",
    re.I,
)


def domain_tier(url: str) -> int:
    """1 = reputable publisher, 3 = unknown, 5 = content farm / aggregator spam."""
    d = domain_of(url)
    if not d:
        return 3
    if any(h in d for h in SPAM_HINTS):
        return 5
    for trust in TRUSTED_DOMAINS:
        if d == trust or d.endswith("." + trust):
            return 1
    return 3


def norm_title(title: str) -> str:
    t = strip_source_suffix(title).lower()
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def item_hash(item: dict) -> str:
    url = canonical_url(item.get("url"))
    if url and "news.google.com" not in url:
        return sha1("u:" + url)
    return sha1("t:" + norm_title(item.get("title", "")))


def title_hash(title: str) -> str:
    return sha1("t:" + norm_title(title))


def source_tier(source_id: str) -> int:
    if source_id in SOURCE_TIER:
        return SOURCE_TIER[source_id]
    if source_id.startswith("sec"):
        return 1
    if source_id.startswith("gnews"):
        return 2
    if source_id.startswith("nasdaq"):
        return 2
    if source_id.startswith("websearch"):
        return 2
    if source_id.startswith("stocktwits"):
        return 4
    return 3


class Scanner:
    def __init__(self) -> None:
        self.scanning = False
        self.current_run: int | None = None
        self.last_stats: dict = {}
        self.last_scan_at: str | None = None
        self.subscribers: set[asyncio.Queue] = set()
        self.quotes: dict[str, dict] = {}
        self.etf_quotes: dict[str, dict] = {}
        self.quotes_at: str | None = None
        self.brief: str | None = None
        self.brief_at: str | None = None
        self._brief_marker = 0
        # macro desk
        self.macro: dict = {}
        self.macro_at: str | None = None
        self.macro_scanning = False
        self._macro_cache: tuple[float, dict] | None = None
        self._brief_macro: dict | None = None
        self.progress: dict = {"stage": "idle", "done": 0, "total": 0, "message": ""}
        self._scheduler_task: asyncio.Task | None = None
        self._discovery_cursor = 0
        self._last_ingest: dict = {}
        self.source_errors: list[dict] = []
        # technical scanner
        self.technicals: dict[str, dict] = {}      # ticker -> full snapshot (latest scan)
        self.tech_at: str | None = None
        self.tech_summary: dict = {}
        self.tech_scanning = False
        self._bar_cache: dict[str, tuple[float, list[dict]]] = {}

    # -------------------------------------------------------------- plumbing
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    def emit(self, event: dict) -> None:
        for q in list(self.subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    def _progress(self, stage: str, message: str, done: int = 0, total: int = 0) -> None:
        self.progress = {"stage": stage, "message": message, "done": done, "total": total}
        self.emit({"type": "progress", **self.progress})

    # ------------------------------------------------------------ watchlists
    def universe(self) -> tuple[list[dict], dict[str, str], dict[str, dict]]:
        """Returns (positions, ticker->name, ticker->security)."""
        positions = db.list_positions()
        names: dict[str, str] = {}
        secs: dict[str, dict] = {}
        for p in positions:
            t = p["ticker"].upper()
            sec = db.get_security(t) or {}
            secs[t] = sec
            names[t] = sec.get("name") or t
        return positions, names, secs

    # ------------------------------------------------------------------ scan
    async def scan(self, trigger: str = "manual") -> dict:
        if self.scanning:
            return {"ok": False, "error": "scan already running"}
        self.scanning = True
        self.source_errors = []
        t_start = time.perf_counter()
        u_before = usage_snapshot()
        run_id = db.start_run(trigger)
        self.current_run = run_id
        settings = db.settings()
        max_age = float(settings.get("item_max_age_hours", "48"))
        per_feed = int(settings.get("max_items_per_feed", "35"))
        per_pub = int(settings.get("max_per_publisher", "4"))
        stats: dict[str, Any] = {"trigger": trigger, "sources": 0, "raw": 0, "new": 0,
                                 "dupes": 0, "analyzed": 0, "errors": 0}
        self.emit({"type": "scan_start", "run_id": run_id, "trigger": trigger,
                   "started_at": iso()})
        self._progress("start", "Preparing watchlist & industry map")

        try:
            async with Sources(concurrency=18) as src, LLM() as llm:
                llm_enabled = llm.enabled
                positions, names, secs = self.universe()

                # ---- industry mapping (cached; rebuilt weekly)
                try:
                    await resolver.ensure_index(src)
                except Exception as exc:  # noqa: BLE001
                    self.source_errors.append({"source": "gics-index", "error": str(exc)[:150]})
                tickers = [p["ticker"].upper() for p in positions]
                if tickers:
                    self._progress("industry", f"Mapping {len(tickers)} tickers to GICS")
                    resolved = await resolver.resolve_many(src, llm if llm_enabled else None, tickers)
                    for t, sec in resolved.items():
                        secs[t] = sec
                        names[t] = sec.get("name") or names.get(t) or t

                # ---- quotes
                await self.refresh_quotes(src, tickers)

                # ---- technical read of every name in the book
                sides = {p["ticker"].upper(): p.get("side") for p in positions}
                if settings.get("tech_enabled", "1") == "1" and tickers:
                    stats.update(await self.refresh_technicals(src, tickers, sides))

                # ---- macro desk: yields, curve, credit, dollar, commodities
                if settings.get("macro_enabled", "1") == "1":
                    stats.update(await self.refresh_macro(src))

                # ---- build fetch jobs
                jobs: list[tuple[str, Any]] = []
                labels: dict[str, str] = {}

                for topic in C.MACRO_TOPICS:
                    for q in topic["queries"]:
                        jobs.append(("macro", src.google_news(
                            q, label=f"Macro · {topic['name']}", scope="macro",
                            topic=topic["key"], max_items=per_feed,
                            max_per_publisher=per_pub)))
                for feed in C.DIRECT_FEEDS:
                    if feed.get("enabled", True):
                        jobs.append(("feed", src.rss(feed, max_items=per_feed)))
                for sector in C.SECTORS:
                    for q in sector["queries"]:
                        jobs.append(("sector", src.google_news(
                            q, label=f"Sector · {sector['name']}", scope="sector",
                            sector_key=sector["key"], max_items=per_feed,
                            max_per_publisher=per_pub)))

                # peer groups: one query per unique sub-industry
                peer_groups: dict[tuple, dict] = {}
                for t in tickers:
                    sec = secs.get(t) or {}
                    peers = [p for p in (sec.get("peers") or []) if isinstance(p, dict)
                             and p.get("ticker") and p["ticker"].upper() not in tickers]
                    if not peers:
                        continue
                    key = (sec.get("sector"), sec.get("sub_industry"))
                    grp = peer_groups.setdefault(key, {"names": [], "tickers": [],
                                                       "label": sec.get("sub_industry") or "industry"})
                    for p in peers[:5]:
                        sym = p["ticker"].upper()
                        if sym in grp["tickers"]:
                            continue
                        grp["tickers"].append(sym)
                        if p.get("name"):
                            grp["names"].append(p["name"])
                for key, grp in list(peer_groups.items())[:8]:
                    names_q = [f'"{n}"' for n in grp["names"][:4]] or \
                              [f'"{t} stock"' for t in grp["tickers"][:3]]
                    if not names_q:
                        continue
                    q = f"({' OR '.join(names_q)}) when:2d"
                    jobs.append(("peer", src.google_news(
                        q, label=f"Peers · {grp['label']}", scope="peer",
                        sector_key=C.SECTOR_NAME_TO_KEY.get(key[0] or ""),
                        max_items=per_feed, max_per_publisher=per_pub)))
                    labels[f"peers:{grp['label']}"] = ",".join(grp["tickers"])

                # per-ticker jobs
                for p in positions:
                    t = p["ticker"].upper()
                    name = names.get(t) or t
                    safe_name = name.replace('"', "")
                    q_name = f'"{safe_name}" when:3d'
                    jobs.append(("ticker", src.google_news(
                        q_name, label=f"Ticker · {t}", scope="ticker", ticker=t,
                        relation="held", max_items=per_feed, max_per_publisher=per_pub)))
                    if len(safe_name.split()) <= 1 or len(safe_name) < 8:
                        jobs.append(("ticker", src.google_news(
                            f'"{t} stock" when:2d', label=f"Ticker · {t} (sym)", scope="ticker",
                            ticker=t, relation="held", max_items=per_feed,
                            max_per_publisher=per_pub)))
                    jobs.append(("ticker", src.nasdaq_news(t, max_items=20)))
                    if settings.get("sec_enabled", "1") == "1":
                        cik = (secs.get(t) or {}).get("cik")
                        if cik:
                            jobs.append(("ticker", src.sec_filings(
                                t, str(cik),
                                include_insider=settings.get("sec_insider_enabled", "0") == "1")))
                    if settings.get("stocktwits_enabled", "0") == "1":
                        jobs.append(("ticker", src.stocktwits(t, max_items=12)))
                    if p.get("ir_feed"):
                        jobs.append(("ticker", src.rss_url(
                            p["ir_feed"], source_id=f"ir:{t}", name=f"{t} Investor Relations",
                            ticker=t, relation="held")))

                stats["sources"] = len(jobs)
                self._progress("fetch", f"Fetching {len(jobs)} sources", 0, len(jobs))
                t_fetch = time.perf_counter()
                results: list[FetchResult] = []
                batch = 24
                for i in range(0, len(jobs), batch):
                    chunk = jobs[i:i + batch]
                    got = await asyncio.gather(*(c for _, c in chunk), return_exceptions=True)
                    for (kind, _), res in zip(chunk, got):
                        if isinstance(res, FetchResult):
                            results.append(res)
                            db.record_health(res.source_id, res.name, res.ok, res.ms,
                                             len(res.items), res.error)
                            if not res.ok:
                                self.source_errors.append({"source": res.source_id,
                                                           "error": (res.error or "")[:150]})
                        elif isinstance(res, Exception):
                            stats["errors"] += 1
                    self._progress("fetch", f"Fetching sources ({min(i + batch, len(jobs))}/{len(jobs)})",
                                   min(i + batch, len(jobs)), len(jobs))

                stats["errors"] = len(self.source_errors)
                raw_items: list[dict] = []
                for res in results:
                    raw_items.extend(res.items)
                stats["raw"] = len(raw_items)

                # ---- normalise, dedupe, cluster, tag
                stats["ms_fetch"] = int((time.perf_counter() - t_fetch) * 1000)
                self._progress("process", f"Processing {len(raw_items)} headlines")
                t_ingest = time.perf_counter()
                new_items = await self._ingest(raw_items, secs, names, tickers, max_age)
                stats["new"] = len(new_items)
                stats["dupes"] = self._last_ingest.get("dupes", 0)
                stats["merged"] = self._last_ingest.get("merged", 0)
                if new_items:
                    self.emit({"type": "items", "items": [self.to_public(i) for i in new_items]})

                # ---- LLM classification
                stats["ms_ingest"] = int((time.perf_counter() - t_ingest) * 1000)
                if llm_enabled:
                    t_llm = time.perf_counter()
                    if settings.get("llm_classify", "1") == "1":
                        stats["analyzed"] = await self._analyze(llm, names, secs, tickers)
                    if stats["analyzed"]:
                        refreshed = db.list_items(limit=int(settings.get("llm_max_analyze", "45")))
                        self.emit({"type": "items_update",
                                   "items": [self.to_public(i) for i in refreshed]})
                    stats["ms_llm"] = int((time.perf_counter() - t_llm) * 1000)

                # ---- LLM discovery (rotating) + IR feed discovery
                if llm_enabled and settings.get("discovery_enabled", "1") == "1":
                    await self._discover(src, llm, positions, names, secs)

                # ---- deep IR feed discovery for opted-in tickers
                for p in positions:
                    if p.get("deep") and not p.get("ir_feed"):
                        feed = await resolver.discover_ir_feed(src, llm if llm_enabled else None,
                                                               p["ticker"], names.get(p["ticker"]))
                        if feed:
                            db.upsert_position(p["ticker"], kind=p["kind"], ir_feed=feed)

                # ---- LLM second opinion on the technical picture (cached per day)
                if llm_enabled and settings.get("tech_llm", "1") == "1" and self.technicals:
                    stats["tech_reads"] = await self.enrich_technicals(llm, sides)

                # ---- desk brief
                if llm_enabled and settings.get("llm_brief", "1") == "1":
                    await self.write_brief(llm, trigger=trigger)

                await self.refresh_quotes(src, tickers)

            # ---- finish
            db.prune(int(settings.get("max_items_store", "4000")))
            stats["elapsed_ms"] = int((time.perf_counter() - t_start) * 1000)
            u_after = usage_snapshot()
            stats["llm_calls"] = u_after["calls"] - u_before["calls"]
            stats["llm_tokens_in"] = u_after["tokens_in"] - u_before["tokens_in"]
            stats["llm_tokens_out"] = u_after["tokens_out"] - u_before["tokens_out"]
            self.last_stats = stats
            self.last_scan_at = iso()
            db.finish_run(run_id, "ok", stats)
            self.emit({"type": "scan_done", "run_id": run_id, "stats": stats,
                       "at": self.last_scan_at,
                       "counts": db.counts_by_sector(),
                       "topic_counts": db.counts_by_topic(),
                       "db": db.stats()})
            self._progress("idle", "Idle")
        except Exception as exc:  # noqa: BLE001
            stats["error"] = f"{type(exc).__name__}: {exc}"
            db.finish_run(run_id, "error", stats)
            self.emit({"type": "scan_error", "error": stats["error"], "run_id": run_id})
            self.last_stats = stats
            self._progress("idle", "Idle")
        finally:
            self.scanning = False
            self.current_run = None
        return {"ok": True, "run_id": run_id, "stats": self.last_stats}

    # --------------------------------------------------------------- ingest
    async def _ingest(self, raw_items: list[dict], secs: dict[str, dict], names: dict[str, str],
                     tickers: list[str], max_age: float) -> list[dict]:
        settings = db.settings()
        now = now_utc()
        cutoff = now - timedelta(hours=max_age)
        # Process the most specific feeds first so a headline picked up by a ticker
        # or sector query keeps that attribution instead of being classed as "market".
        raw_items = sorted(raw_items, key=lambda r: SCOPE_PRIORITY.get(r.get("scope"), 4))

        # alias map for name matching (longest first)
        aliases: list[tuple[str, str]] = []
        for t, n in names.items():
            base = re.sub(r"\b(inc|corp|corporation|co|plc|ltd|limited|group|holdings|company|"
                          r"the|class [a-z]|common stock)\b\.?", "", (n or t), flags=re.I)
            base = clean_text(base)
            if len(base) >= 4:
                aliases.append((base, t))
            if t not in AMBIGUOUS:
                aliases.append((t, t))
        for t, sec in secs.items():
            for p in (sec.get("peers") or []):
                pn = p.get("name") if isinstance(p, dict) else None
                if pn and len(pn) >= 4:
                    aliases.append((re.sub(r"\b(inc|corp|co|plc|ltd)\b\.?", "", pn, flags=re.I).strip(), p["ticker"]))
                if isinstance(p, dict) and p.get("ticker") and p["ticker"] not in AMBIGUOUS:
                    aliases.append((p["ticker"].upper(), p["ticker"].upper()))
        aliases = sorted({(a.lower(), t.upper()) for a, t in aliases if a},
                         key=lambda x: -len(x[0]))

        recent = db.recent_titles(hours=max_age, limit=4000)
        tok_index: dict[str, list[tuple[int, str, set[str]]]] = {}
        for iid, title, cluster in recent:
            toks = tokens(title)
            for tok in toks:
                tok_index.setdefault(tok, []).append((iid, cluster, toks))

        seen_hashes: set[str] = set()
        seen_titles: set[str] = set()
        new_items: list[dict] = []
        dupes = 0
        merged = 0
        ticker_set = {t.upper() for t in tickers}
        peer_set: set[str] = set()
        for sec in secs.values():
            for p in (sec.get("peers") or []):
                if isinstance(p, dict) and p.get("ticker"):
                    peer_set.add(p["ticker"].upper())

        for raw in raw_items:
            title = clean_text(raw.get("title"))
            if not title or len(title) < 12:
                continue
            pub = raw.get("published")
            if not isinstance(pub, type(now)):
                pub = parse_dt(pub)
            undated = bool(raw.get("undated")) or not pub
            # some sources (SEC filings) carry their own, longer freshness window
            item_max_age = max_age
            if raw.get("max_age_hours"):
                try:
                    item_max_age = max(max_age, float(raw["max_age_hours"]))
                except (TypeError, ValueError):
                    pass
            # undated items have no publish time to check, so they skip the window
            if not undated and (pub < now - timedelta(hours=item_max_age)
                                or pub > now + timedelta(hours=2)):
                continue
            h = item_hash(raw)
            th = title_hash(title)
            if h in seen_hashes:
                continue
            # filings can legitimately share a headline (two 8-Ks on one day)
            if th in seen_titles and not raw.get("unique"):
                continue

            body = f"{title} {raw.get('summary') or ''}"
            # ---- ticker tagging
            found: dict[str, str] = {}
            for m in _CASHTAG_RE.finditer(body):
                sym = m.group(1).upper()
                if sym in ticker_set:
                    found[sym] = "held"
                elif sym in peer_set:
                    found[sym] = "peer"
            hint_ticker = (raw.get("ticker") or "").upper()
            if hint_ticker:
                if hint_ticker in ticker_set:
                    found[hint_ticker] = raw.get("relation") or "held"
                elif hint_ticker in peer_set:
                    found[hint_ticker] = "peer"
            low = body.lower()
            for alias, sym in aliases:
                if len(alias) <= 3:
                    continue
                if re.search(rf"\b{re.escape(alias)}\b", low):
                    rel = "held" if sym in ticker_set else ("peer" if sym in peer_set else "mention")
                    found.setdefault(sym, rel)
            for m in _TICKER_RE.finditer(body):
                sym = m.group(1)
                if sym in AMBIGUOUS:
                    continue
                if sym in ticker_set:
                    found.setdefault(sym, "held")
                elif sym in peer_set:
                    found.setdefault(sym, "peer")

            # ---- sector / topic tagging
            sectors: list[str] = []
            if raw.get("sector_key"):
                sectors.append(raw["sector_key"])
            # the GICS sector of a named tracked ticker is the strongest signal
            for sym in found:
                sk = (secs.get(sym) or {}).get("sector_key")
                if sk and sk not in sectors:
                    sectors.append(sk)
            sector_hits: dict[str, int] = {}
            for s in C.SECTORS:
                hits = sum(1 for kw in s["keywords"] if kw in low)
                if hits:
                    sector_hits[s["key"]] = hits
            # keyword-derived sectors are a fallback/secondary signal
            max_kw = 1 if found else 2
            for k, _ in sorted(sector_hits.items(), key=lambda kv: -kv[1])[:max_kw]:
                if k not in sectors:
                    sectors.append(k)
            sectors = sectors[:3]
            topics: list[str] = []
            if raw.get("topic"):
                topics.append(raw["topic"])
            for t in C.MACRO_TOPICS:
                if any(kw in low for kw in t["keywords"]) and t["key"] not in topics:
                    topics.append(t["key"])
            topics = topics[:4]

            # ---- scope
            if any(v == "held" for v in found.values()):
                scope = "ticker"
            elif any(v == "peer" for v in found.values()):
                scope = "peer"
            elif raw.get("scope") in ("ticker", "peer"):
                scope = raw["scope"] if (sectors or found) else "sector"
            elif sectors and sector_hits:
                scope = "sector"
            elif raw.get("scope") == "macro" or topics:
                scope = "macro"
            else:
                scope = "market"

            # ---- relevance
            score = float(SCOPE_BASE.get(scope, 16))
            ah = age_hours(pub)
            if undated:
                score -= 8
            elif ah < 1:
                score += 12
            elif ah < 3:
                score += 9
            elif ah < 8:
                score += 6
            elif ah < 24:
                score += 2
            elif ah > 36:
                score -= 10
            tier = source_tier(raw.get("source_id", ""))
            score += {1: 9, 2: 4, 3: 0, 4: -6}.get(tier, 0)
            score += min(12, 6 * sum(1 for v in found.values() if v == "held"))
            score += min(6, 1.5 * sum(sector_hits.values()))
            score += min(6, 1.5 * len(topics))
            dt = domain_tier(raw.get("url") or "")
            if dt == 5:
                continue          # content farms / scrapers never enter the feed
            score += {1: 3, 3: -6}.get(dt, 0)
            if EVERGREEN_RE.search(title):
                score -= 9
            if raw.get("source_id", "").startswith("sec"):
                score += 6
            score = max(0.0, min(88.0, score))

            item = {
                "hash": h,
                "cluster_id": h,
                "is_dup": 0,
                "title": title,
                "url": raw.get("url") or "",
                "domain": domain_of(raw.get("url")),
                "source": raw.get("source") or "",
                "source_id": raw.get("source_id") or "",
                "published_at": iso(pub) if pub else None,
                "fetched_at": iso(now),
                "undated": 1 if undated else 0,
                "summary": clean_text(raw.get("summary"), 400),
                "tickers": list(found.keys()),
                "peers": [],
                "sectors": sectors,
                "topics": topics,
                "scope": scope,
                "relevance": round(score, 1),
                "sentiment": None,
                "impact": None,
                "why": None,
                "analyzed": 0,
                "dup_count": 0,
                "source_list": [],
            }

            # ---- near-duplicate clustering against what we already have
            toks = tokens(title)
            cand_counts: dict[int, int] = {}
            meta: dict[int, tuple[str, set[str]]] = {}
            for tok in list(toks)[:12]:
                for iid, cluster, ctoks in tok_index.get(tok, [])[:120]:
                    cand_counts[iid] = cand_counts.get(iid, 0) + 1
                    meta[iid] = (cluster, ctoks)
            best_id, best_j = None, 0.0
            for iid, shared in cand_counts.items():
                if shared < 3:
                    continue
                cluster, ctoks = meta[iid]
                j = jaccard(toks, ctoks)
                if j > best_j:
                    best_id, best_j = iid, j
            if best_id is not None and best_j >= 0.72:
                item["is_dup"] = 1
                item["cluster_id"] = meta[best_id][0]
                seen_hashes.add(h)
                seen_titles.add(th)
                db.insert_item(item)
                db.bump_cluster(meta[best_id][0], item["source"])
                dupes += 1
                continue

            seen_hashes.add(h)
            seen_titles.add(th)
            item_id, inserted = db.insert_item(item)
            if not inserted:
                # same story already stored (often from a broader feed): keep the
                # richer sector/ticker attribution instead of dropping it
                if db.merge_item_tags(h, sectors=sectors, topics=topics,
                                      tickers=list(found.keys()), scope=scope):
                    merged += 1
                    if found and item_id:
                        db.add_links(item_id, [(t, rel) for t, rel in found.items()])
                continue
            item["id"] = item_id
            if found:
                db.add_links(item_id, [(t, rel) for t, rel in found.items()])
            for tok in list(toks)[:12]:
                tok_index.setdefault(tok, []).append((item_id, h, toks))
            new_items.append(item)

        self._last_ingest = {"dupes": dupes, "merged": merged}
        return new_items

    # -------------------------------------------------------------- analysis
    async def _analyze(self, llm: LLM, names: dict[str, str], secs: dict[str, dict],
                       tickers: list[str]) -> int:
        limit = int(db.setting("llm_max_analyze", "45"))
        floor = float(db.setting("llm_min_rule", "30"))
        # items the rule engine already scores as noise never reach the LLM
        pending = db.unanalyzed(limit, min_relevance=floor)
        if not pending:
            return 0
        universe = dict(names)
        for sec in secs.values():
            for p in (sec.get("peers") or []):
                if isinstance(p, dict) and p.get("ticker"):
                    universe.setdefault(p["ticker"].upper(), p.get("name") or p["ticker"])
        payload = [{
            "title": it["title"], "source": it.get("source"), "summary": it.get("summary"),
            "scope": it.get("scope"), "sector_key": (it.get("sectors") or [None])[0],
            "ticker": (it.get("tickers") or [None])[0], "topic": (it.get("topics") or [None])[0],
        } for it in pending]
        total = len(pending)
        done = {"n": 0}

        def _tick(amount: int) -> None:
            done["n"] = min(total, done["n"] + max(0, amount))
            self._progress("llm", f"Classifying headlines ({done['n']}/{total})",
                           done["n"], total)

        self._progress("llm", f"Classifying headlines (0/{total})", 0, total)
        classified = await llm.classify(payload, universe,
                                        concurrency=int(db.setting("llm_concurrency", "4")),
                                        on_done=_tick)
        if not classified:
            return 0
        peer_set = {p["ticker"].upper() for s in secs.values() for p in (s.get("peers") or [])
                    if isinstance(p, dict) and p.get("ticker")}
        ticker_set = {t.upper() for t in tickers}
        updated = 0
        for idx, cls in classified.items():
            if idx >= len(pending):
                continue
            it = pending[idx]
            held = [t for t in cls["tickers"] if t in ticker_set]
            peers = [t for t in cls["tickers"] if t in peer_set and t not in held]
            others = [t for t in cls["tickers"] if t not in ticker_set and t not in peer_set]
            rule_sectors = it.get("sectors") or []
            sectors = list(dict.fromkeys((cls["sectors"] or []) + rule_sectors))
            # the tracked ticker's own GICS sector always stays in the tag list
            for t in held:
                sk = (secs.get(t) or {}).get("sector_key")
                if sk and sk not in sectors:
                    sectors.insert(0, sk)
            sectors = sectors[:3]
            topics = list(dict.fromkeys((it.get("topics") or []) + cls["topics"]))[:4]
            rule_rel = float(it.get("relevance") or 0)
            llm_rel = float(cls["relevance"])
            scope = cls["scope"]
            if held and scope in ("market", "macro", "sector"):
                scope = "ticker"
            elif peers and scope in ("market", "macro"):
                scope = "peer"
            blended = 0.30 * rule_rel + 0.70 * llm_rel + 2.5 * (cls["impact"] - 2.5)
            if scope in ("market", "macro") and llm_rel < 40:
                blended -= 6
            blended = round(max(0.0, min(97.0, blended)), 1)
            db.mark_analyzed(it["id"], {
                "scope": scope,
                "relevance": blended,
                "sentiment": cls["sentiment"],
                "impact": cls["impact"],
                "why": cls["why"],
                "sectors": sectors,
                "topics": topics,
                "tickers": list(dict.fromkeys((it.get("tickers") or []) + held + peers)),
            })
            links = [(t, "held") for t in held] + [(t, "peer") for t in peers] + \
                    [(t, "mention") for t in others]
            if links:
                db.add_links(it["id"], links)
            # propagate to duplicate rows in the same cluster
            updated += 1
        return updated

    # ------------------------------------------------------------- discovery
    async def _discover(self, src: Sources, llm: LLM, positions: list[dict],
                        names: dict[str, str], secs: dict[str, dict]) -> None:
        if not positions:
            return
        settings = db.settings()
        per_scan = max(1, int(settings.get("discovery_per_scan", "3")))
        # prioritise live positions, round-robin so every ticker gets covered
        ordered = [p for p in positions if p.get("kind") == "position"] + \
                  [p for p in positions if p.get("kind") != "position"]
        batch = []
        n = len(ordered)
        for k in range(per_scan):
            batch.append(ordered[(self._discovery_cursor + k) % n])
        self._discovery_cursor = (self._discovery_cursor + per_scan) % n

        results = await asyncio.gather(
            *(llm.discover_ticker_news(p["ticker"], names.get(p["ticker"])) for p in batch),
            return_exceptions=True,
        )
        raw: list[dict] = []
        for r in results:
            if isinstance(r, list):
                raw.extend(r)
        if not raw:
            return
        new_items = await self._ingest(raw, secs, names, list(names.keys()),
                                       float(settings.get("item_max_age_hours", "48")))
        if new_items:
            self.emit({"type": "items", "items": [self.to_public(i) for i in new_items],
                       "channel": "discovery"})

    # ---------------------------------------------------------------- brief
    def hydrate_brief(self) -> None:
        """Reload the newest archived desk note so the panel survives a restart."""
        try:
            row = db.latest_brief()
        except Exception:  # noqa: BLE001  (an old DB without the table must not block boot)
            return
        if not row:
            return
        self.brief = row["text"]
        self.brief_at = row["created_at"]
        self._brief_marker = int(row["item_marker"] or 0)
        meta = row.get("macro")
        if isinstance(meta, dict):
            self._brief_macro = meta.get("sig") or None

    def hydrate_macro(self) -> None:
        """Restore the last macro snapshot so the tape is populated on first paint."""
        if db.setting("macro_enabled", "1") != "1":
            return
        try:
            snap = db.cache_get("macro:snapshot")
        except Exception:  # noqa: BLE001
            return
        if not isinstance(snap, dict) or not snap.get("groups"):
            return
        self.macro = snap
        self.macro_at = snap.get("at")
        # carry the snapshot's real age into the TTL so a stale restore refreshes
        # on the next scan instead of waiting a full cycle
        age = 0.0
        captured = parse_dt(snap.get("at"))
        if captured:
            age = max(0.0, (now_utc() - captured).total_seconds())
        self._macro_cache = (time.time() - age, snap)

    async def write_brief(self, llm: LLM, force: bool = False, trigger: str = "scan") -> str | None:
        """Desk note, regenerated only when there is genuinely new material to summarise.

        The deep model is the most expensive call in the scan, and the note barely
        changes while nothing significant lands — so it is rate-limited by both
        wall-clock time and the number of newly scored, relevant items. A material
        move in the macro tape (yields, curve, VIX, credit, dollar) is itself new
        material, so it can refresh the note even when the headlines are quiet.
        Every note that is actually written is archived in SQLite (`briefs`) before
        it is shown, together with the macro line it was written against.
        """
        settings = db.settings()
        macro_payload = macro.desk_payload(self.macro) if self.macro else None
        macro_sig = macro.signature(self.macro) if self.macro else {}
        if not force and self.brief_at:
            min_min = float(settings.get("llm_brief_min_minutes", "20"))
            min_new = int(settings.get("llm_brief_min_new", "6"))
            age_min = (now_utc() - (parse_dt(self.brief_at) or now_utc())).total_seconds() / 60.0
            if age_min < min_min:
                return self.brief
            enough_new = (self._brief_marker
                          and db.count_new_relevant_since(self._brief_marker) >= min_new)
            tape_moved = (settings.get("macro_brief_trigger", "1") == "1"
                          and bool(self._brief_macro) and bool(macro_sig)
                          and macro.moved(self._brief_macro, macro_sig))
            if not enough_new and not tape_moved:
                return self.brief
        try:
            top = db.list_items(limit=int(settings.get("llm_brief_items", "30")), min_relevance=35)
            positions = db.list_positions()
            sides = {p["ticker"].upper(): p.get("side") for p in positions}
            u_before = usage_snapshot()
            text = await llm.brief(top, db.counts_by_sector(), positions,
                                   technicals=self.technicals or None, sides=sides,
                                   macro=macro_payload)
            u_after = usage_snapshot()
        except Exception:  # noqa: BLE001
            return None
        if text:
            self.brief = text
            self.brief_at = iso()
            self._brief_marker = db.max_item_id()
            macro_meta = None
            if self.macro:
                macro_meta = {"line": self.macro.get("line") or macro.desk_line(self.macro),
                              "sig": macro_sig}
                self._brief_macro = macro_sig
            try:
                db.add_brief(text, model=settings.get("llm_model_deep", ""), trigger=trigger,
                             item_marker=self._brief_marker,
                             tokens_in=u_after["tokens_in"] - u_before["tokens_in"],
                             tokens_out=u_after["tokens_out"] - u_before["tokens_out"],
                             created_at=self.brief_at, macro=macro_meta)
                db.prune_briefs(int(settings.get("llm_brief_keep", "200")))
            except Exception:  # noqa: BLE001  (never lose the note over storage trouble)
                pass
            self.emit({"type": "brief", "text": text, "at": self.brief_at,
                       "count": db.count_briefs(),
                       "macro_line": (macro_meta or {}).get("line")})
        return text

    # ----------------------------------------------------------------- macro
    async def refresh_macro(self, src: Sources, force: bool = False) -> dict:
        """Pull the macro tape (yields, curve, credit, dollar, commodities, calendar)."""
        settings = db.settings()
        if settings.get("macro_enabled", "1") != "1":
            if self.macro:
                # a disabled desk should not keep showing yesterday's tape
                self.macro = {}
                self.macro_at = None
                self._macro_cache = (0.0, None)
                self.emit({"type": "macro", "macro": None, "at": None})
            return {"macro": 0}
        ttl = max(60.0, float(settings.get("macro_refresh_minutes", "15")) * 60)
        hit = self._macro_cache
        if hit and not force and hit[1] and time.time() - hit[0] <= ttl:
            return {"macro": sum(len(g.get("metrics") or []) for g in hit[1].get("groups") or [])}
        if self.macro_scanning:
            return {"macro": sum(len(g.get("metrics") or []) for g in self.macro.get("groups") or [])}
        self.macro_scanning = True
        try:
            snap = await macro.fetch(src, calendar=settings.get("macro_calendar", "1") == "1",
                                     force=force)
        except Exception as exc:  # noqa: BLE001  (a macro failure never aborts a scan)
            err = f"{type(exc).__name__}: {exc}"[:180]
            db.record_health("macro:feed", "Macro · data desk", False, 0, 0, err)
            self.source_errors.append({"source": "macro", "error": err[:120]})
            return {"macro": 0, "macro_error": err}
        finally:
            self.macro_scanning = False
        for s in snap.get("sources", []):
            db.record_health(s["id"], s["name"], bool(s.get("ok")), int(s.get("ms") or 0),
                             int(s.get("items") or 0), s.get("error"))
        self.macro = snap
        self.macro_at = snap.get("at") or iso()
        self._macro_cache = (time.time(), snap)
        try:
            db.cache_put("macro:snapshot", snap, "macro")
        except Exception:  # noqa: BLE001  (cache is an optimisation, not the source of truth)
            pass
        self.emit({"type": "macro", "macro": snap, "at": self.macro_at})
        return {"macro": sum(len(g.get("metrics") or []) for g in snap.get("groups") or [])}

    # ---------------------------------------------------------------- quotes
    async def refresh_quotes(self, src: Sources, tickers: list[str]) -> dict:
        etfs = [s["etf"] for s in C.SECTORS if s.get("etf")] + ["SPY", "QQQ"]
        jobs = [(t, "stocks") for t in tickers] + [(e, "etf") for e in etfs]
        out = await asyncio.gather(*(src.quote(t, ac) for t, ac in jobs),
                                   return_exceptions=True)
        for (t, ac), q in zip(jobs, out):
            if not isinstance(q, dict):
                continue
            if ac == "etf":
                self.etf_quotes[t] = {**q, "ticker": t}
            else:
                self.quotes[t] = q
        self.quotes_at = iso()
        self.emit({"type": "quotes", "quotes": self.quotes, "etfs": self.etf_quotes,
                   "at": self.quotes_at})
        return self.quotes

    # ------------------------------------------------------------- technicals
    async def _bars_for(self, src: Sources, ticker: str, bars_n: int, ttl: float,
                        force: bool) -> list[dict] | None:
        """Daily bars with an in-memory TTL cache; stale cache beats no cache."""
        hit = self._bar_cache.get(ticker)
        now = time.time()
        if hit and not force and now - hit[0] <= ttl:
            return hit[1]
        t0 = time.perf_counter()
        try:
            info = await src.stockanalysis_history(ticker, bars=bars_n)
            self._bar_cache[ticker] = (time.time(), info["bars"])
            db.record_health("sa:history", "StockAnalysis · daily bars", True,
                             int((time.perf_counter() - t0) * 1000), len(info["bars"]))
            return info["bars"]
        except Exception as exc:  # noqa: BLE001
            db.record_health("sa:history", "StockAnalysis · daily bars", False,
                             int((time.perf_counter() - t0) * 1000), 0, str(exc)[:180])
            self.source_errors.append({"source": f"tech:{ticker}", "error": str(exc)[:120]})
            return hit[1] if hit else None

    async def refresh_technicals(self, src: Sources, tickers: list[str],
                                 sides: dict[str, str | None],
                                 force: bool = False) -> dict:
        """Price-action pass over every name in positions/watchlist."""
        settings = db.settings()
        if settings.get("tech_enabled", "1") != "1":
            return {"tech": 0}
        tickers = [t.upper() for t in dict.fromkeys(tickers) if t]
        if not tickers or self.tech_scanning:
            return {"tech": 0}
        self.tech_scanning = True
        try:
            return await self._refresh_technicals(src, tickers, sides, force)
        finally:
            self.tech_scanning = False

    async def _refresh_technicals(self, src: Sources, tickers: list[str],
                                  sides: dict[str, str | None], force: bool) -> dict:
        settings = db.settings()
        if settings.get("tech_enabled", "1") != "1":
            return {"tech": 0}
        bars_n = max(60, int(settings.get("tech_bars", "260")))
        ttl = max(60.0, float(settings.get("tech_refresh_minutes", "30")) * 60)
        benchmark = (settings.get("tech_benchmark", "SPY") or "SPY").upper()
        self._progress("technical", f"Reading price action ({len(tickers)} names)", 0,
                       len(tickers))
        t0 = time.perf_counter()
        sem = asyncio.Semaphore(6)

        async def load(t: str) -> tuple[str, list[dict] | None]:
            async with sem:
                return t, await self._bars_for(src, t, bars_n, ttl, force)

        wanted = list(dict.fromkeys(tickers + ([benchmark] if benchmark else [])))
        rows = await asyncio.gather(*(load(t) for t in wanted))
        bar_map = {t: bars for t, bars in rows if bars}
        bench_bars = bar_map.get(benchmark) if benchmark else None
        snaps: dict[str, dict] = {}
        done = 0
        for t in tickers:
            bars = bar_map.get(t)
            quote = self.quotes.get(t)
            name = (db.get_security(t) or {}).get("name") or (quote or {}).get("name")
            if bars:
                snap = technical.analyse(
                    t, bars, quote=quote, benchmark_bars=bench_bars,
                    benchmark=benchmark, name=name,
                )
                db.upsert_technical(t, snap)
            else:
                # keep the last good read rather than blanking the panel on a hiccup
                snap = db.get_technical(t) or technical.analyse(t, [], quote=quote, name=name)
            snaps[t] = snap
            done += 1
            if done % 8 == 0 or done == len(tickers):
                self._progress("technical", f"Reading price action ({done}/{len(tickers)})",
                               done, len(tickers))
        try:
            db.prune_technicals(tickers)
        except Exception:  # noqa: BLE001
            pass
        self.technicals = snaps
        self.publish_technicals(sides)
        ok = sum(1 for s in snaps.values() if not s.get("error"))
        return {"tech": ok, "tech_errors": len(snaps) - ok,
                "ms_tech": int((time.perf_counter() - t0) * 1000)}

    def publish_technicals(self, sides: dict[str, str | None]) -> dict:
        """Recompute the breadth summary and push the light snapshots to clients."""
        self.tech_at = iso()
        kinds = {p["ticker"].upper(): p.get("kind") for p in db.list_positions()}
        self.tech_summary = technical.book_summary(self.technicals, sides, kinds)
        self.emit({
            "type": "technicals", "at": self.tech_at,
            "summaries": {t: technical.light(s) for t, s in self.technicals.items()},
            "summary": self.tech_summary,
        })
        return self.tech_summary

    async def enrich_technicals(self, llm: LLM,
                                sides: dict[str, str | None]) -> int:
        """Add the cached LLM second opinion; never blocks the computed read."""
        snaps = [s for s in self.technicals.values() if not s.get("error")]
        if not snaps or not llm.enabled:
            return 0
        reads = await llm.tech_read(snaps)
        if not reads:
            return 0
        for ticker, text in reads.items():
            snap = self.technicals.get(ticker)
            if not snap:
                continue
            technical.apply_read(snap, text)
            try:
                db.upsert_technical(ticker, snap)
            except Exception:  # noqa: BLE001
                pass
        self.publish_technicals(sides)
        return len(reads)

    def hydrate_technicals(self) -> None:
        """Restore the last snapshots so the technical grid survives a restart."""
        try:
            rows = db.list_technicals()
        except Exception:  # noqa: BLE001  (old DB without the table must not block boot)
            return
        if not rows:
            return
        self.technicals = {s["ticker"]: s for s in rows}
        self.tech_at = max((s.get("asof") or "" for s in rows), default=None)
        positions = db.list_positions()
        sides = {p["ticker"].upper(): p.get("side") for p in positions}
        kinds = {p["ticker"].upper(): p.get("kind") for p in positions}
        self.tech_summary = technical.book_summary(self.technicals, sides, kinds)

    # -------------------------------------------------------------- public io
    def to_public(self, item: dict) -> dict:
        return {
            "id": item.get("id"),
            "title": item.get("title"),
            "url": item.get("url"),
            "domain": item.get("domain"),
            "source": item.get("source"),
            "source_id": item.get("source_id"),
            "published_at": item.get("published_at"),
            "fetched_at": item.get("fetched_at"),
            "undated": bool(item.get("undated")),
            "summary": item.get("summary"),
            "tickers": item.get("tickers") or [],
            "sectors": item.get("sectors") or [],
            "topics": item.get("topics") or [],
            "scope": item.get("scope"),
            "relevance": item.get("relevance"),
            "sentiment": item.get("sentiment"),
            "impact": item.get("impact"),
            "why": item.get("why"),
            "dup_count": item.get("dup_count") or 0,
            "source_list": item.get("source_list") or [],
            "analyzed": bool(item.get("analyzed")),
        }

    # ------------------------------------------------------------- scheduler
    async def scheduler_loop(self) -> None:
        while True:
            try:
                settings = db.settings()
                interval = max(30, int(settings.get("scan_interval_seconds", "180")))
                auto = settings.get("auto_refresh", "1") in ("1", "true", "True")
                await asyncio.sleep(interval)
                if auto and not self.scanning:
                    started = time.perf_counter()
                    await self.scan(trigger="auto")
                    # keep a steady cadence regardless of how long the scan took
                    remaining = interval - (time.perf_counter() - started)
                    if remaining > 5:
                        await asyncio.sleep(remaining)
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                await asyncio.sleep(30)

    def start_scheduler(self) -> None:
        if not self._scheduler_task or self._scheduler_task.done():
            self._scheduler_task = asyncio.create_task(self.scheduler_loop())

    def stop_scheduler(self) -> None:
        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()


scanner = Scanner()
