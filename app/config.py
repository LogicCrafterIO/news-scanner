"""Configuration: env loading, runtime settings, GICS sector profiles, feed catalog."""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "news.db"
CACHE_DIR = DATA_DIR / "cache"
CACHE_DIR.mkdir(exist_ok=True)


# --------------------------------------------------------------------------- #
# .env loading (no external dependency)
# --------------------------------------------------------------------------- #
def load_dotenv(path: Path = BASE_DIR / ".env") -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


load_dotenv()


# --------------------------------------------------------------------------- #
# Defaults (overridable from the UI / stored in the settings table)
# --------------------------------------------------------------------------- #
DEFAULTS: dict[str, str] = {
    "scan_interval_seconds": "180",      # server-side auto refresh cadence
    "auto_refresh": "1",                # 1 = scheduler on
    "item_max_age_hours": "48",
    "max_items_per_feed": "35",
    "max_per_publisher": "4",          # diversify Google News results per outlet
    "max_items_store": "4000",
    "peer_limit": "6",
    "llm_enabled": "1",
    "llm_classify": "1",            # headline scoring (the main token spend)
    "llm_model_fast": os.environ.get("OLLAMA_MODEL_FAST", "deepseek-v4.1-flash"),
    "llm_model_deep": os.environ.get("OLLAMA_MODEL_DEEP", "glm-5.3"),
    "llm_max_analyze": "45",            # new items sent to the LLM per scan
    "llm_min_rule": "30",              # rule-score floor before an item is worth LLM tokens
    "llm_batch_size": "10",
    "llm_concurrency": "6",   # parallel Ollama calls (5 batches for 45 items fits one wave)
    "web_search_enabled": "1",
    "discovery_enabled": "1",           # LLM web-search discovery for tickers/sectors
    "discovery_per_scan": "2",          # tickers hunted per scan (results cached 14 days)
    "llm_brief": "1",                   # desk note on/off
    "llm_brief_min_new": "6",           # ...regenerate only after this many new relevant items
    "llm_brief_min_minutes": "20",      # ...and never faster than this
    "llm_brief_items": "30",            # headlines fed to the desk-note model
    "llm_brief_keep": "200",            # desk notes kept in history (0 = never prune)
    "stocktwits_enabled": "0",
    "sec_enabled": "1",
    "sec_insider_enabled": "0",
    # ---- technical scanner -------------------------------------------------
    "tech_enabled": "1",              # price-action pass over book + watchlist
    "tech_bars": "260",               # daily bars requested per ticker (~1 year)
    "tech_refresh_minutes": "30",     # bar cache TTL (daily bars need one fetch a day)
    "tech_benchmark": "SPY",          # index used for relative strength
    "tech_llm": "1",                 # one cached LLM read per ticker per trading day
    # ---- macro desk ---------------------------------------------------------
    "macro_enabled": "1",            # yields, curve, credit, dollar, commodities
    "macro_refresh_minutes": "15",   # snapshot TTL (daily data needs one fetch a day)
    "macro_calendar": "1",           # upcoming US high/medium-impact data releases
    "macro_brief_trigger": "1",      # let a rates/vol move alone regenerate the desk note
    "theme": "dark",
}

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "https://ollama.com").rstrip("/")
OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY", "").strip()

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
SEC_UA = os.environ.get(
    "SEC_USER_AGENT", "NewsScanner/1.0 (personal research; contact@example.com)"
)
# Wikimedia requires a descriptive UA with contact info for API clients (generic
# browser UAs are rejected with HTTP 403 by their robot policy).
WIKI_UA = os.environ.get(
    "WIKI_USER_AGENT", "NewsScanner/1.0 (personal market research; contact@example.com)"
)
# FRED stalls a request carrying a generic browser UA; its robot policy wants a
# descriptive client string (same shape as the SEC/Wikimedia requirements).
MACRO_UA = os.environ.get(
    "MACRO_USER_AGENT", "NewsScanner/1.0 (personal market research; contact@example.com)"
)

