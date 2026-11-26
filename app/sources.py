"""Source fetchers: Google News, direct RSS, Nasdaq, SEC EDGAR, StockTwits, IR feeds."""
from __future__ import annotations

import asyncio
import csv
import io
import json
import time
from typing import Any, Iterable
from urllib.parse import quote_plus, urlparse

import feedparser
import httpx

from . import config as C
from .util import canonical_url, clean_text, domain_of, iso, now_utc, parse_ago, parse_dt

GN_BASE = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
SA_BASE = "https://stockanalysis.com/api"
TREASURY_BASE = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
                 "daily-treasury-rates.csv/{year}/all?type=daily_treasury_yield_curve"
                 "&field_tdr_date_value={year}&page&_format=csv")
FRED_BASE = "https://fred.stlouisfed.org/graph/fredgraph.csv"
CBOE_VIX_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
METALS_BASE = "https://api.gold-api.com/price/{symbol}"
CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

TREASURY_TENORS = ("1 Mo", "2 Mo", "3 Mo", "6 Mo", "1 Yr", "2 Yr", "3 Yr",
                   "5 Yr", "7 Yr", "10 Yr", "20 Yr", "30 Yr")


def _num(value: Any) -> float | None:
    """Parse '$1,234.50', '3.2%' and friends into a float."""
    try:
        return float(str(value).replace("$", "").replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def _mdy_to_iso(text: str) -> str | None:
    """'10/23/2026' -> '2026-10-23' (Treasury and CBOE both use m/d/yyyy)."""
    try:
        m, d, y = str(text).strip().split("/")
        return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
    except (ValueError, TypeError):
        dt = parse_dt(text)
        return dt.date().isoformat() if dt else None


def parse_treasury_csv(text: str) -> list[dict]:
    """Parse the daily Treasury par yield curve CSV into rows, newest first."""
    rows: list[dict] = []
    for r in csv.DictReader(io.StringIO(text or "")):
        date = _mdy_to_iso((r.get("Date") or "").strip())
        if not date:
            continue
        row: dict[str, Any] = {"date": date}
        for tenor in TREASURY_TENORS:
            val = _num(r.get(tenor))
            if val is not None:
                row[tenor] = val
        if len(row) > 1:
            rows.append(row)
    rows.sort(key=lambda r: r["date"], reverse=True)
    return rows


def parse_fred_csv(text: str) -> list[tuple[str, float | None]]:
    """Parse a FRED `fredgraph.csv` payload into [(date, value)] oldest first."""
    out: list[tuple[str, float | None]] = []
    for row in csv.reader(io.StringIO(text or "")):
        if not row or len(row) < 2:
            continue
        first = (row[0] or "").strip()
        if first.lower().startswith("observation"):
            continue
        date = first[:10]
        try:
            val = float(row[1])
        except (TypeError, ValueError):
            val = None
        out.append((date, val))
    out.sort(key=lambda p: p[0])
    return out


def parse_cboe_vix(text: str) -> list[tuple[str, float]]:
    """Parse CBOE's VIX history CSV into [(date, close)] oldest first."""
    out: list[tuple[str, float]] = []
    for row in csv.reader(io.StringIO(text or "")):
        if not row or (row[0] or "").strip().upper() == "DATE":
            continue
        date = _mdy_to_iso(row[0])
        try:
            close = float(row[4] if len(row) > 4 else row[-1])
        except (TypeError, ValueError):
            continue
        if date:
            out.append((date, close))
    out.sort(key=lambda p: p[0])
    return out


class FetchResult:
    __slots__ = ("source_id", "name", "items", "ok", "ms", "error")

    def __init__(self, source_id: str, name: str, items: list[dict], ok: bool,
                 ms: int = 0, error: str | None = None) -> None:
        self.source_id = source_id
        self.name = name
        self.items = items
        self.ok = ok
        self.ms = ms
        self.error = error


def _mk_item(**kw: Any) -> dict:
    base = {
        "title": "", "url": "", "source": "", "source_id": "", "published": None,
        "summary": "", "scope": "market", "sector_key": None, "topic": None,
        "ticker": None, "relation": None, "lang": "en",
    }
    base.update(kw)
    return base


def _parse_feed_bytes(raw: bytes) -> feedparser.FeedParserDict:
    return feedparser.parse(raw)


class Sources:
    """Async HTTP facade with a shared client, concurrency limit and health stats."""

    def __init__(self, concurrency: int = 16) -> None:
        self.sem = asyncio.Semaphore(concurrency)
        self.client: httpx.AsyncClient | None = None
        self._sec_cache: dict[str, tuple[float, dict]] = {}
        # which provider answered last; quote() tries it first next time
        self._quote_source: str = "sa"

    async def __aenter__(self) -> "Sources":
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(C.HTTP_TIMEOUT, connect=10.0),
            follow_redirects=True,
            headers={
                "User-Agent": C.USER_AGENT,
                "Accept": "application/rss+xml, application/atom+xml, application/xml, "
                          "application/json, text/xml, */*",
                "Accept-Language": "en-US,en;q=0.9",
            },
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self.client:
            await self.client.aclose()

    # ------------------------------------------------------------------ core
    async def _get(self, url: str, *, headers: dict | None = None,
                   params: dict | None = None,
                   retries: int = C.HTTP_RETRIES) -> httpx.Response:
        assert self.client is not None
        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                async with self.sem:
                    resp = await self.client.get(url, headers=headers, params=params)
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                    await asyncio.sleep(0.6 * (attempt + 1))
                    continue
                resp.raise_for_status()
                return resp
            except (httpx.HTTPError, httpx.TimeoutException) as exc:
                last = exc
                if attempt < retries:
                    await asyncio.sleep(0.5 * (attempt + 1))
        raise last or RuntimeError("request failed")

    # ----------------------------------------------------------- google news
    async def google_news(self, query: str, *, label: str, scope: str,
                          sector_key: str | None = None, topic: str | None = None,
                          ticker: str | None = None, relation: str | None = None,
                          max_items: int = 35, max_per_publisher: int = 4) -> FetchResult:
        sid = f"gnews:{label}"
        url = GN_BASE.format(q=quote_plus(query))
        t0 = time.perf_counter()
        try:
            resp = await self._get(url)
            parsed = await asyncio.to_thread(_parse_feed_bytes, resp.content)
        except Exception as exc:  # noqa: BLE001
            return FetchResult(sid, f"Google News · {label}", [], False,
                               int((time.perf_counter() - t0) * 1000), str(exc)[:200])

        items: list[dict] = []
        for e in parsed.entries:
            title = clean_text(e.get("title", ""))
            if not title:
                continue
            publisher = "Google News"
            src = e.get("source")
            if isinstance(src, dict) and src.get("title"):
                publisher = clean_text(src["title"])
            summary = clean_text(e.get("summary", ""), 400)
            items.append(_mk_item(
                title=title, url=canonical_url(e.get("link", "")), source=publisher,
                source_id=sid, published=parse_dt(e.get("published") or e.get("updated")),
                summary=summary, scope=scope, sector_key=sector_key, topic=topic,
                ticker=ticker, relation=relation,
            ))
        items.sort(key=lambda i: i["published"] or now_utc(), reverse=True)
        # publishers that churn hundreds of SEO articles would otherwise crowd out
        # everything else in a recency-sorted result set
        if max_per_publisher > 0:
            per: dict[str, int] = {}
            diversified: list[dict] = []
            for it in items:
                name = it["source"]
                if per.get(name, 0) >= max_per_publisher:
                    continue
                per[name] = per.get(name, 0) + 1
                diversified.append(it)
                if len(diversified) >= max_items:
                    break
            items = diversified
        items = items[:max_items]
        return FetchResult(sid, f"Google News · {label}", items, True,
                           int((time.perf_counter() - t0) * 1000))

    # ----------------------------------------------------------- direct RSS
    async def rss(self, feed: dict, max_items: int = 35) -> FetchResult:
        sid = feed["id"]
        t0 = time.perf_counter()
        try:
            resp = await self._get(feed["url"])
            parsed = await asyncio.to_thread(_parse_feed_bytes, resp.content)
        except Exception as exc:  # noqa: BLE001
            return FetchResult(sid, feed["name"], [], False,
                               int((time.perf_counter() - t0) * 1000), str(exc)[:200])

        items: list[dict] = []
        for e in parsed.entries:
            title = clean_text(e.get("title", ""))
            if not title:
                continue
            link = e.get("link") or ""
            if not link:
                for l in e.get("links", []) or []:
                    if l.get("rel") == "alternate" and l.get("href"):
                        link = l["href"]
                        break
            summary = clean_text(e.get("summary") or e.get("description") or "", 400)
            if not summary:
                content = e.get("content")
                if isinstance(content, list) and content:
                    summary = clean_text(content[0].get("value", ""), 400)
            items.append(_mk_item(
                title=title, url=canonical_url(link), source=feed["name"], source_id=sid,
                published=parse_dt(e.get("published") or e.get("updated")),
                summary=summary, scope=feed.get("scope", "market"),
                sector_key=feed.get("sector_key"), topic=feed.get("topic"),
            ))
        items.sort(key=lambda i: i["published"] or now_utc(), reverse=True)
        items = items[:max_items]
        return FetchResult(sid, feed["name"], items, True,
                           int((time.perf_counter() - t0) * 1000))

    async def rss_url(self, url: str, *, source_id: str, name: str, ticker: str | None = None,
                      relation: str = "held", max_items: int = 25) -> FetchResult:
        t0 = time.perf_counter()
        try:
            resp = await self._get(url)
            parsed = await asyncio.to_thread(_parse_feed_bytes, resp.content)
        except Exception as exc:  # noqa: BLE001
            return FetchResult(source_id, name, [], False,
                               int((time.perf_counter() - t0) * 1000), str(exc)[:200])
        items: list[dict] = []
        for e in parsed.entries[:max_items]:
            title = clean_text(e.get("title", ""))
            if not title:
                continue
            items.append(_mk_item(
                title=title, url=canonical_url(e.get("link", "")), source=name,
                source_id=source_id, published=parse_dt(e.get("published") or e.get("updated")),
                summary=clean_text(e.get("summary") or "", 400),
                scope="ticker" if ticker else "market", ticker=ticker, relation=relation,
            ))
        return FetchResult(source_id, name, items, True,
                           int((time.perf_counter() - t0) * 1000))

    # -------------------------------------------------------------- nasdaq
    async def nasdaq_news(self, ticker: str, max_items: int = 25) -> FetchResult:
        sid = f"nasdaq_news:{ticker}"
        url = (f"https://api.nasdaq.com/api/news/topic/articlebysymbol"
               f"?q={ticker}%7CSTOCKS&offset=0&limit={max_items}&fallback=true")
        t0 = time.perf_counter()
        try:
            resp = await self._get(url, headers={
                "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/",
                "Accept": "application/json, text/plain, */*",
            })
            data = resp.json().get("data") or {}
        except Exception as exc:  # noqa: BLE001
            return FetchResult(sid, f"Nasdaq · {ticker}", [], False,
                               int((time.perf_counter() - t0) * 1000), str(exc)[:200])

        items: list[dict] = []
        for row in (data.get("rows") or [])[:max_items]:
            title = clean_text(row.get("title"))
            if not title:
                continue
            # Nasdaq gives "Oct 15, 2026" (date only) plus a relative "4 days ago";
            # the relative stamp restores the time of day when the two agree
            pub = parse_dt(row.get("created"))
            ago_dt = parse_ago(row.get("ago"))
            if pub is None:
                pub = ago_dt
            elif ago_dt is not None and abs((pub.date() - ago_dt.date()).days) <= 1:
                pub = ago_dt
            link = row.get("url") or ""
            if link.startswith("/"):
                link = "https://www.nasdaq.com" + link
            items.append(_mk_item(
                title=title, url=canonical_url(link), source=clean_text(row.get("publisher")) or "Nasdaq",
                source_id=sid, published=pub,
                summary=clean_text(row.get("description"), 400),
                scope="ticker", ticker=ticker, relation="held",
                # Nasdaq's per-symbol feed is archive-heavy analyst content: keep a
                # week of it so quiet small caps still get coverage
                max_age_hours=168,
            ))
        return FetchResult(sid, f"Nasdaq · {ticker}", items, True,
                           int((time.perf_counter() - t0) * 1000))

    # ----------------------------------------------------------- sec edgar
    async def sec_filings(self, ticker: str, cik: str, *, days: int = 14,
                          max_items: int = 12,
                          include_insider: bool = False) -> FetchResult:
        sid = f"sec:{ticker}"
        t0 = time.perf_counter()
        cik10 = str(cik).lstrip("0").zfill(10)
        url = f"https://data.sec.gov/submissions/CIK{cik10}.json"
        headers = {"User-Agent": C.SEC_UA, "Accept": "application/json"}
        cached = self._sec_cache.get(cik10)
        try:
            if cached and time.time() - cached[0] < 1800:
                data = cached[1]
            else:
                resp = await self._get(url, headers=headers)
                data = resp.json()
                self._sec_cache[cik10] = (time.time(), data)
        except Exception as exc:  # noqa: BLE001
            return FetchResult(sid, f"SEC · {ticker}", [], False,
                               int((time.perf_counter() - t0) * 1000), str(exc)[:200])

        from datetime import timedelta
        cutoff = (now_utc() - timedelta(days=days)).date().isoformat()
        recent = (data.get("filings") or {}).get("recent") or {}
        forms = recent.get("form") or []
        dates = recent.get("filingDate") or []
        accs = recent.get("accessionNumber") or []
        docs = recent.get("primaryDocument") or []
        descs = recent.get("primaryDocDescription") or []
        items_meta = recent.get("items") or []
        accepted = recent.get("acceptanceDateTime") or []
        entity = data.get("name") or ticker
        allowed = set(C.SEC_KEY_FORMS) | (set(C.SEC_INSIDER_FORMS) if include_insider else set())
        label = {
            "8-K": "current report", "8-K/A": "current report (amended)", "10-Q": "quarterly report",
            "10-K": "annual report", "6-K": "foreign issuer report", "20-F": "annual report (20-F)",
            "40-F": "annual report (40-F)", "S-1": "registration statement",
            "S-3": "shelf registration", "424B5": "securities offering",
            "SC 13D": "activist stake (13D)", "SC 13D/A": "activist stake update",
            "SC 13G": "passive stake (13G)", "DEF 14A": "proxy statement",
            "NT 10-Q": "late filing notice", "NT 10-K": "late filing notice",
            "4": "insider transaction", "144": "insider sale notice",
        }

        items: list[dict] = []
        for i in range(len(forms)):
            form = forms[i]
            fdate = dates[i] if i < len(dates) else ""
            if fdate < cutoff or form not in allowed:
                continue
            acc = accs[i] if i < len(accs) else ""
            doc = docs[i] if i < len(docs) else ""
            acc_nodash = acc.replace("-", "")
            link = (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_nodash}/{doc}"
                    if doc else f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_nodash}/")
            what = label.get(form) or clean_text(descs[i] if i < len(descs) else "", 90) or form
            raw_items = clean_text(items_meta[i] if i < len(items_meta) else "", 120)
            details = [C.SEC_8K_ITEMS.get(code.strip(), code.strip())
                       for code in raw_items.replace(" ", "").split(",") if code.strip()]
            detail = ", ".join(dict.fromkeys(details))
            if form.startswith("8-K") and detail:
                title = f"{entity}: {form} — {detail} ({fdate})"
            else:
                title = f"{entity} filed {form} ({what}) on {fdate}"
            summary = f"SEC {form} filing · {what} · filed {fdate}"
            if detail:
                summary += f" · items: {detail}"
            pub = parse_dt(accepted[i]) if i < len(accepted) else parse_dt(fdate)
            items.append(_mk_item(
                title=title, url=link, source="SEC EDGAR", source_id=sid, published=pub,
                summary=summary, scope="ticker", ticker=ticker, relation="held",
                topic="earnings" if form in ("8-K", "10-Q", "10-K", "6-K", "20-F") else "regulation",
                max_age_hours=days * 24, unique=True,
            ))
            if len(items) >= max_items:
                break
        return FetchResult(sid, f"SEC · {ticker}", items, True,
                           int((time.perf_counter() - t0) * 1000))

    # ---------------------------------------------------------- stocktwits
    async def stocktwits(self, ticker: str, max_items: int = 15) -> FetchResult:
        sid = f"stocktwits:{ticker}"
        url = f"https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json"
        t0 = time.perf_counter()
        try:
            resp = await self._get(url, headers={"Accept": "application/json"})
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            return FetchResult(sid, f"StockTwits · {ticker}", [], False,
                               int((time.perf_counter() - t0) * 1000), str(exc)[:200])
        items: list[dict] = []
        for m in (data.get("messages") or [])[:max_items]:
            body = clean_text(m.get("body"), 300)
            if not body:
                continue
            sent = ((m.get("entities") or {}).get("sentiment") or {}).get("basic")
            likes = ((m.get("likes") or {}).get("total") or 0)
            user = (m.get("user") or {}).get("username") or "anon"
            items.append(_mk_item(
                title=f"${ticker} · {body[:160]}", url=f"https://stocktwits.com/message/{m.get('id')}",
                source=f"StockTwits @{user}", source_id=sid,
                published=parse_dt(m.get("created_at")), summary=body,
                scope="ticker", ticker=ticker, relation="held", topic="social",
                sentiment_hint=sent, likes=likes,
            ))
        return FetchResult(sid, f"StockTwits · {ticker}", items, True,
                           int((time.perf_counter() - t0) * 1000))

    # --------------------------------------------------------------- quotes
    async def stockanalysis_quote(self, ticker: str) -> dict | None:
        """Live-ish snapshot from stockanalysis.com (works when Nasdaq's API is blocked)."""
        url = f"{SA_BASE}/quotes/s/{quote_plus(ticker.lower())}"
        try:
            resp = await self._get(url, headers={"Accept": "application/json"}, retries=1)
            d = resp.json().get("data") or {}
            price = _num(d.get("p"))
            if price is None:
                return None
            prev = _num(d.get("cl"))
            change = _num(d.get("c"))
            change_pct = _num(d.get("cp"))
            if change is None and prev:
                change = round(price - prev, 4)
            if change_pct is None and prev:
                change_pct = round((price / prev - 1) * 100, 4)
            return {
                "ticker": ticker.upper(),
                "name": None,
                "price": price,
                "change": change,
                "change_pct": change_pct,
                "volume": _num(d.get("v")),
                "exchange": d.get("ex"),
                "asof": d.get("u"),
                "asof_iso": iso(),
                "bar_date": d.get("td"),
                "open": _num(d.get("o")),
                "high": _num(d.get("h")),
                "low": _num(d.get("l")),
                "prev_close": prev,
                "high_52w": _num(d.get("h52")),
                "low_52w": _num(d.get("l52")),
                "market_state": d.get("ms"),
                "extended": {
                    "price": _num(d.get("ep")), "change_pct": _num(d.get("ecp")),
                    "session": d.get("es"), "asof": d.get("eu"),
                } if _num(d.get("ep")) else None,
                "source": "stockanalysis",
            }
        except Exception:  # noqa: BLE001
            return None

    async def quote(self, ticker: str, assetclass: str = "stocks") -> dict | None:
        """Prefer the source that last worked, fall back to the other one.

        Nasdaq rate-limits/blocks whole IP ranges; stockanalysis.com occasionally
        throttles. Remembering the winner keeps a scan from paying for 40 failed
        requests every cycle.
        """
        order = ("sa", "nasdaq") if self._quote_source != "nasdaq" else ("nasdaq", "sa")
        for kind in order:
            q = (await self.stockanalysis_quote(ticker) if kind == "sa"
                 else await self.nasdaq_quote(ticker, assetclass))
            if q:
                self._quote_source = kind
                return q
        return None

    async def nasdaq_quote(self, ticker: str, assetclass: str = "stocks") -> dict | None:
        url = (f"https://api.nasdaq.com/api/quote/{quote_plus(ticker)}/info"
               f"?assetclass={assetclass}")
        try:
            resp = await self._get(url, headers={
                "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/",
                "Accept": "application/json, text/plain, */*",
            }, retries=0)
            d = (resp.json().get("data") or {})
            pd = d.get("primaryData") or {}
            def num(v: Any) -> float | None:
                try:
                    return float(str(v).replace("$", "").replace(",", "").replace("%", ""))
                except (TypeError, ValueError):
                    return None
            price = num(pd.get("lastSalePrice"))
            change = num(pd.get("netChange"))
            change_pct = num(pd.get("percentageChange"))
            if price is not None and change_pct is None:
                # Nasdaq leaves netChange/percentageChange blank outside regular hours
                prev = await self._nasdaq_prev_close(ticker, assetclass)
                if prev:
                    change = round(price - prev, 4)
                    change_pct = round((price / prev - 1) * 100, 4)
            return {
                "ticker": ticker.upper(), "name": d.get("companyName"),
                "price": price, "change": change, "change_pct": change_pct,
                "volume": num(pd.get("volume")),
                "exchange": d.get("exchange"),
                "asof": pd.get("lastTradeTimestamp") or pd.get("lastSaleTime"),
                "asof_iso": iso(),
                "source": "nasdaq",
            }
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------- price history
    async def stockanalysis_history(self, ticker: str, *, bars: int = 260) -> dict:
        """Daily OHLCV from stockanalysis.com, oldest bar first.

        Returns {'ticker', 'bars': [{t,o,h,l,c,v}], 'source'}. Raises on failure so
        the caller can put the source in the health table.
        """
        range_ = "1Y" if bars <= 260 else "2Y"
        url = (f"{SA_BASE}/symbol/s/{quote_plus(ticker.lower())}/history"
               f"?range={range_}&period=Daily")
        resp = await self._get(url, headers={"Accept": "application/json"}, retries=1)
        rows = resp.json().get("data") or []
        out: list[dict] = []
        for r in rows:
            date = str(r.get("t") or "")
            close = _num(r.get("c"))
            adj = _num(r.get("a")) or close
            if not date or close is None or adj is None:
                continue
            # StockAnalysis adjusts closes for splits/dividends but not O/H/L; scale
            # them by the same per-bar factor so a series is internally consistent,
            # then rebase to the latest raw close so levels line up with live prices.
            factor = close / adj if adj else 1.0
            out.append({
                "t": date,
                "o": round((_num(r.get("o")) or close) * factor, 4),
                "h": round((_num(r.get("h")) or close) * factor, 4),
                "l": round((_num(r.get("l")) or close) * factor, 4),
                "c": close,
                "v": int(_num(r.get("v")) or 0),
            })
        out.sort(key=lambda b: b["t"])
        # de-duplicate dates, keeping the newest record for each
        dedup: dict[str, dict] = {}
        for b in out:
            dedup[b["t"]] = b
        series = list(dedup.values())[-max(bars, 30):]
        if not series:
            raise ValueError(f"no bars for {ticker}")
        return {"ticker": ticker.upper(), "bars": series, "source": "stockanalysis"}

    # ---------------------------------------------------------------- macro
    async def treasury_yields(self, year: int | None = None) -> list[dict]:
        """Daily Treasury par yield curve, newest row first.

        This is the official close and the cleanest source for 2s10s / 3m10y —
        no intraday futures proxy. Early in January the current-year file can be
        empty, so the previous year is merged in as a fallback.
        """
        y = int(year or now_utc().year)
        rows = await self._treasury_year(y)
        if len(rows) < 2 and y > 2000:
            rows = await self._treasury_year(y - 1) + rows
        dedup = {r["date"]: r for r in rows}
        out = sorted(dedup.values(), key=lambda r: r["date"], reverse=True)
        if not out:
            raise ValueError("Treasury yield curve feed empty")
        return out

    async def _treasury_year(self, year: int) -> list[dict]:
        resp = await self._get(TREASURY_BASE.format(year=int(year)),
                               headers={"Accept": "text/csv,text/plain,*/*"}, retries=1)
        return parse_treasury_csv(resp.text)

    async def fred_series(self, series_id: str, *, start: str | None = None
                          ) -> list[tuple[str, float | None]]:
        """One FRED series as [(date, value)] — keyless `fredgraph.csv` endpoint."""
        params = {"id": series_id}
        if start:
            params["cosd"] = start
        resp = await self._get(FRED_BASE, params=params,
                               headers={"Accept": "text/csv,text/plain,*/*",
                                        "User-Agent": C.MACRO_UA}, retries=1)
        rows = parse_fred_csv(resp.text)
        if not rows:
            raise ValueError(f"FRED series {series_id} empty")
        return rows

    async def cboe_vix(self) -> list[tuple[str, float]]:
        """CBOE's official VIX close history."""
        resp = await self._get(CBOE_VIX_URL, headers={"Accept": "text/csv,text/plain,*/*"},
                               retries=1)
        rows = parse_cboe_vix(resp.text)
        if not rows:
            raise ValueError("CBOE VIX feed empty")
        return rows

    async def metals_spot(self) -> dict[str, dict]:
        """Live spot gold and silver (USD/oz). Raises when neither is available."""

        async def one(symbol: str) -> tuple[str, dict | None]:
            resp = await self._get(METALS_BASE.format(symbol=symbol),
                                   headers={"Accept": "application/json"}, retries=1)
            data = resp.json()
            price = _num(data.get("price"))
            if price is None:
                return symbol, None
            return symbol, {"price": price, "at": data.get("updatedAt"),
                            "name": data.get("name") or symbol}

        pairs = await asyncio.gather(*(one(s) for s in ("XAU", "XAG")),
                                     return_exceptions=True)
        out: dict[str, dict] = {}
        error: Exception | None = None
        for pair in pairs:
            if isinstance(pair, Exception):
                error = pair
            elif pair[1]:
                out[pair[0]] = pair[1]
        if not out:
            raise error or ValueError("metals feed empty")
        return out

    async def economic_calendar(self) -> list[dict]:
        """This week's economic calendar (ForexFactory's public JSON feed)."""
        resp = await self._get(CALENDAR_URL,
                               headers={"Accept": "application/json",
                                        "User-Agent": C.MACRO_UA}, retries=0)
        data = resp.json()
        if not isinstance(data, list):
            raise ValueError("calendar feed malformed")
        return data

    async def _nasdaq_prev_close(self, ticker: str, assetclass: str) -> float | None:
        url = (f"https://api.nasdaq.com/api/quote/{quote_plus(ticker)}/summary"
               f"?assetclass={assetclass}")
        try:
            resp = await self._get(url, headers={
                "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/",
                "Accept": "application/json, text/plain, */*",
            }, retries=0)
            val = ((resp.json().get("data") or {}).get("summaryData") or {})
            prev = (val.get("PreviousClose") or {}).get("value")
            return float(str(prev).replace("$", "").replace(",", "")) if prev else None
        except Exception:  # noqa: BLE001
            return None

    async def nasdaq_profile(self, ticker: str) -> dict | None:
        url = f"https://api.nasdaq.com/api/company/{quote_plus(ticker)}/company-profile"
        try:
            resp = await self._get(url, headers={
                "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/",
                "Accept": "application/json, text/plain, */*",
            }, retries=0)
            d = resp.json().get("data") or {}
            return {
                "name": (d.get("CompanyName") or {}).get("value"),
                "sector": (d.get("Sector") or {}).get("value"),
                "industry": (d.get("Industry") or {}).get("value"),
                "description": (d.get("CompanyDescription") or {}).get("value"),
                "region": (d.get("Region") or {}).get("value"),
            }
        except Exception:  # noqa: BLE001
            return None

    # ---------------------------------------------------------------- misc
    async def homepage_probe(self, url: str, needle: str = "rss") -> bool:
        try:
            resp = await self._get(url, retries=0)
            return needle in resp.text.lower()
        except Exception:  # noqa: BLE001
            return False


async def gather_fetches(coros: Iterable[Any]) -> list[FetchResult]:
    results = await asyncio.gather(*coros, return_exceptions=True)
    out: list[FetchResult] = []
    for r in results:
        if isinstance(r, FetchResult):
            out.append(r)
    return out
