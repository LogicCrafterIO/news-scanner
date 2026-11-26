# News Scanner — macro · sector · ticker

A local news desk for a public-equity portfolio: put in your watchlist and live
positions, hit **Scan**, and get one deduplicated, relevance-ranked live feed
covering **all 11 GICS sectors**, **12 macro themes**, **your tickers**, and
**their closest peers** (same GICS sub-industry). It refreshes itself on a timer
and streams new headlines into the browser as they land.

Every scan also runs a **technical scanner** over the same book: a year of daily
bars per name becomes a structured read of trend, momentum, volatility,
participation, relative strength and support/resistance. Tag a name **long** or
**short** and the read is judged against that side — *with the position*,
*mixed*, or *against the position*.

Alongside the news it pulls a **macro desk** — US Treasury yields and the curve,
real yields and breakevens, credit spreads, the dollar, VIX, commodities,
inflation/labour data and the upcoming US release calendar. The tape sits above
the feed and is fed into the desk note, so the note opens with rates and macro
instead of only headlines.

```
./run.sh            # → http://127.0.0.1:8787
```

---

## 1. Quick start

```bash
cd news-scanner
cp .env.example .env          # then paste your Ollama Cloud key
./run.sh                      # creates .venv, installs deps, starts the server
```

Open <http://127.0.0.1:8787>. Add tickers in the left rail (autocomplete knows
every S&P 500/400/600 name), or use a preset (Mag 7, AI & semis, banks, energy,
health care, indices & macro). The first scan starts automatically.

Requirements: Python 3.11+ (built on 3.14), no browser extensions, no cloud
account beyond the optional LLM key.

### `.env`

| key | required | purpose |
| --- | --- | --- |
| `OLLAMA_API_KEY` | optional* | Ollama Cloud key — classification, industry mapping, desk note, web-search discovery |

\* Without a key everything still works: tagging falls back to the rule engine
(keywords, ticker matching, GICS taxonomy), no desk note, no LLM discovery.

Optional: `PORT` (default `8787`), `HOST` (default `127.0.0.1`), `RELOAD=1` for
uvicorn auto-reload while developing.

---

## 2. What a scan does

```
        ┌────────────────────────── 72 sources in parallel ──────────────────────────┐
        │ Google News (24 queries) · 15 direct RSS feeds · Nasdaq news · SA quotes  │
        │ SEC EDGAR filings  ·  per-ticker IR feeds  ·  LLM web-search discovery    │
        └───────────────────────────────────┬───────────────────────────────────────┘
                                            ▼
   normalise → dedupe (hash + near-dup clustering) → tag (ticker / peer / sector / topic)
           → score (rule relevance) → LLM triage of the top slice → impact & sentiment
                                            ▼
                       SQLite (data/news.db)  +  Server-Sent Events  →  dashboard

   daily bars (StockAnalysis) ──► indicators ──► trend / momentum / vol / levels / score
                                  └─► side-aware context (long vs short) ──► Technicals tab

   Treasury · FRED · CBOE · spot metals · US calendar ──► macro snapshot
                                  └─► macro tape (UI) + authoritative block in the desk note
```

* **Macro tape** — keyless macro data in one snapshot per scan: the official
  Treasury curve (3M/2Y/5Y/10Y/30Y, 2s10s, 3m10y), 10Y real yield and breakeven,
  the Fed funds target range, VIX, HY/IG credit spreads, the broad dollar, WTI,
  Brent, Henry Hub gas, spot gold/silver, CPI (headline and core, y/y),
  unemployment, payrolls, jobless claims, the 30Y mortgage rate, SPY/QQQ/IWM/TLT/
  HYG/UUP, and the next US high/medium-impact releases. Cached with a short TTL
  (`macro_refresh_minutes`, default 15) and shown as its own panel — this is not
  news, so it never enters the headline feed.
* **News themes** — 12 themes (Fed & rates, inflation, labour, growth, trade
  policy, commodities, credit, geopolitics, earnings, regulation, FX/dollar, AI
  capex), each backed by a targeted Google News query plus the always-on wires
  (Fed, WSJ, NYT, BBC, Guardian, FT, MarketWatch ×3, Investing.com ×2, Seeking
  Alpha, Nasdaq, Yahoo, PR Newswire).