HTTP_TIMEOUT = 20.0
HTTP_RETRIES = 2


# --------------------------------------------------------------------------- #
# 11 GICS super-sectors with news queries + rule-based keywords
# --------------------------------------------------------------------------- #
SECTORS: list[dict] = [
    {
        "key": "information_technology",
        "name": "Information Technology",
        "etf": "XLK",
        "color": "#4f8ef7",
        "queries": [
            '(semiconductor OR chipmaker OR "cloud computing" OR software OR "data center") stock when:1d',
            'XLK technology sector ETF when:2d',
        ],
        "keywords": [
            "semiconductor", "chip", "chips", "software", "cloud", "saas", "data center",
            "datacenter", "cybersecurity", "hardware", "ai chip", "gpu", "server", "it services",
            "tech sector", "xlk", "quantum computing", "fab", "foundry", "lithography",
        ],
    },
    {
        "key": "health_care",
        "name": "Health Care",
        "etf": "XLV",
        "color": "#22c39a",
        "queries": [
            '("health care" OR pharma OR biotech OR medtech OR FDA OR "drug trial") stock when:1d',
            'XLV health care sector ETF when:2d',
        ],
        "keywords": [
            "pharma", "biotech", "drug", "fda", "clinical trial", "medtech", "medical device",
            "hospital", "health insurer", "vaccine", "obesity drug", "glp-1", "oncology",
            "health care", "xlv", "generic drug", "medicare",
        ],
    },
    {
        "key": "financials",
        "name": "Financials",
        "etf": "XLF",
        "color": "#f7b955",
        "queries": [
            '(bank earnings OR "investment bank" OR insurer OR "asset manager" OR fintech OR "regional bank") stock when:1d',
            'XLF financial sector ETF when:2d',
        ],
        "keywords": [
            "bank", "banks", "banking", "lender", "insurer", "insurance", "asset manager",
            "private equity", "fintech", "payments", "brokerage", "credit card", "mortgage lender",
            "financials", "xlf", "deposits", "net interest", "capital markets", "stock exchange",
        ],
    },
    {
        "key": "consumer_discretionary",
        "name": "Consumer Discretionary",
        "etf": "XLY",
        "color": "#e879b9",
        "queries": [
            '(retail sales OR automaker OR e-commerce OR restaurant OR apparel OR travel demand) stock when:1d',
            'XLY consumer discretionary sector ETF when:2d',
        ],
        "keywords": [
            "retailer", "retail", "e-commerce", "automaker", "ev maker", "vehicle sales",
            "restaurant", "apparel", "luxury", "hotel", "airline demand", "homebuilder",
            "consumer discretionary", "xly", "same-store sales", "delivery", "gaming",
        ],
    },
    {
        "key": "consumer_staples",
        "name": "Consumer Staples",
        "etf": "XLP",
        "color": "#8bd17c",
        "queries": [
            '(consumer staples OR "packaged food" OR beverage OR "household products" OR grocer) stock when:1d',
            'XLP consumer staples sector ETF when:2d',
        ],
        "keywords": [
            "consumer staples", "packaged food", "beverage", "brewer", "spirits", "household products",
            "grocer", "supermarket", "tobacco", "personal care", "xlp", "food prices", "volume growth",
        ],
    },
    {
        "key": "energy",
        "name": "Energy",
        "etf": "XLE",
        "color": "#d96a5f",
        "queries": [
            '(oil prices OR OPEC OR "natural gas" OR refinery OR shale OR "crude output") when:1d',
            'XLE energy sector ETF when:2d',
        ],
        "keywords": [
            "oil", "crude", "opec", "natural gas", "lng", "refiner", "refinery", "shale",
            "drilling", "upstream", "oilfield", "energy sector", "xle", "petroleum", "barrel",
            "gas prices", "pipeline",
        ],
    },
    {
        "key": "industrials",
        "name": "Industrials",
        "etf": "XLI",
        "color": "#a8a29e",
        "queries": [
            '(aerospace OR defense contractor OR machinery OR freight OR railroad OR "industrial production") stock when:1d',
            'XLI industrial sector ETF when:2d',
        ],
        "keywords": [
            "aerospace", "defense", "defence", "machinery", "freight", "railroad", "rail",
            "logistics", "airlines", "aircraft", "manufacturing", "industrial production",
            "industrials", "xli", "capital goods", "electrification", "waste management",
        ],
    },
    {
        "key": "materials",
        "name": "Materials",
        "etf": "XLB",
        "color": "#c084fc",
        "queries": [
            '(chemicals OR "gold miner" OR copper OR steel OR lithium OR fertilizer) prices when:1d',
            'XLB materials sector ETF when:2d',
        ],
        "keywords": [
            "chemicals", "chemical", "miner", "mining", "gold", "copper", "steel", "aluminum",
            "lithium", "fertilizer", "paper", "packaging", "materials", "xlb", "rare earth",
            "iron ore", "commodity producer",
        ],
    },
    {
        "key": "utilities",
        "name": "Utilities",
        "etf": "XLU",
        "color": "#60c5e8",
        "queries": [
            '(utility company OR "power grid" OR "electricity demand" OR "nuclear power" OR "data center power") when:1d',
            'XLU utilities sector ETF when:2d',
        ],
        "keywords": [
            "utility", "utilities", "power grid", "electricity", "nuclear", "renewable",
            "solar project", "wind farm", "rate case", "transmission", "xlu", "capacity auction",
            "power demand", "grid operator",
        ],
    },
    {
        "key": "real_estate",
        "name": "Real Estate",
        "etf": "XLRE",
        "color": "#b0a58f",
        "queries": [
            '(REIT OR "real estate investment trust" OR "office vacancy" OR "data center REIT") when:1d',
            'XLRE real estate sector ETF when:2d',
        ],
        "keywords": [
            "reit", "real estate", "office vacancy", "industrial real estate", "apartment",
            "shopping center", "data center reit", "xlre", "cap rate", "property",
            "logistics real estate", "self-storage", "mortgage rates",
        ],
    },
    {
        "key": "communication_services",
        "name": "Communication Services",
        "etf": "XLC",
        "color": "#f472b6",
        "queries": [
            '(streaming OR telecom OR "advertising market" OR "social media" OR "media company") stock when:1d',
            'XLC communication services sector ETF when:2d',
        ],
        "keywords": [
            "streaming", "telecom", "wireless carrier", "advertising", "ad market", "social media",
            "media company", "publisher", "broadband", "subscriber growth", "xlc", "video game",
            "search advertising", "content",
        ],
    },
]

