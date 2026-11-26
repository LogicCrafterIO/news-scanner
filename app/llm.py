"""Ollama Cloud client: batch news classification, web-search discovery, industry mapping."""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx

from . import config as C
from .db import db
from .util import clean_text, sha1, trim_sentence

SECTOR_KEYS = [s["key"] for s in C.SECTORS]
SECTOR_LABEL = {s["key"]: s["name"] for s in C.SECTORS}
TOPIC_KEYS = [t["key"] for t in C.MACRO_TOPICS]
VALID_SCOPES = {"macro", "sector", "ticker", "peer", "market"}


# Rough token accounting: ~4 characters per token for English + JSON.
# We use it to show where the API budget goes; it is not a billing-grade meter.
USAGE: dict[str, Any] = {
    "calls": 0, "tokens_in": 0, "tokens_out": 0, "errors": 0,
    "by_tag": {},          # tag -> {"calls", "in", "out"}
    "started_at": None,
}


def est_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _account(tag: str, tokens_in: int = 0, tokens_out: int = 0) -> None:
    USAGE["calls"] += 1
    USAGE["tokens_in"] += tokens_in
    USAGE["tokens_out"] += tokens_out
    row = USAGE["by_tag"].setdefault(tag, {"calls": 0, "in": 0, "out": 0})
    row["calls"] += 1
    row["in"] += tokens_in
    row["out"] += tokens_out


def usage_snapshot(reset: bool = False) -> dict:
    """Current counters; with reset=True return the delta since the last call."""
    snap = {
        "calls": USAGE["calls"],
        "tokens_in": USAGE["tokens_in"],
        "tokens_out": USAGE["tokens_out"],
        "errors": USAGE["errors"],
        "by_tag": {k: dict(v) for k, v in USAGE["by_tag"].items()},
    }
    if reset:
        USAGE["calls"] = USAGE["tokens_in"] = USAGE["tokens_out"] = USAGE["errors"] = 0
        USAGE["by_tag"] = {}
    return snap


def _json_from_text(text: str) -> Any:
    """Best-effort JSON extraction from an LLM response."""
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    for candidate in (t, t[t.find("{"): t.rfind("}") + 1] if "{" in t else "",
                      t[t.find("["): t.rfind("]") + 1] if "[" in t else ""):
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