* **Sectors** — one query per GICS sector per scan (rotation keeps queries
  tight), then rule/LLM tagging assigns each story to up to 3 sectors. The 11
  sector tiles are clickable filters and show the sector ETF's live move.
* **Tickers** — company-name queries (`"Nvidia" when:3d`) plus a `TICKER stock`
  query for short/ambiguous names, Nasdaq's own news API, SEC filings (8-K,
  10-Q, 10-K, S-1, 13D/G…), an IR RSS feed if one is discovered, and per-publisher
  caps so content farms can't crowd out the wires.
* **Peers** — every ticker is mapped to its GICS sub-industry through Wikipedia's
  S&P 500/400/600 tables (≈1,485 tickers with sector, sub-industry and CIK), so
  "Semiconductors" news reaches you even when the headline names AMD or TSMC.
  Peer news is fetched as one query per unique sub-industry and tagged
  `scope=peer`, so it never masquerades as your own holding's news.

### The technical read

The technical pass is independent of the news pipeline and works without an LLM
key. For every name in positions + watchlist it fetches ~260 daily OHLCV bars
from [stockanalysis.com](https://stockanalysis.com) (cached in memory;
`tech_refresh_minutes`, default 30), merges the live quote as today's provisional
bar, and computes locally:

* **Trend** — 20/50/200-day SMAs and stack, 50-day slope, Wilder ADX with ±DI,
  swing structure (higher highs/lows or lower highs/lows), 10-day MA.
* **Momentum** — RSI(14) with zone and 5-day change, MACD(12,26,9) line/signal/
  histogram state (bull/bear × expanding/cooling), stochastic, 5/20/60/120-day
  rate of change.
* **Volatility** — ATR(14) in % of price and its one-year percentile, Bollinger
  width + percentile, 20-day realised annualised vol, extension from the 20-day
  in ATR units.
* **Participation** — last volume vs 20-day average, on-balance volume trend,
  up/down volume ratio.
* **Relative strength** — 20- and 60-session return spread vs `tech_benchmark`
  (default SPY), plus the benchmark's own trend.
* **Levels** — fractal swing highs/lows (with prior-high/prior-low role flips),
  the three SMAs, 20-day Donchian edges and the 52-week range, clustered and
  ranked into the three nearest supports and resistances with distance and ATR
  multiples.

Those inputs become a **-100…+100 composite score** (trend 45%, momentum 30%,
relative strength 15%, participation 10%), a bias (`bullish / neutral /
bearish`), a setup label (`breakout`, `uptrend pullback`, `extended uptrend`,
`breakdown`, `oversold downtrend`, `downtrend bounce`, `coiled range`, `range`),
a list of signals and risks, and a deterministic English narrative such as:

> BB closed at 8.87 on 2026-10-20 (+8.70%), 34.7% below its 52-week high and 33.2%
> above the 200-day average. Trend is a strong uptrend (bullish 20/50/200 stack,
> ADX 13, higher highs / higher lows). Momentum: RSI 54 (neutral), +6 over five
> sessions; MACD bear fading; 20-day change +2.90%. Participation: volume 0.88×
> the 20-day average, OBV flat, +3.5% vs SPY over 20 sessions. Nearest support
> 8.83 (prior high, -0.45%) · resistance 9.22 (swing high, +3.95%).

**Long/short context.** A technical picture is only useful relative to a
position. Every name — watchlist included — can be tagged `long` or `short`
(click the **L/S pill** in the rail, use the side selector in the ticker drawer,
or write `NVDA L` / `TSLA S` in the manage boxes). The snapshot pre-computes
both sides, so switching is instant, and the read says which side it favours:
`with the position`, `mixed`, or `against the position`, with a stop/invalidation
level, a first objective and the trigger that would change the picture. The
Technicals header keeps the aggregate picture honest — breadth, then live
positions and watchlist ideas separately, each split into with / mixed /
against — and every tagged name is a clickable chip so a conflict is one click
from its full chart read. Names where the technicals fight the tagged side
appear in the **conflicts only** filter and in the desk note.