SECTOR_BY_KEY = {s["key"]: s for s in SECTORS}
SECTOR_NAMES = [s["name"] for s in SECTORS]
SECTOR_NAME_TO_KEY = {s["name"]: s["key"] for s in SECTORS}


# --------------------------------------------------------------------------- #
# Macro topic radar — each becomes a Google News query
# --------------------------------------------------------------------------- #
MACRO_TOPICS: list[dict] = [
    {
        "key": "fed_rates",
        "name": "Fed & Rates",
        "queries": ['("Federal Reserve" OR FOMC OR "rate cut" OR "rate hike") when:1d'],
        "keywords": ["federal reserve", "fomc", "powell", "rate cut", "rate hike", "fed funds",
                     "monetary policy", "basis points", "tightening", "easing"],
    },
    {
        "key": "inflation",
        "name": "Inflation",
        "queries": ['(CPI OR "inflation report" OR "PCE inflation") when:1d'],
        "keywords": ["cpi", "inflation", "pce", "price index", "disinflation", "core inflation"],
    },
    {
        "key": "labor",
        "name": "Labor & Jobs",
        "queries": ['("jobs report" OR payrolls OR unemployment OR "labor market") when:1d'],
        "keywords": ["jobs report", "nonfarm", "payrolls", "unemployment", "jobless claims",
                     "labor market", "wage growth"],
    },
    {
        "key": "growth",
        "name": "Growth & Recession",
        "queries": ['(GDP OR recession OR "economic outlook" OR "soft landing") when:1d'],
        "keywords": ["gdp", "recession", "soft landing", "economic growth", "consumer confidence",
                     "pmi", "ism", "retail sales data"],
    },
    {
        "key": "trade_policy",
        "name": "Tariffs & Trade",
        "queries": ['(tariffs OR "trade war" OR "export controls" OR "supply chain") when:1d'],
        "keywords": ["tariff", "trade war", "export control", "sanctions", "import duty",
                     "supply chain", "reshoring", "decoupling"],
    },
    {
        "key": "commodities",
        "name": "Commodities",
        "queries": ['(oil prices OR "gold price" OR "copper price") when:1d'],
        "keywords": ["oil price", "brent", "wti", "gold", "copper", "commodity", "silver",
                     "natural gas price"],
    },
    {
        "key": "credit",
        "name": "Rates & Credit",
        "queries": ['("Treasury yields" OR "bond market" OR "credit spreads") when:1d'],
        "keywords": ["treasury yield", "bond yield", "10-year", "credit spread", "default",
                     "high yield", "bank lending", "liquidity"],
    },
    {
        "key": "geopolitics",
        "name": "Geopolitics",
        "queries": ['(geopolitics OR "Middle East" OR "China economy" OR elections) markets when:1d'],
        "keywords": ["geopolitic", "middle east", "china", "taiwan", "russia", "ukraine",
                     "election", "war", "conflict", "strait"],
    },
    {
        "key": "earnings",
        "name": "Earnings Cycle",
        "queries": ['("earnings season" OR "guidance cut" OR "profit warning" OR "earnings beat") when:1d'],
        "keywords": ["earnings", "guidance", "profit warning", "revenue beat", "eps", "outlook cut",
                     "preannounce", "quarterly results"],
    },
    {
        "key": "regulation",
        "name": "Regulation",
        "queries": ['(antitrust OR "SEC" OR "FTC" OR "regulatory probe") companies when:1d'],
        "keywords": ["antitrust", "ftc", "doj", "sec probe", "regulator", "lawsuit", "fine",
                     "settlement", "class action"],
    },
    {
        "key": "fx_dollar",
        "name": "Dollar & FX",
        "queries": ['("dollar index" OR "yen" OR "euro" OR "currency markets") when:1d'],
        "keywords": ["dollar", "yen", "euro", "currency", "forex", "devaluation", "peg"],
    },
    {
        "key": "ai_theme",
        "name": "AI & Capex Theme",
        "queries": ['("artificial intelligence" OR "AI spending" OR "AI capex" OR "AI bubble") markets when:1d'],
        "keywords": ["artificial intelligence", "ai capex", "ai spending", "ai bubble",
                     "ai infrastructure", "inference", "model training", "ai chip"],
    },
]