class LLM:
    """Thin async wrapper around Ollama's /api/chat and /api/web_search."""

    def __init__(self) -> None:
        self.base = C.OLLAMA_BASE_URL
        self.key = C.OLLAMA_API_KEY
        self.client: httpx.AsyncClient | None = None
        self.calls = 0
        self.errors = 0
        self.last_error: str | None = None
        self._sem = asyncio.Semaphore(int(db.setting("llm_concurrency", "3")))

    @property
    def enabled(self) -> bool:
        return bool(self.key) and db.setting("llm_enabled", "1") in ("1", "true", "True")

    def model(self, kind: str = "fast") -> str:
        return db.setting("llm_model_deep" if kind == "deep" else "llm_model_fast",
                          "deepseek-v4.1-flash")

    async def __aenter__(self) -> "LLM":
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(150.0, connect=15.0),
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self.client:
            await self.client.aclose()

    # ------------------------------------------------------------------ chat
    async def chat(self, messages: list[dict], *, kind: str = "fast", json_mode: bool = True,
                   temperature: float = 0.1, retries: int = 1, tag: str = "chat") -> str | None:
        if not self.key or self.client is None:
            return None
        model = self.model(kind)
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature, "num_ctx": 8192},
        }
        if json_mode:
            body["format"] = "json"
        for attempt in range(retries + 1):
            try:
                async with self._sem:
                    self.calls += 1
                    resp = await self.client.post(f"{self.base}/api/chat", json=body)
                resp.raise_for_status()
                data = resp.json()
                content = (data.get("message") or {}).get("content") or ""
                prompt_tokens = sum(est_tokens(m.get("content", "")) for m in messages)
                _account(tag, prompt_tokens, est_tokens(content))
                return content.strip()
            except Exception as exc:  # noqa: BLE001
                self.errors += 1
                self.last_error = f"{type(exc).__name__}: {str(exc)[:180]}"
                if attempt < retries:
                    await asyncio.sleep(1.2 * (attempt + 1))
        return None

    # ------------------------------------------------------------ web search
    async def web_search(self, query: str, max_results: int = 5) -> list[dict]:
        if not self.key or self.client is None:
            return []
        try:
            async with self._sem:
                self.calls += 1
                resp = await self.client.post(
                    f"{self.base}/api/web_search",
                    json={"query": query, "max_results": max_results},
                )
            resp.raise_for_status()
            results = (resp.json().get("results") or [])[:max_results]
            _account("web_search", est_tokens(query),
                     sum(est_tokens((r.get("content") or "") + (r.get("title") or ""))
                         for r in results))
            return results
        except Exception as exc:  # noqa: BLE001
            self.errors += 1
            self.last_error = f"web_search: {type(exc).__name__}: {str(exc)[:150]}"
            return []

    async def list_models(self) -> list[str]:
        if not self.key or self.client is None:
            return []
        try:
            resp = await self.client.get(f"{self.base}/api/tags")
            resp.raise_for_status()
            return [m.get("name") or m.get("model") for m in resp.json().get("models", [])]
        except Exception:  # noqa: BLE001
            return []

    # --------------------------------------------------------- classification
    def _classify_prompt(self, batch: list[dict], universe: dict[str, str]) -> list[dict]:
        sector_lines = "\n".join(f"- {s['key']}: {s['name']} (news keywords: {', '.join(s['keywords'][:8])})"
                                 for s in C.SECTORS)
        topic_lines = ", ".join(f"{t['key']}={t['name']}" for t in C.MACRO_TOPICS)
        uni_lines = "\n".join(f"- {t}: {n}" for t, n in list(universe.items())[:40])
        payload = []
        for i, it in enumerate(batch):
            payload.append({
                "i": i,
                "title": it["title"][:220],
                "source": it.get("source", ""),
                "summary": (it.get("summary") or "")[:200],
                "feed_hint": {"scope": it.get("scope"), "sector": it.get("sector_key"),
                              "ticker": it.get("ticker"), "topic": it.get("topic")},
            })
        system = (
            "You are a sell-side news analyst scoring headlines for a portfolio manager who "
            "holds the listed tickers. Be strict: unrelated items get low relevance. "
            "Never invent facts. Answer with JSON only."
        )
        user = f"""TASK: classify each news item.

GICS SUPER-SECTORS (use these exact keys):
{sector_lines}

MACRO TOPICS (use these exact keys):
{topic_lines}

TRACKED TICKERS (use ticker symbols):
{uni_lines}

For EVERY item return an object with:
- "i": item index (integer)
- "scope": one of "ticker" (materially about a tracked ticker), "peer" (about a close competitor
  in the same industry), "sector" (about a whole sector), "macro" (economy/policy), "market" (broad
  markets, low information)
- "tickers": array of tracked ticker symbols materially affected (may be empty)
- "sectors": array of GICS sector keys affected (may be empty)
- "topics": array of macro topic keys that apply (may be empty)
- "sentiment": "bullish" | "bearish" | "neutral" | "mixed"  (equity impact)
- "impact": integer 1-5 (5 = likely moves prices today)
- "relevance": integer 0-100 (how useful to this specific portfolio; 0 = noise)
- "why": max 18 words explaining the equity angle, no filler

Rules: sector and macro items should still name the most affected sector keys. Drop
duplicate/PR fluff to relevance < 20. Prices-only recaps are relevance <= 25 unless they
name a tracked ticker.

ITEMS:
{json.dumps(payload, ensure_ascii=False)}

Return JSON: {{"items": [...]}}"""
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    async def classify(self, items: list[dict], universe: dict[str, str],
                       concurrency: int = 3, on_done=None) -> dict[int, dict]:
        """items: list of dicts with keys title/source/summary/scope/sector_key/ticker/topic.
        Returns {index: classification}."""
        if not items or not self.key:
            return {}
        batch_size = max(4, int(db.setting("llm_batch_size", "10")))
        batches = [items[i:i + batch_size] for i in range(0, len(items), batch_size)]
        sem = asyncio.Semaphore(max(1, concurrency))

        async def run(batch: list[dict], base: int) -> dict[int, dict]:
            # titles already scored in the last two weeks are free
            cached: dict[int, dict] = {}
            todo: list[tuple[int, dict]] = []
            for j, it in enumerate(batch):
                hit = db.cache_get(f"cls:{sha1(it['title'].lower())}")
                if isinstance(hit, dict):
                    cached[base + j] = hit
                else:
                    todo.append((j, it))
            if not todo:
                return cached
            msgs = self._classify_prompt([it for _, it in todo], universe)
            async with sem:
                raw = await self.chat(msgs, kind="fast", json_mode=True, tag="classify")
            parsed = _json_from_text(raw or "")
            if not isinstance(parsed, dict):
                return {}
            rows = parsed.get("items") or parsed.get("results") or []
            if not isinstance(rows, list):
                return {}
            out: dict[int, dict] = dict(cached)
            if on_done:
                on_done(len(cached))
            for r in rows:
                if not isinstance(r, dict):
                    continue
                try:
                    local = int(r.get("i", r.get("index", -1)))
                except (TypeError, ValueError):
                    continue
                if local < 0 or local >= len(todo):
                    continue
                coerced = self._coerce_classification(r)
                j, it = todo[local]
                out[base + j] = coerced
                db.cache_put(f"cls:{sha1(it['title'].lower())}", coerced, self.model("fast"))
            if on_done:
                on_done(len(out) - len(cached))
            return out

        results = await asyncio.gather(
            *(run(b, i * batch_size) for i, b in enumerate(batches)), return_exceptions=True
        )
        merged: dict[int, dict] = {}
        for r in results:
            if isinstance(r, dict):
                merged.update(r)
        return merged

    @staticmethod
    def _coerce_classification(r: dict) -> dict:
        scope = str(r.get("scope", "market")).lower().strip()
        if scope not in VALID_SCOPES:
            scope = "market"
        sectors = [s for s in (r.get("sectors") or []) if isinstance(s, str)]
        sectors = [s.strip().lower().replace(" ", "_").replace("-", "_") for s in sectors]
        sectors = [s for s in sectors if s in SECTOR_KEYS]
        topics = [t.strip().lower() for t in (r.get("topics") or []) if isinstance(t, str)]
        topics = [t for t in topics if t in TOPIC_KEYS]
        tickers = []
        for t in (r.get("tickers") or []):
            if isinstance(t, str):
                sym = re.sub(r"[^A-Z.\-]", "", t.upper().strip())
                if 1 <= len(sym) <= 6:
                    tickers.append(sym)
        sentiment = str(r.get("sentiment", "neutral")).lower().strip()
        if sentiment not in ("bullish", "bearish", "neutral", "mixed"):
            sentiment = "neutral"

        def clamp(v: Any, lo: int, hi: int, default: int) -> int:
            try:
                return max(lo, min(hi, int(float(v))))
            except (TypeError, ValueError):
                return default

        return {
            "scope": scope,
            "sectors": sectors,
            "topics": topics,
            "tickers": tickers[:6],
            "sentiment": sentiment,
            "impact": clamp(r.get("impact"), 1, 5, 2),
            "relevance": clamp(r.get("relevance"), 0, 100, 30),
            "why": clean_text(str(r.get("why", "")), 160),
        }

    # -------------------------------------------------------- industry mapping
    def _industry_prompt(self, ticker: str, name: str | None, hints: str = "") -> list[dict]:
        sector_lines = "\n".join(f"- {s['key']}: {s['name']}" for s in C.SECTORS)
        return [
            {"role": "system", "content": (
                "You are an equity classification specialist. Map companies to GICS. "
                "Use only well-known, verifiable facts. Answer JSON only."
            )},
            {"role": "user", "content": f"""Classify ticker {ticker} ({name or 'unknown name'}).

{hints}

GICS super-sector keys:
{sector_lines}

Return JSON:
{{"ticker":"{ticker}","sector":"<sector key>","sub_industry":"<GICS sub-industry, e.g. Semiconductors>",
"peers":["TICK1","TICK2","TICK3","TICK4"],"confidence":0.0-1.0,
"reason":"max 15 words"}}
Peers must be 3-6 US-listed companies in the same GICS sub-industry, larger or comparable first."""},
        ]

    async def map_industry(self, ticker: str, name: str | None, hints: str = "",
                           use_search: bool = True) -> dict | None:
        cache_key = f"industry:{ticker.upper()}"
        cached = db.cache_get(cache_key)
        if cached:
            return cached
        search_hints = hints
        if use_search and self.key:
            results = await self.web_search(
                f"{name or ticker} {ticker} company industry sector what does it do", 3
            )
            if results:
                search_hints += "\nWeb results:\n" + "\n".join(
                    f"- {r.get('title','')}: {(r.get('content') or '')[:300]}" for r in results
                )
        raw = await self.chat(self._industry_prompt(ticker, name, search_hints), kind="deep",
                              json_mode=True, tag="industry")
        data = _json_from_text(raw or "")
        if not isinstance(data, dict):
            return None
        sector = str(data.get("sector", "")).strip().lower().replace(" ", "_").replace("-", "_")
        if sector not in SECTOR_KEYS:
            sector = self._fuzzy_sector(sector)
        if not sector:
            return None
        peers = []
        for p in data.get("peers") or []:
            if isinstance(p, str):
                sym = re.sub(r"[^A-Z.\-]", "", p.upper().strip())
                if 1 <= len(sym) <= 6 and sym != ticker.upper():
                    peers.append(sym)
        out = {
            "sector": sector,
            "sector_name": SECTOR_LABEL.get(sector, sector),
            "sub_industry": clean_text(str(data.get("sub_industry", "")), 60)
                            or "Unclassified",
            "peers": peers[:6],
            "confidence": float(data.get("confidence") or 0.5),
        }
        db.cache_put(cache_key, out, self.model("deep"))
        return out

    @staticmethod
    def _fuzzy_sector(word: str) -> str | None:
        if not word:
            return None
        table = {
            "tech": "information_technology", "technology": "information_technology",
            "healthcare": "health_care", "health": "health_care",
            "finance": "financials", "financial": "financials",
            "consumer": "consumer_discretionary", "staples": "consumer_staples",
            "energy": "energy", "industrial": "industrials", "basic_materials": "materials",
            "material": "materials", "utility": "utilities", "realestate": "real_estate",
            "communication": "communication_services", "telecom": "communication_services",
        }
        if word in SECTOR_KEYS:
            return word
        for k, v in table.items():
            if k in word:
                return v
        return None

    # ------------------------------------------------------------- discovery
    async def discover_ticker_news(self, ticker: str, name: str | None,
                                   max_results: int = 6) -> list[dict]:
        query = f"{name} {ticker} stock news today" if name else f"{ticker} stock news today"
        cache_key = f"disc:{sha1(query.lower())}"
        cached = db.cache_get(cache_key)
        results = cached if isinstance(cached, list) else await self.web_search(query, max_results)
        if not cached:
            db.cache_put(cache_key, results, "web_search")
        out = []
        for r in results[:max_results]:
            title = clean_text(r.get("title", ""), 220)
            url = r.get("url") or ""
            if not title or not url:
                continue
            out.append({
                "title": title, "url": url,
                "source": clean_text(url.split("/")[2].removeprefix("www.") if "://" in url else "Web", 40),
                "source_id": "websearch", "summary": clean_text(r.get("content", ""), 400),
                "scope": "ticker", "ticker": ticker, "relation": "held", "topic": None,
                "published": None, "undated": True,
            })
        return out

    async def discover_ir_feed(self, ticker: str, name: str | None) -> dict | None:
        """Find a company investor-relations RSS feed using web search + page autodiscovery."""
        cache_key = f"ir:{ticker.upper()}"
        cached = db.cache_get(cache_key)
        if cached is not None:
            return cached or None
        results = await self.web_search(
            f"{name or ticker} investor relations press releases RSS feed news", 5
        )
        candidate_pages = []
        for r in results:
            url = r.get("url") or ""
            low = url.lower()
            if any(k in low for k in ("investor", "ir.", "/ir/", "press", "news", "media", "corporate")):
                candidate_pages.append(url)
        out = {"ticker": ticker.upper(), "ir_url": candidate_pages[0] if candidate_pages else None,
               "feed_url": None, "candidates": candidate_pages[:5]}
        db.cache_put(cache_key, out, "web_search")
        return out

    # ---------------------------------------------------- technical second opinion
    @staticmethod
    def _tech_payload(snap: dict) -> dict:
        t = snap.get("trend_detail") or {}
        m = snap.get("momentum") or {}
        v = snap.get("volatility") or {}
        vol = snap.get("volume") or {}
        r = snap.get("relative") or {}
        lv = snap.get("levels") or {}
        return {
            "ticker": snap.get("ticker"),
            "date": snap.get("bar_date"),
            "price": snap.get("price"),
            "change_pct": snap.get("change_pct"),
            "computed_bias": snap.get("bias"),
            "score": snap.get("score"),
            "trend": snap.get("trend"),
            "setup": snap.get("setup"),
            "sma": {"sma20": t.get("sma20"), "sma50": t.get("sma50"), "sma200": t.get("sma200")},
            "stack": t.get("stack"), "structure": t.get("structure"),
            "adx": t.get("adx"), "plus_di": t.get("plus_di"), "minus_di": t.get("minus_di"),
            "rsi14": m.get("rsi14"), "rsi_zone": m.get("rsi_zone"),
            "rsi_chg5": m.get("rsi_chg5"), "macd_state": m.get("macd_state"),
            "roc20_pct": m.get("roc20_pct"),
            "atr_pct": v.get("atr_pct"), "vol_regime": v.get("regime"),
            "bb_width_pctile": v.get("bb_width_pctile"),
            "volume_ratio": vol.get("ratio"), "obv_trend": vol.get("obv_trend"),
            "rs20": r.get("rs20"), "rs60": r.get("rs60"), "benchmark": r.get("benchmark"),
            "from_high_pct": lv.get("from_high_pct"), "from_low_pct": lv.get("from_low_pct"),
            "support": [{"price": x.get("price"), "kind": x.get("kind")}
                        for x in (lv.get("support") or [])[:2]],
            "resistance": [{"price": x.get("price"), "kind": x.get("kind")}
                           for x in (lv.get("resistance") or [])[:2]],
            "signals": [x.get("label") for x in (snap.get("signals") or [])[:9]],
            "risks": snap.get("risks") or [],
        }

    def _tech_prompt(self, rows: list[dict]) -> list[dict]:
        payload = [self._tech_payload(s) for s in rows]
        return [
            {"role": "system", "content": (
                "You are a technical analyst writing terse tape notes for a portfolio manager. "
                "Use ONLY the supplied computed indicators and levels — never add outside facts, "
                "news, fundamentals, price targets or trade recommendations. Answer JSON only."
            )},
            {"role": "user", "content": f"""TASK: write a technical read for each ticker.

For every ticker return:
- "ticker": the symbol, exactly as given
- "read": 30-45 words. State the trend structure, what momentum is doing, and what the
  levels imply. Mention whether price is extended or coiling, and the single level that
  matters next.
- "risk": max 14 words naming the main technical risk (e.g. "extended 2.8 ATR, RSI 74").

Rules: no hedging, no preamble, no invented numbers. A trend that is up but has cooling
momentum should be called that. If indicators conflict, say so.

TICKERS (JSON with computed indicators):
{json.dumps(payload, ensure_ascii=False)}

Return JSON: {{"reads": [{{"ticker": "...", "read": "...", "risk": "..."}}]}}"""},
        ]

    async def tech_read(self, snapshots: list[dict], concurrency: int = 2) -> dict[str, str]:
        """Cached LLM second opinion per ticker per trading day.

        Returns {ticker: read}. Only tickers without a cached read are sent, so a
        daily bar refresh costs one call per ticker at most, not one per scan.
        """
        if not snapshots or not self.key:
            return {}
        pending: list[dict] = []
        out: dict[str, str] = {}
        for snap in snapshots:
            if snap.get("error") or not snap.get("bar_date"):
                continue
            key = f"techread:{snap['ticker']}:{snap['bar_date']}"
            hit = db.cache_get(key)
            if isinstance(hit, dict) and hit.get("read"):
                out[snap["ticker"]] = hit["read"]
            else:
                pending.append(snap)
        if not pending:
            return out
        batch_size = 8
        sem = asyncio.Semaphore(max(1, concurrency))

        async def run(batch: list[dict]) -> None:
            async with sem:
                raw = await self.chat(self._tech_prompt(batch), kind="fast",
                                      json_mode=True, temperature=0.15, tag="tech")
            data = _json_from_text(raw or "")
            rows = data.get("reads") if isinstance(data, dict) else None
            if not isinstance(rows, list):
                return
            by_ticker = {str(r.get("ticker", "")).upper(): r for r in rows if isinstance(r, dict)}
            for snap in batch:
                row = by_ticker.get(snap["ticker"].upper())
                if not row:
                    continue
                read = trim_sentence(clean_text(str(row.get("read", "")), None), 320)
                if not read:
                    continue
                risk = trim_sentence(clean_text(str(row.get("risk", "")), None), 130)
                text = read + (f" Risk: {risk}" if risk else "")
                out[snap["ticker"]] = text
                db.cache_put(f"techread:{snap['ticker']}:{snap['bar_date']}",
                             {"read": text}, self.model("fast"))

        await asyncio.gather(*(run(pending[i:i + batch_size])
                               for i in range(0, len(pending), batch_size)))
        return out

    # ------------------------------------------------------------------ brief
    async def brief(self, items: list[dict], sectors: dict[str, int],
                    positions: list[dict], technicals: dict[str, dict] | None = None,
                    sides: dict[str, str | None] | None = None,
                    macro: dict | None = None) -> str | None:
        if not items and not technicals and not macro:
            return None
        compact = [
            {"t": it["title"][:150], "s": it.get("source", ""), "sent": it.get("sentiment"),
             "sect": it.get("sectors"), "tick": it.get("tickers"), "why": it.get("why")}
            for it in items[:40]
        ]
        held = ", ".join(f"{p['ticker']}({p.get('kind')}"
                         f"{', ' + p['side'] if p.get('side') else ''})" for p in positions)
        tech_rows = []
        for ticker, snap in list((technicals or {}).items())[:28]:
            side = (sides or {}).get(ticker)
            ctx = ((snap.get("context") or {}).get(side) or {}) if side else {}
            lv = snap.get("levels") or {}
            sup = (lv.get("support") or [{}])[0].get("price")
            res = (lv.get("resistance") or [{}])[0].get("price")
            tech_rows.append({
                "t": ticker, "side": side, "trend": snap.get("trend"),
                "setup": snap.get("setup"), "score": snap.get("score"),
                "rsi": (snap.get("momentum") or {}).get("rsi14"),
                "support": sup, "resistance": res,
                "aligned": ctx.get("alignment"),
            })
        tech_block = ""
        if tech_rows:
            tech_block = f"""

Technical snapshots (computed locally, -100..+100 score, 0 = neutral):
{json.dumps(tech_rows, ensure_ascii=False)}

Include one short paragraph on the technical picture: which names are aligned with their
tagged long/short side and which are fighting the tape. Do not invent any other technical
numbers."""
        macro_block = ""
        if macro:
            macro_block = f"""

MACRO SNAPSHOT — official/keyless data, authoritative for this note (as of {macro.get('asof') or 'latest'}):
{json.dumps(macro, ensure_ascii=False)}

Open with the rates and macro read: 3M/2Y/10Y/30Y levels and the move in bp, the curve
shape (2s10s, 3m10y), real yields and breakeven inflation, then the dollar, VIX, credit
spreads and the commodity tape. Quote the exact figures and moves from the snapshot.
If a number is not in the snapshot, do not state a number. The "calendar" list holds
upcoming US high/medium-impact releases: if it is empty and "calendar_status" is "ok",
say no high/medium-impact US releases are left this week; if "calendar_status" is
"unavailable", say the calendar could not be fetched; if it is "off", do not mention
the calendar at all."""
        msgs = [
            {"role": "system", "content": (
                "You are a concise markets desk editor. Plain text only. Use only the supplied "
                "headlines, computed technicals and macro snapshot; never invent figures."
            )},
            {"role": "user", "content": f"""Write a 120-165 word desk note for a PM holding: {held}.

Recent scored headlines (JSON):
{json.dumps(compact, ensure_ascii=False)}

Sector headline counts: {json.dumps(sectors)}{tech_block}{macro_block}

Rules: 4 short paragraphs, in this order — (1) rates and macro, with levels and bp moves;
(2) sector rotation; (3) own-name news and the technical picture; (4) what to watch,
naming the next calendar event and its time when one is listed. Lead with the single most
market-moving point, name specific tickers where relevant, no preamble, no disclaimers,
no markdown headers."""},
        ]
        return await self.chat(msgs, kind="deep", json_mode=False, temperature=0.3, tag="brief")