**LLM second opinion.** With `tech_llm=1` and an Ollama key, one batched fast-model
call per trading day turns the computed indicators into a 30-45 word tape read
per name (cached in `llm_cache` per ticker + bar date). It is additive only: the
numbers, levels, score and narrative are always local and deterministic.

### The macro desk

Macro is not news, so it gets its own pipeline and its own panel. One fetch per
`macro_refresh_minutes` (default 15) builds a JSON snapshot that is rendered as
the **Macro tape** above the sector tiles and, more importantly, passed to the
desk-note model as an authoritative block.

| group | metrics | source |
| --- | --- | --- |
| Rates | 3M, 2Y, 5Y, 10Y, 30Y (level + bp vs prior close), 2s10s, 3m10y, 10Y real (TIPS), 10Y breakeven, Fed funds target range | US Treasury daily par curve · FRED |
| Risk & credit | VIX, US high-yield OAS, US IG OAS, broad trade-weighted dollar | CBOE · FRED |
| Commodities | WTI, Brent, Henry Hub gas, spot gold, spot silver | FRED · gold-api.com |
| Economy | CPI y/y, core CPI y/y, unemployment, payrolls m/m, initial claims, 30Y mortgage | FRED (computed locally) |
| Markets | SPY, QQQ, IWM, TLT, HYG, UUP (price + % change) | stockanalysis.com |
| Calendar | next US high/medium-impact releases (ET, forecast, previous) | ForexFactory weekly JSON |

Implementation notes:

* Yields and the curve come from Treasury's official daily close — the cleanest
  source for 2s10s/3m10y, with no futures proxy. FRED fills in what Treasury does
  not publish (TIPS real yield, breakevens, credit OAS, dollar, commodities,
  labour and inflation series); CPI y/y and payrolls m/m are computed locally
  from the index/level series, with the prior observation providing the change.
* Slow series are cached on their own clocks (daily 15 min, weekly 1 h, monthly
  6 h) inside the snapshot TTL, so a 3-minute scan cadence does not hammer the
  sources. A failed source keeps its last good series (marked `stale` in source
  health) instead of blanking the tape.
* Every metric carries its own as-of date and source; the tooltip shows the
  figure, the move, the date and the source. FRED series such as WTI and the broad
  dollar lag the official release by a few business days — the as-of date makes
  that explicit rather than hiding it.
* The calendar feed is rate-limited and only covers the current week. When it is
  unavailable the tape says so and the note is told not to claim that nothing is
  scheduled.
* No LLM key is needed: the whole macro desk is deterministic and keyless.

**In the desk note.** The model receives a compact macro block (levels, moves,
curve, credit, commodities, markets and the next calendar events) and is
instructed to open with the rates/macro paragraph and to use only those figures.
A material tape move (≥4bp on 2Y/10Y/curve, ≥1 VIX point, ≥0.5pp HY OAS or broad
dollar) can regenerate the note even when the headlines are quiet —
`macro_brief_trigger`, on by default. The macro line the note was written against
is archived with it and shown in the history list.

### Deduplication

Two layers. (1) A canonical-URL/title hash catches syndicated copies. (2) An
in-memory token inverted index clusters near-identical headlines
(Jaccard ≥ 0.72 with ≥ 3 shared tokens), so "Nvidia beats on Q3" from twelve
outlets becomes one card with a **+N sources** counter — and later feeds still
enrich the stored row (extra tickers, sectors, topics) instead of being dropped.

### Relevance, impact, sentiment

Rules first, cheap and explainable: scope base score (held ticker 52 > peer 40 >
sector 34 > macro 29 > market 16), recency bonus/penalty, source tier
(1 = primary wire/broker, 3 = aggregator, 5 = content farm), ticker/sector/topic
hits, +6 for SEC filings, −9 for evergreen SEO listicles
("here's what $1,000 in Nvidia would be worth"), −20 and outright exclusion for
known spam domains.

Then the LLM triages only the top `llm_max_analyze` unanalysed headlines per
scan and returns strict JSON (scope, tickers, sectors, topics, relevance,
impact, sentiment, one-line **why**). Final relevance blends
`0.30 × rule + 0.70 × llm + 2.5 × (impact − 2.5)`, clamped to 0–97 — so the LLM
can promote a dull-sounding filing and demote a loud listicle, but the rules
keep it anchored.