MACRO_BY_KEY = {t["key"]: t for t in MACRO_TOPICS}


# --------------------------------------------------------------------------- #
# Direct RSS/Atom feeds — broad macro & market coverage (all verified live)
# --------------------------------------------------------------------------- #
DIRECT_FEEDS: list[dict] = [
    {"id": "fed_press", "name": "Federal Reserve", "url": "https://www.federalreserve.gov/feeds/press_all.xml",
     "scope": "macro", "tier": 1, "topic": "fed_rates", "enabled": True},
    {"id": "wsj_markets", "name": "WSJ Markets", "url": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
     "scope": "macro", "tier": 1, "topic": "growth", "enabled": True},
    {"id": "nyt_business", "name": "NYT Business", "url": "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",
     "scope": "macro", "tier": 1, "topic": "growth", "enabled": True},
    {"id": "bbc_business", "name": "BBC Business", "url": "https://feeds.bbci.co.uk/news/business/rss.xml",
     "scope": "macro", "tier": 2, "topic": "growth", "enabled": True},
    {"id": "guardian_business", "name": "Guardian Business", "url": "https://www.theguardian.com/uk/business/rss",
     "scope": "macro", "tier": 2, "topic": "growth", "enabled": True},
    {"id": "mw_top", "name": "MarketWatch Top", "url": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
     "scope": "macro", "tier": 1, "topic": "growth", "enabled": True},
    {"id": "mw_realtime", "name": "MarketWatch Live", "url": "https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines",
     "scope": "macro", "tier": 1, "topic": "growth", "enabled": True},
    {"id": "mw_pulse", "name": "MarketWatch Pulse", "url": "https://feeds.content.dowjones.io/public/rss/mw_marketpulse",
     "scope": "macro", "tier": 2, "topic": "growth", "enabled": True},
    {"id": "investing_econ", "name": "Investing.com Economy", "url": "https://www.investing.com/rss/news_14.rss",
     "scope": "macro", "tier": 2, "topic": "growth", "enabled": True},
    {"id": "investing_stocks", "name": "Investing.com Stocks", "url": "https://www.investing.com/rss/news_25.rss",
     "scope": "macro", "tier": 2, "topic": "earnings", "enabled": True},
    {"id": "sa_market", "name": "Seeking Alpha", "url": "https://seekingalpha.com/market_currents.xml",
     "scope": "macro", "tier": 2, "topic": "earnings", "enabled": True},
    {"id": "nasdaq_markets", "name": "Nasdaq Markets", "url": "https://www.nasdaq.com/feed/rssoutbound?category=Markets",
     "scope": "macro", "tier": 2, "topic": "growth", "enabled": True},
    {"id": "ft_markets", "name": "FT Markets", "url": "https://www.ft.com/markets?format=rss",
     "scope": "macro", "tier": 1, "topic": "growth", "enabled": True},
    {"id": "yahoo_news", "name": "Yahoo Finance News", "url": "https://news.yahoo.com/rss/",
     "scope": "macro", "tier": 2, "topic": "growth", "enabled": True},
    {"id": "prn_all", "name": "PR Newswire", "url": "https://www.prnewswire.com/rss/news-releases-list.rss",
     "scope": "macro", "tier": 3, "topic": "earnings", "enabled": True},
]

