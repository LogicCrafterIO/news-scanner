"""GICS industry mapping + peer discovery.

Layer 1  Wikipedia "List of S&P 500/400/600 companies" -> ~1,500 tickers with
         GICS Sector + GICS Sub-Industry + CIK (covers most liquid US names).
Layer 2  Nasdaq company profile -> sector / industry for other US listings.
Layer 3  SEC company_tickers + SIC code -> GICS super-sector.
Layer 4  Ollama (deep model + web search) -> mapping for anything else (e.g. non-US).
Peers    Same GICS sub-industry from the index, else LLM-suggested competitors.
IR RSS   Web-search found investor-relations page -> feed autodiscovery.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

import feedparser
import httpx

from . import config as C
from .db import db
from .util import clean_text, iso, norm_ticker, parse_dt, now_utc, ticker_variants

WIKI_PAGES = [
    ("List of S&P 500 companies", "S&P 500"),
    ("List of S&P 400 companies", "S&P 400"),
    ("List of S&P 600 companies", "S&P 600"),
]
CACHE_FILE = C.CACHE_DIR / "gics_index.json"
SEC_TICKERS_CACHE = C.CACHE_DIR / "sec_tickers.json"
INDEX_TTL_HOURS = 24 * 7
SEC_MAP_TTL_HOURS = 24

NASDAQ_SECTOR_MAP = {
    "technology": "information_technology",
    "health care": "health_care",
    "finance": "financials",
    "consumer discretionary": "consumer_discretionary",
    "consumer staples": "consumer_staples",
    "industrials": "industrials",
    "energy": "energy",
    "utilities": "utilities",
    "real estate": "real_estate",
    "telecommunications": "communication_services",
    "basic materials": "materials",
    "miscellaneous": None,
    "other": None,
}

# SIC ranges / specific codes -> GICS super-sector key
SIC_SECTORS: list[tuple[int, int, str]] = [
    (100, 999, "consumer_staples"),      # agriculture
    (1000, 1099, "materials"),           # metal mining
    (1220, 1299, "energy"),              # coal
    (1311, 1389, "energy"),              # oil & gas
    (1400, 1499, "materials"),           # nonmetallic minerals
    (1500, 1799, "industrials"),         # construction
    (2000, 2199, "consumer_staples"),    # food, tobacco, beverage
    (2200, 2399, "consumer_discretionary"),
    (2400, 2499, "materials"),
    (2500, 2599, "consumer_discretionary"),
    (2600, 2699, "materials"),
    (2700, 2799, "communication_services"),
    (2800, 2829, "materials"),           # industrial chemicals
    (2830, 2836, "health_care"),         # drugs
    (2840, 2899, "materials"),
    (2900, 2999, "energy"),              # petroleum refining
    (3000, 3099, "materials"),
    (3100, 3199, "consumer_discretionary"),
    (3200, 3299, "materials"),
    (3300, 3399, "materials"),
    (3400, 3499, "industrials"),
    (3500, 3569, "industrials"),
    (3570, 3579, "information_technology"),   # computers
    (3580, 3599, "industrials"),
    (3600, 3699, "information_technology"),   # electronics
    (3700, 3716, "consumer_discretionary"),    # autos
    (3720, 3799, "industrials"),               # aerospace
    (3800, 3899, "health_care"),               # instruments
    (3900, 3999, "consumer_discretionary"),
    (4000, 4499, "industrials"),
    (4500, 4599, "industrials"),
    (4600, 4699, "energy"),                    # pipelines
    (4700, 4799, "industrials"),
    (4800, 4899, "communication_services"),
    (4900, 4949, "utilities"),
    (4950, 4959, "industrials"),               # waste
    (5000, 5199, "industrials"),
    (5200, 5399, "consumer_staples"),
    (5400, 5799, "consumer_discretionary"),
    (5800, 5999, "consumer_discretionary"),
    (6000, 6299, "financials"),
    (6300, 6411, "financials"),
    (6500, 6599, "real_estate"),
    (6600, 6799, "financials"),
    (7000, 7099, "consumer_discretionary"),
    (7200, 7379, "industrials"),
    (7370, 7379, "information_technology"),
    (7380, 7999, "industrials"),
    (8000, 8099, "health_care"),
    (8100, 8999, "industrials"),
]
SIC_OVERRIDES = {
    1311: "energy", 1381: "energy", 1389: "energy", 2911: "energy",
    2834: "health_care", 2835: "health_care", 2836: "health_care",
    3674: "information_technology", 3672: "information_technology",
    7372: "information_technology", 7374: "information_technology",
    6021: "financials", 6022: "financials", 6311: "financials", 6411: "financials",
    4813: "communication_services", 4833: "communication_services",
    4911: "utilities", 4931: "utilities",
    6500: "real_estate", 6798: "real_estate",
    5411: "consumer_staples", 5122: "health_care",
}


def sic_to_sector(sic: Any, desc: str = "") -> str | None:
    try:
        code = int(str(sic).strip())
    except (TypeError, ValueError):
        return None
    if code in SIC_OVERRIDES:
        return SIC_OVERRIDES[code]
    d = (desc or "").lower()
    for lo, hi, key in SIC_SECTORS:
        if lo <= code <= hi:
            return key
    if "bank" in d or "insur" in d or "financial" in d:
        return "financials"
    if "semi" in d or "software" in d or "computer" in d:
        return "information_technology"
    if "drug" in d or "medical" in d:
        return "health_care"
    if "oil" in d or "gas" in d:
        return "energy"
    if "gold" in d or "mining" in d or "chemical" in d:
        return "materials"
    if "electric" in d or "utility" in d:
        return "utilities"
    return None


# --------------------------------------------------------------------------- #
# Wikipedia table parsing
# --------------------------------------------------------------------------- #
_ATTR_PREFIX = re.compile(r'^\s*[a-zA-Z:\-]+\s*=\s*"[^"]*"\s*\|', re.I)
_TEMPLATE = re.compile(r"\{\{(?:[^{}|]*\|)?([A-Z][A-Z0-9.\-]{0,6})\}\}")
_LINK = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
_REF = re.compile(r"<ref[^>]*>.*?</ref>|<ref[^>]*/>", re.S)
_TAGS = re.compile(r"<[^>]+>")

GICS_NAMES_LOWER = {
    "information technology": "Information Technology",
    "health care": "Health Care",
    "financials": "Financials",
    "consumer discretionary": "Consumer Discretionary",
    "consumer staples": "Consumer Staples",
    "energy": "Energy",
    "industrials": "Industrials",
    "materials": "Materials",
    "utilities": "Utilities",
    "real estate": "Real Estate",
    "communication services": "Communication Services",
}


def _clean_wiki(cell: str) -> str:
    c = _REF.sub("", cell)
    c = _TAGS.sub("", c)
    c = _LINK.sub(lambda m: (m.group(2) or m.group(1)).strip(), c)
    c = re.sub(r"\{\{[^{}]*\}\}", "", c)
    c = c.replace("'''", "").replace("''", "")
    c = c.replace("&nbsp;", " ").replace("&amp;", "&")
    return clean_text(c).strip(" |!")


def _cells(row: str) -> list[str]:
    """Split a wikitext table row into raw cell strings.

    Handles both `|| cell` (newline separated) and `| a || b` (inline) layouts and
    strips HTML attribute prefixes such as `style="..." |`.
    """
    normalised = row.replace("!!", "\n|").replace("||", "\n|")
    out: list[str] = []
    for line in normalised.split("\n"):
        line = line.strip()
        if not line.startswith("|"):
            continue
        cell = line[1:].strip()
        prev = None
        while prev != cell:  # strip stacked attribute prefixes
            prev = cell
            cell = _ATTR_PREFIX.sub("", cell).strip()
        out.append(cell)
    return out


def _row_cells(row: str) -> list[str]:
    return [_clean_wiki(c) for c in _cells(row)]


def _ticker_from_cell(cell: str) -> str:
    m = _TEMPLATE.search(cell)
    if m:
        return m.group(1).upper()
    plain = _clean_wiki(cell)
    m2 = re.match(r"^([A-Z][A-Z0-9.\-]{0,6})$", plain.strip())
    return m2.group(1).upper() if m2 else ""


def parse_wiki_table(wikitext: str, index_name: str) -> list[dict]:
    """Parse the `id="constituents"` table into ticker/GICS rows."""
    m = re.search(r'\{\|[^\n]*id="constituents".*?\n\|\}', wikitext, re.S)
    block = m.group(0) if m else wikitext
    block = re.sub(r"^\{\|[^\n]*\n", "", block)
    block = re.sub(r"\n\|\}\s*$", "", block)

    parts = re.split(r"\n\|-+[^\n]*\n", block, maxsplit=1)
    header = parts[0] if parts else ""
    body = parts[1] if len(parts) > 1 else ""

    # ---- header: one cell per `!`-line, or several per line split by `!!`
    header_cells: list[str] = []
    for line in header.splitlines():
        line = line.strip()
        if not line.startswith("!"):
            continue
        for chunk in line[1:].split("!!"):
            chunk = _ATTR_PREFIX.sub("", chunk.strip()).strip()
            header_cells.append(_clean_wiki(chunk))

    header_idx: dict[str, int] = {}
    for i, cell in enumerate(header_cells):
        k = cell.lower()
        if "sub-industry" in k or "sub industry" in k:
            header_idx.setdefault("sub_industry", i)
        elif "sector" in k or k.startswith("gics"):
            header_idx.setdefault("sector", i)
        elif "symbol" in k or "ticker" in k:
            header_idx.setdefault("ticker", i)
        elif "security" in k or "company" in k:
            header_idx.setdefault("name", i)
        elif k in ("cik", "central index key") or "central index" in k:
            header_idx.setdefault("cik", i)

    ti = header_idx.get("ticker", 0)
    ni = header_idx.get("name", 1)
    si = header_idx.get("sector", 2)
    ui = header_idx.get("sub_industry", 3)
    ci = header_idx.get("cik")

    out: list[dict] = []
    for row in re.split(r"\n\|-+[^\n]*\n", body):
        raw_cells = _cells(row)
        if len(raw_cells) < 3:
            continue
        ticker = _ticker_from_cell(raw_cells[ti]) if ti < len(raw_cells) else ""
        if not ticker or not re.match(r"^[A-Z][A-Z0-9.\-]{0,6}$", ticker):
            continue
        cells = [_clean_wiki(c) for c in raw_cells]
        name = cells[ni] if ni < len(cells) else ticker
        sector = cells[si] if si < len(cells) else ""
        sub = cells[ui] if ui < len(cells) else ""

        # fallback: find the GICS sector name anywhere in the row (layout drift)
        if sector.lower() not in GICS_NAMES_LOWER:
            for i, c in enumerate(cells):
                if c.lower() in GICS_NAMES_LOWER:
                    sector = c
                    sub = cells[i + 1] if i + 1 < len(cells) else sub
                    break
        sector = GICS_NAMES_LOWER.get(sector.lower(), sector)
        if sector.lower() not in {v.lower() for v in GICS_NAMES_LOWER.values()}:
            continue

        cik = ""
        if ci is not None and ci < len(cells):
            mm = re.search(r"\d{6,10}", cells[ci])
            cik = mm.group(0) if mm else ""
        out.append({
            "ticker": ticker, "name": name or ticker, "sector": sector,
            "sub_industry": sub or "Unclassified", "cik": cik, "index_name": index_name,
        })
    return out


class IndustryResolver:
    """Resolves tickers -> GICS sector / sub-industry / peers, with layered fallbacks."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.last_build: str | None = None

    # ----------------------------------------------------------- index build
    async def ensure_index(self, sources: Any, force: bool = False) -> int:
        async with self._lock:
            count = db.gics_count()
            if count and not force:
                meta = db.cache_get("gics_index_meta")
                if meta and meta.get("built_at"):
                    built = parse_dt(meta["built_at"])
                    age_h = (now_utc() - built).total_seconds() / 3600 if built else 999
                    if age_h < INDEX_TTL_HOURS:
                        return count
            if CACHE_FILE.exists() and not force:
                try:
                    rows = json.loads(CACHE_FILE.read_text())
                    if rows:
                        n = db.replace_gics_index(rows)
                        db.cache_put("gics_index_meta", {"built_at": iso(), "rows": n, "from": "file"})
                        return n
                except (json.JSONDecodeError, OSError):
                    pass
            rows: dict[str, dict] = {}
            for page, index_name in WIKI_PAGES:
                try:
                    resp = await sources._get(
                        "https://en.wikipedia.org/w/api.php",
                        headers={"User-Agent": C.WIKI_UA},
                        params={"action": "parse", "page": page, "prop": "wikitext",
                                "format": "json", "formatversion": "2", "redirects": "1"},
                    )
                    wikitext = (resp.json().get("parse") or {}).get("wikitext") or ""
                    parsed = parse_wiki_table(wikitext, index_name)
                    for r in parsed:
                        r["sector"] = self._normalise_sector_name(r["sector"])
                        if r["ticker"] in rows and rows[r["ticker"]]["index_name"] == "S&P 500":
                            continue
                        rows[r["ticker"]] = r
                except (httpx.HTTPError, ValueError, KeyError):
                    continue
            data = list(rows.values())
            if data:
                db.replace_gics_index(data)
                try:
                    CACHE_FILE.write_text(json.dumps(data), encoding="utf-8")
                except OSError:
                    pass
                db.cache_put("gics_index_meta", {"built_at": iso(), "rows": len(data), "from": "wiki"})
            self.last_build = iso()
            return len(data)

    @staticmethod
    def _normalise_sector_name(name: str) -> str:
        n = clean_text(name).lower()
        table = {
            "information technology": "Information Technology",
            "tech": "Information Technology",
            "health care": "Health Care",
            "healthcare": "Health Care",
            "financials": "Financials",
            "finance": "Financials",
            "consumer discretionary": "Consumer Discretionary",
            "consumer staples": "Consumer Staples",
            "energy": "Energy",
            "industrials": "Industrials",
            "materials": "Materials",
            "utilities": "Utilities",
            "real estate": "Real Estate",
            "communication services": "Communication Services",
        }
        return table.get(n, clean_text(name))

    # ------------------------------------------------------------- sec lookup
    async def sec_tickers(self, sources: Any) -> dict[str, dict]:
        cached = db.cache_get("sec_ticker_map")
        if cached:
            return cached
        if SEC_TICKERS_CACHE.exists():
            if time.time() - SEC_TICKERS_CACHE.stat().st_mtime < SEC_MAP_TTL_HOURS * 3600:
                try:
                    data = json.loads(SEC_TICKERS_CACHE.read_text())
                    if data:
                        db.cache_put("sec_ticker_map", data, "sec")
                        return data
                except (json.JSONDecodeError, OSError):
                    pass
        try:
            resp = await sources._get("https://www.sec.gov/files/company_tickers.json",
                                      headers={"User-Agent": C.SEC_UA})
            raw = resp.json()
            data: dict[str, dict] = {}
            for v in raw.values():
                try:
                    entry = {"cik": int(v["cik_str"]), "name": v["title"]}
                except (KeyError, TypeError, ValueError):
                    continue
                for variant in ticker_variants(str(v.get("ticker", ""))):
                    data.setdefault(variant, entry)
            db.cache_put("sec_ticker_map", data, "sec")
            try:
                SEC_TICKERS_CACHE.write_text(json.dumps(data), encoding="utf-8")
            except OSError:
                pass
            return data
        except (httpx.HTTPError, ValueError, KeyError, AttributeError):
            return {}

    async def sec_company(self, sources: Any, cik: str) -> dict | None:
        cik10 = str(cik).lstrip("0").zfill(10)
        key = f"sec_co:{cik10}"
        cached = db.cache_get(key)
        if cached:
            return cached
        try:
            resp = await sources._get(f"https://data.sec.gov/submissions/CIK{cik10}.json",
                                      headers={"User-Agent": C.SEC_UA})
            j = resp.json()
            out = {"name": j.get("name"), "sic": j.get("sic"),
                   "sicDescription": j.get("sicDescription"),
                   "tickers": j.get("tickers") or [],
                   "exchanges": j.get("exchanges") or []}
            db.cache_put(key, out, "sec")
            return out
        except (httpx.HTTPError, ValueError):
            return None

    # --------------------------------------------------------------- resolve
    async def resolve(self, sources: Any, llm: Any, ticker: str, name: str | None = None,
                      force: bool = False) -> dict:
        ticker = norm_ticker(ticker)
        variants = ticker_variants(ticker)
        existing = db.get_security(ticker)
        if existing and not force:
            updated = parse_dt(existing.get("updated_at"))
            fresh = updated and (now_utc() - updated).total_seconds() < 72 * 3600
            if fresh and existing.get("sector_key"):
                return existing

        sec: dict[str, Any] = {
            "ticker": ticker, "name": name, "cik": None, "exchange": None,
            "sector": None, "sub_industry": None, "sector_key": None,
            "source": "unresolved", "confidence": 0.0, "peers": [],
        }

        # 1) Wikipedia GICS index (best quality)
        wiki = next((w for w in (db.gics_get(v) for v in variants) if w), None)
        if wiki:
            key = C.SECTOR_NAME_TO_KEY.get(wiki["sector"])
            if key:
                sec.update({
                    "name": sec["name"] or wiki["name"], "sector": wiki["sector"],
                    "sector_key": key, "sub_industry": wiki["sub_industry"],
                    "cik": wiki.get("cik") or None, "source": "wiki:" + str(wiki.get("index_name")),
                    "confidence": 0.95,
                })

        # 2) SEC ticker map (name + CIK for anything US-listed/filing)
        sec_map = await self.sec_tickers(sources)
        entry = next((sec_map[v] for v in variants if v in sec_map), None)
        if entry:
            sec["cik"] = sec["cik"] or str(entry["cik"])
            sec["name"] = sec["name"] or entry.get("name")
            if not sec["sector_key"]:
                company = await self.sec_company(sources, str(entry["cik"]))
                if company:
                    ex = company.get("exchanges") or []
                    if ex and not sec["exchange"]:
                        sec["exchange"] = ex[0]
                    key = sic_to_sector(company.get("sic"), company.get("sicDescription") or "")
                    if key:
                        sec.update({
                            "sector_key": key, "sector": C.SECTOR_BY_KEY[key]["name"],
                            "sub_industry": company.get("sicDescription") or "Unclassified",
                            "source": "sec-sic", "confidence": 0.6,
                        })

        # 3) Nasdaq profile (sector/industry text)
        if not sec["sector_key"] or not sec["sub_industry"] or not sec["name"]:
            profile = None
            for v in variants:
                profile = await sources.nasdaq_profile(v)
                if profile and profile.get("name"):
                    break
            if profile:
                sec["name"] = sec["name"] or profile.get("name")
                ns = (profile.get("sector") or "").strip().lower()
                mapped = NASDAQ_SECTOR_MAP.get(ns)
                if mapped and not sec["sector_key"]:
                    sec.update({
                        "sector_key": mapped, "sector": C.SECTOR_BY_KEY[mapped]["name"],
                        "source": "nasdaq", "confidence": 0.65,
                    })
                if profile.get("industry") and (
                    not sec["sub_industry"] or sec["sub_industry"] == "Unclassified"
                ):
                    sec["sub_industry"] = clean_text(profile["industry"], 60)

        # 4) LLM fallback (works for non-US / obscure names); also refines sub-industry
        if llm and getattr(llm, "key", None) and (
            not sec["sector_key"] or (sec["confidence"] < 0.7 and not wiki)
        ):
            hints = ""
            if sec["name"]:
                hints += f"Company name hint: {sec['name']}\n"
            if sec["sub_industry"]:
                hints += f"Registry industry hint: {sec['sub_industry']}\n"
            mapped = await llm.map_industry(ticker, sec["name"], hints)
            if mapped:
                if not sec["sector_key"]:
                    sec.update({
                        "sector_key": mapped["sector"], "sector": mapped["sector_name"],
                        "sub_industry": mapped["sub_industry"], "source": "llm",
                        "confidence": float(mapped.get("confidence") or 0.55),
                    })
                elif sec["confidence"] < 0.7:
                    # SIC/Nasdaq taxonomies are coarser than GICS: trust the LLM's
                    # finer classification when the registry source is weak.
                    sec.update({
                        "sector_key": mapped["sector"], "sector": mapped["sector_name"],
                        "sub_industry": mapped.get("sub_industry") or sec["sub_industry"],
                        "source": "llm-refined", "confidence": 0.8,
                    })
                elif mapped.get("sub_industry") and sec["sub_industry"] == "Unclassified":
                    sec["sub_industry"] = mapped["sub_industry"]
                if mapped.get("peers"):
                    sec["peers"] = [{"ticker": p, "name": ""} for p in mapped["peers"]]

        # ---- peers: same GICS sub-industry, largest index members first
        peer_limit = max(1, int(db.setting("peer_limit", "6")))
        peer_rows = db.gics_peers(sec["sector"], sec["sub_industry"],
                                  exclude=[ticker], limit=peer_limit)
        peers = [{"ticker": r["ticker"], "name": r["name"],
                  "sub_industry": r["sub_industry"]} for r in peer_rows]
        # thin sub-industry (e.g. Wireless Telecom) -> top up with LLM-known competitors
        if len(peers) < 3 and llm is not None and getattr(llm, "key", None):
            mapped = await llm.map_industry(
                ticker, sec["name"], f"GICS sub-industry: {sec['sub_industry']}"
            )
            if mapped and mapped.get("peers"):
                seen = {p["ticker"] for p in peers}
                for p in mapped["peers"]:
                    if p in seen:
                        continue
                    g = db.gics_get(p) or {}
                    peers.append({"ticker": p, "name": g.get("name") or "",
                                  "sub_industry": g.get("sub_industry") or sec["sub_industry"]})
                    seen.add(p)
        if not peers:
            known = await self.sec_tickers(sources)
            peers = [
                {"ticker": p["ticker"],
                 "name": p.get("name") or (known.get(p["ticker"], {}) or {}).get("name", "")}
                for p in sec.get("peers", []) if p.get("ticker")
            ]
        sec["peers"] = peers[:peer_limit]

        db.upsert_security(sec)
        return sec

    async def resolve_many(self, sources: Any, llm: Any, tickers: list[str],
                           force: bool = False) -> dict[str, dict]:
        sem = asyncio.Semaphore(4)

        async def one(t: str) -> tuple[str, dict]:
            async with sem:
                try:
                    return t, await self.resolve(sources, llm, t, force=force)
                except Exception as exc:  # noqa: BLE001
                    return t, {"ticker": t, "sector": None, "sector_key": None,
                               "sub_industry": None, "peers": [], "source": "error",
                               "error": str(exc)[:120]}

        results = await asyncio.gather(*(one(t) for t in tickers), return_exceptions=True)
        out: dict[str, dict] = {}
        for r in results:
            if isinstance(r, tuple):
                out[r[0]] = r[1]
        return out

    # ---------------------------------------------------------- IR discovery
    async def discover_ir_feed(self, sources: Any, llm: Any, ticker: str,
                               name: str | None) -> str | None:
        if not llm or not getattr(llm, "key", None):
            return None
        cache_key = f"irfeed:{ticker.upper()}"
        cached = db.cache_get(cache_key)
        if cached and cached.get("feed_url"):
            return cached["feed_url"]
        info = await llm.discover_ir_feed(ticker, name)
        pages = list(info.get("candidates") or []) if info else []
        feed_url = None

        # probe conventional paths on each candidate origin, then scrape pages
        origins: list[str] = []
        for p in pages[:4]:
            try:
                from urllib.parse import urlparse
                pr = urlparse(p)
                if pr.scheme and pr.netloc:
                    origins.append(f"{pr.scheme}://{pr.netloc}")
            except ValueError:
                continue
        tried: set[str] = set()
        for origin in dict.fromkeys(origins):
            for path in C.IR_FEED_PATHS:
                url = origin.rstrip("/") + path
                if url in tried:
                    continue
                tried.add(url)
                if await self._is_feed(sources, url):
                    feed_url = url
                    break
            if feed_url:
                break
        if not feed_url:
            for page in pages[:4]:
                links = await self._feed_links_on_page(sources, page)
                for link in links:
                    if await self._is_feed(sources, link):
                        feed_url = link
                        break
                if feed_url:
                    break
        db.cache_put(cache_key, {"feed_url": feed_url, "checked": iso(), "pages": pages[:5]}, "ir")
        return feed_url

    @staticmethod
    async def _is_feed(sources: Any, url: str) -> bool:
        try:
            resp = await sources._get(url, retries=0)
            ct = resp.headers.get("content-type", "")
            if "xml" not in ct and "rss" not in ct and "atom" not in ct:
                head = resp.text[:400].lower()
                if "<rss" not in head and "<feed" not in head:
                    return False
            parsed = await asyncio.to_thread(feedparser.parse, resp.content)
            return bool(parsed.entries) and not parsed.bozo
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    async def _feed_links_on_page(sources: Any, page: str) -> list[str]:
        from urllib.parse import urljoin
        try:
            resp = await sources._get(page, retries=0)
        except Exception:  # noqa: BLE001
            return []
        html = resp.text
        links: list[str] = []
        for m in re.finditer(
            r'<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]*>', html, re.I
        ):
            href = re.search(r'href=["\']([^"\']+)', m.group(0), re.I)
            if href:
                links.append(urljoin(page, href.group(1)))
        for m in re.finditer(r'href=["\']([^"\']*(?:rss|feed)[^"\']*)["\']', html, re.I):
            href = m.group(1)
            if any(x in href.lower() for x in ("rss", "feed", ".xml")):
                links.append(urljoin(page, href))
        return list(dict.fromkeys(links))[:12]


resolver = IndustryResolver()