### The desk note

Each scan optionally asks the deep model for a four-paragraph PM note:
(1) rates and macro — 3M/2Y/10Y/30Y levels and bp moves, curve, real yields,
breakevens, dollar, VIX, credit and commodities; (2) sector rotation; (3) own-name
news and the technical picture; (4) what to watch, naming the next calendar event.
It's cached per scan and rendered at the top of the feed. **Every note actually
written is archived** in the `briefs` table with its timestamp, model, trigger,
token cost and the macro line it was written against, so the panel survives
restarts and you can page back through earlier takes via **history** in the
desk-note header (newest first, click a note to expand). The number of notes kept
is capped by `llm_brief_keep` (default 200).

---

## 3. Dashboard

| area | what it does |
| --- | --- |
| Top bar | brand, live quote tape (SPY/QQQ + your names), **Scan now**, auto-refresh toggle, interval, settings |
| Left rail | positions + watchlist rows (price, %chg, sector dot, **L/S side pill**, technical trend arrow; hover **⇄** to move a name between the two lists, **✕** to remove), add-ticker box with autocomplete, watch/position selector and side selector, **manage** for bulk editing, presets, source-health strip |
| Manage list | "manage" in the Watchlist header opens two ticker boxes (positions / watchlist). Edit or drag names across, see a live `+add ~move ↔side −remove` diff, save in one shot. Add `L`/`S` after a ticker to tag it long/short. `Ctrl/⌘+Enter` saves. |
| Desk note | the LLM's take on the current scan, led by the macro tape, with "generating…" state |
| Desk note history | **history** in the desk-note header lists archived notes newest-first (absolute time, relative age, model, trigger, macro line at write time); click one to expand it. Hide collapses the body, not the header — history stays reachable. |
| Macro tape | one row per group — rates (3M…30Y, 2s10s, 3m10y, real, breakeven, Fed funds), risk & credit (VIX, HY/IG OAS, broad dollar), commodities (WTI, Brent, gas, gold, silver), economy (CPI, core CPI, unemployment, payrolls, claims, mortgage) and markets (SPY/QQQ/IWM/TLT/HYG/UUP), plus the next US calendar events. Every figure has its as-of date, source and move in a tooltip; **refresh** refetches on demand |
| Sector tiles | 11 GICS sectors, live ETF move, item count — click to filter |
| Topic chips | the 12 macro themes with counts — click to filter |
| Filter bar | All / Macro / Sectors / Tickers / Peers · search · sort · min-relevance slider · hide-noise |
| Feed cards | source, **publish time** (relative *and* absolute), scope, relevance badge, impact dots, why-line, ticker/sector/topic chips, sentiment, +N sources |

**Published vs pulled.** Every card shows both: the relative/absolute **publish**
time from the source, and — as a tooltip (`publish … · pulled …`) — when the
scanner actually fetched it. Sort options:

| sort | orders by |
| --- | --- |
| `smart` (default) | relevance blended with a freshness bonus |
| `published` | publish time, newest first (undated items sink) |
| `published_asc` | publish time, oldest first |
| `added` | when we pulled it, newest first — the "what's new since I last looked" view |
| `relevance` | pure score |

A first scan backfills up to 48h of history, so the two timestamps diverge: an
article published on Friday but pulled on Monday shows `3d ago · 17 Oct 14:22`
and sits low under `published` / high under `added`. Items whose feed carries no
publish date are labelled **undated** and show the pull time instead of a
fabricated "just now".
| Technicals tab | the whole book at a glance: a **breadth + side-alignment header** (bullish/neutral/bearish, and longs/shorts split into with / mixed / against the tape), a **positions vs watchlist alignment board** with clickable name chips grouped by side and verdict, overbought/oversold and 52-week extremes, plus one card per name (sparkline, trend, setup, RSI/MACD/ATR chips, score meter, support/resistance, side verdict). Filter by All / Longs / Shorts / No side, **conflicts only**, search and sort; **Rescan** refreshes bars immediately |
| Ticker drawer | click any ticker (or technical card) → GICS mapping and provenance, peers, full technical read (indicators, signals, levels, computed narrative, cached LLM read, long vs short context), shares/avg-cost/P&L, side selector, IR feed discovery, re-map, remove, that ticker's news |