# Candidate paths probed when a ticker has no known IR RSS feed
IR_FEED_PATHS = [
    "/rss/news-releases.xml", "/rss/press-releases.xml", "/press-releases/rss",
    "/news-releases/rss", "/feed/", "/feed", "/rss", "/news/rss", "/rss.xml",
    "/investor/news-releases/feed/", "/news-releases/feed/", "/feed/news-releases",
]

# SEC forms worth surfacing as news (key = market-moving, insider = ownership noise).
# Routine shelf takedowns (424B2/FWP) and proxy solicitations (DEFA14A) are excluded:
# large issuers file them daily and they drown out real events.
SEC_KEY_FORMS = [
    "8-K", "8-K/A", "10-Q", "10-K", "6-K", "20-F", "40-F", "S-1", "S-1/A",
    "S-3", "SC 13D", "SC 13D/A", "SC 13G", "NT 10-Q", "NT 10-K", "8-K12B",
]
SEC_INSIDER_FORMS = ["4", "4/A", "3", "5", "144", "13F-HR"]
SEC_FORMS = SEC_KEY_FORMS + SEC_INSIDER_FORMS

# 8-K item codes carry the actual news, so they get spelled out in the headline
SEC_8K_ITEMS = {
    "1.01": "material agreement", "1.02": "agreement terminated", "2.01": "acquisition completed",
    "2.02": "results of operations", "2.03": "new financial obligation",
    "2.04": "triggering events or acceleration", "2.05": "exit or restructuring costs",
    "2.06": "material impairment", "3.01": "delisting notice", "3.02": "unregistered equity sales",
    "3.03": "material modification of rights", "4.01": "auditor change",
    "4.02": "non-reliance on prior financials", "5.01": "change of control",
    "5.02": "director or officer departure", "5.03": "bylaws or fiscal year change",
    "5.07": "shareholder vote results", "7.01": "Reg FD disclosure",
    "8.01": "other material event", "9.01": "financial statements and exhibits",
}