Shortcuts: `s` scan, `/` search, `Esc` close dialogs. Mobile/responsive down to
390 px.

---

## 4. Settings

Stored in SQLite and editable in the UI (gear icon); `app/config.py → DEFAULTS`
holds the initial values.

| key | default | meaning |
| --- | --- | --- |
| `scan_interval_seconds` | 180 | auto-refresh cadence (scan time is subtracted from the wait) |
| `auto_refresh` | 1 | server-side scheduler on/off |
| `item_max_age_hours` | 48 | freshness window for news items |
| `max_items_per_feed` | 35 | cap per query/feed |
| `max_per_publisher` | 4 | per-outlet cap inside a Google News result set |
| `max_items_store` | 4000 | rows kept in SQLite (oldest pruned) |
| `peer_limit` | 6 | peers tracked per holding |
| `llm_enabled` | 1 | master switch for LLM work |
| `llm_model_fast` | deepseek-v4.1-flash | classification / tagging |
| `llm_model_deep` | glm-5.3 | industry mapping, desk note, discovery |
| `llm_max_analyze` | 60 | headlines triaged per scan (cost control) |
| `llm_batch_size` / `llm_concurrency` | 10 / 3 | batching and parallelism |
| `llm_brief_keep` | 200 | desk notes kept in history (0 = never prune) |
| `web_search_enabled` | 1 | Ollama `/api/web_search` for discovery |
| `discovery_enabled` | 1 | LLM hunts undated stories for 3 tickers per scan |
| `sec_enabled` | 1 | SEC EDGAR filings |
| `sec_insider_enabled` | 0 | Form 4/144 insider filings (noisy, off by default) |
| `stocktwits_enabled` | 0 | retail-sentiment firehose |
| `tech_enabled` | 1 | technical pass over positions + watchlist each scan |
| `tech_bars` | 260 | daily bars requested per ticker (~1 year) |
| `tech_refresh_minutes` | 30 | in-memory bar-cache TTL (daily bars need one fetch a day) |
| `tech_benchmark` | SPY | index used for relative strength and market context |
| `tech_llm` | 1 | cached LLM second opinion per ticker per trading day |
| `macro_enabled` | 1 | macro desk (yields, curve, credit, dollar, commodities, economy) |
| `macro_refresh_minutes` | 15 | macro snapshot TTL |
| `macro_calendar` | 1 | upcoming US high/medium-impact releases |
| `macro_brief_trigger` | 1 | a material rates/vol/credit move alone may regenerate the desk note |

### Ollama token budget

Measured on this install (~4 chars/token estimate, shown live in **Settings →
Ollama usage**):

| call | when | tokens (in/out) |
| --- | --- | --- |
| `classify` | up to `llm_max_analyze` unanalysed headlines per scan, `llm_batch_size` per call | ~210/65 per headline |
| `brief` | desk note, deep model, gated; includes the ~230-token macro block | ~2.1k/180 |
| `web_search` | discovery / industry mapping, cached 14 days | ~50/1k, then free |
| `industry` | new tickers only, cached per ticker | ~1k/150 once |
| `tech` | one cached read per ticker per trading day (batches of 8) | ~120/25 per ticker/day |

Cost controls, all in Settings:

* `llm_min_rule` (30) — items the rule engine already scores as noise never reach the LLM.
* `llm_batch_size` (20) — fewer calls, one shared instruction block.
* `llm_max_analyze` (45) — hard cap per scan.
* `llm_brief_min_minutes` / `llm_brief_min_new` — the desk note is skipped while
  fewer than N relevant items have appeared since the last one (a "regenerate"
  link next to it forces a rewrite). A material macro move can still trigger a
  rewrite when the news is quiet; disable that with `macro_brief_trigger=0`.
* The macro desk itself costs no tokens — it is fetched and formatted locally and
  only adds context to the brief call.
* Classification results are cached by title hash for 14 days, so a headline is
  only paid for once.
* Discovery hits a 14-day cache per ticker; industry mapping is cached per ticker.

### How long a scan takes

Measured on this machine (180 sources, 45 headlines scored):

| stage | time | notes |
| --- | --- | --- |
| fetch | ~15s | 180 feeds in batches of 24, the bar advances per batch |
| ingest/dedupe | ~1s | |
| LLM classification | ~12s | 45 headlines = 5 calls of 10, run 6 at a time |
| quotes + discovery | ~3s | |
| macro desk | ~2s | Treasury/FRED/CBOE/metals/calendar in parallel; mostly cached between scans |
| **total** | **~34s** | at a 3-minute cadence, idle ~80% of the time |

Per-call latency is dominated by output length (one JSON object per headline,
~90 tokens each), so batching 10 with high concurrency beats one giant call:
20 items in one call took 19.5s, the same 20 in two parallel calls took 12.5s.
Raise `llm_concurrency` (parallel calls) rather than `llm_batch_size` if you want
the bar to move faster.

Typical steady state after the first backfill: **2–6k tokens per scan** (only
genuinely new headlines), i.e. roughly 40–120k tokens/hour at a 3-minute cadence.
The first hour or two is more expensive: it drains the unanalysed backlog
(~1.3k headlines) at `llm_max_analyze` per scan.

---

## 5. Sources

Verified working (2026-10): Google News RSS (the workhorse — 100 items per
query, 24 queries in <1 s, no key), StockAnalysis quotes + daily OHLCV bars,
Nasdaq news/profile JSON (quote fallback), SEC EDGAR `submissions` API, Fed, WSJ,
NYT, BBC, Guardian, FT, MarketWatch, Investing.com, Seeking Alpha, Nasdaq
Markets, Yahoo News, PR Newswire, StockTwits (optional), Wikipedia (GICS
taxonomy), Ollama Cloud.

Macro sources (all keyless): the **US Treasury** daily par yield curve CSV (official
close), **FRED** `fredgraph.csv` for real yields, breakevens, credit OAS, the broad
dollar, WTI/Brent/gas, claims and the CPI/unemployment/payrolls series, **CBOE**'s
VIX history, **gold-api.com** for spot gold/silver, and **ForexFactory**'s public
weekly calendar JSON. FRED needs a descriptive client user-agent (a browser UA
stalls), and the calendar feed is strict about request rate — both are cached and
degrade to the last good reading.

Known dead ends, already handled or avoided: Yahoo Finance JSON quote/history
APIs (IP-rate-limited, 429), Nasdaq quote/history REST APIs (Akamai 403 from
some IPs — StockAnalysis is primary), Reddit (403), Business Wire
(feed unavailable), CNBC RSS (empty), Reuters (DNS-blocked), AP/Finviz/BLS
(403/Cloudflare), GlobeNewswire (403), Stooq (404), TradingView's economic-calendar
API (403). Source health (last OK, last error, item counts) is visible in Settings,
and a failing source never aborts a scan.

---

## 6. Industry mapping

`app/industry.py` builds a 1,485-row GICS index from Wikipedia's
"List of S&P 500/400/600 companies" (sector + sub-industry + CIK), cached for 7
days in `data/cache/gics_index.json`. Resolution is layered:

1. direct ticker hit (with `BRK.B / BRK-B / BRKB` variants) in the wiki index;
2. SEC SIC-code mapping via `data/cache/sec_tickers.json` + SIC ranges and
   curated overrides;
3. Nasdaq company-profile sector;
4. LLM mapping (deep model + web search) when confidence < 0.7 — this is what
   rescues cases like ASML, which SIC files under Industrials.

Peers = same GICS sub-industry, S&P 500 → 400 → 600 order, topped up by the LLM
when the index yields fewer than three. Provenance is shown in the ticker
drawer, so you can always see how a name got its label — and re-map it with one
click.

---

## 7. CLI

```bash
.venv/bin/python scripts/scan.py                      # scan existing watchlist
.venv/bin/python scripts/scan.py NVDA AMD TSM --json  # add tickers, print JSON
.venv/bin/python scripts/tech.py                      # technical table for the book
.venv/bin/python scripts/tech.py --llm                # + cached LLM second opinion
.venv/bin/python scripts/macro.py                     # macro tape for the terminal
.venv/bin/python scripts/macro.py --json              # raw macro snapshot
.venv/bin/python scripts/test_technical.py [--live]   # indicator/engine checks
.venv/bin/python scripts/test_macro.py [--live]       # macro parsing/metrics checks
```

---

## 8. Layout

```
app/
  config.py     sources, GICS sectors, macro topics, feeds, defaults
  util.py       time/text/url/token helpers
  db.py         SQLite schema + queries (WAL, single writer lock)
  sources.py    HTTP fetchers (Google News, RSS, Nasdaq, StockAnalysis, SEC, StockTwits)
  llm.py        Ollama Cloud client: classify, map_industry, discover, brief, tech_read
  industry.py   Wikipedia/SEC/Nasdaq/LLM GICS resolver + peer sets
  technical.py  indicators, levels, composite score, narrative, side context
  macro.py      macro desk: Treasury/FRED/CBOE/metals/calendar → snapshot, desk payload
  scanner.py    orchestration: ingest → tag → score → analyse → cluster → technicals → macro → emit
  main.py       FastAPI app, REST + SSE, scheduler lifecycle
  static/       dashboard (vanilla HTML/CSS/JS)
scripts/
  scan.py       headless scan
  tech.py       headless technical table for positions + watchlist
  macro.py      headless macro tape
  test_technical.py  indicator/engine unit checks (+ --live fetch)
  test_macro.py      macro parser/metric unit checks (+ --live fetch)
  ui_check.py   headless-Chromium DOM smoke test (dev only, needs playwright)
data/           news.db, cache/ (gitignored)
```

### API

`GET /api/state` · `GET /api/items?sector=&topic=&ticker=&scope=&limit=&min_relevance=&sort=`
· `POST /api/positions` · `PATCH/DELETE /api/positions/{ticker}`
· `POST /api/positions/{ticker}/ir` · `GET /api/security/{ticker}`
· `GET /api/search?q=` · `POST /api/scan` · `GET/POST /api/settings`
· `GET /api/brief` · `GET /api/briefs?limit=&before=` · `GET /api/briefs/{id}`
· `POST /api/brief` · `POST /api/llm/test` · `POST /api/industry/rebuild`
· `GET /api/technicals?side=&kind=` · `GET /api/technicals/{ticker}`
· `POST /api/technicals/scan?force=true`
· `GET /api/macro?refresh=true`
· `GET /api/health` · `GET /api/stream` (SSE)

---

## 9. Notes & caveats

* The SQLite file is the source of truth for watchlist, positions, settings and
  cache — delete `data/news.db` for a clean slate.
* Google News is an aggregator: respect its terms for anything beyond personal
  research, and prefer the underlying publisher link the cards point to.
* Quotes now come from stockanalysis.com first (delayed snapshots, including
  after-hours), with Nasdaq as a fallback; both are refetched every scan.
* Daily bars are also stockanalysis.com. They are split/dividend-adjusted and
  rebased to the latest close so levels line up with the live quote. A failed
  fetch keeps the last good snapshot rather than blanking the panel; the source's
  health shows in **Settings → Source health** as `StockAnalysis · daily bars`.
* Tagging a side is portfolio context, not an order. Levels labelled
  "invalidation"/"objective" are descriptive technical references — ATR-derived
  fallbacks are used when the chart has no usable swing level nearby.
* Macro yields are **official daily closes**, not intraday ticks; the bp moves are
  against the prior close. FRED series (WTI, broad dollar, CPI, payrolls) publish
  with a lag and can revise, which is why every metric shows its own as-of date.
  The release calendar is best-effort (current week only) and rate-limited. Treat
  all of it as context, not investment advice.
* The LLM is a triage assistant, not investment advice. Every card links out.

---

## Repository note

Source only. `data/news.db` and `.env` are gitignored, so a fresh clone starts with an empty
book and no key. Everything except the LLM features works without one: tagging falls back to the
rule engine and the desk note is skipped. Copy `.env.example` to `.env` and add an Ollama key to
enable classification and the note.

```bash
./run.sh          # creates .venv on first run, serves http://127.0.0.1:8787
```
