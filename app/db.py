"""SQLite storage: watchlist/positions, securities, news items, caches, health."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterable

from .config import DB_PATH, DEFAULTS
from .util import iso, normalize_side, now_utc, parse_dt

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
  ticker    TEXT PRIMARY KEY,
  kind      TEXT NOT NULL DEFAULT 'watch',      -- watch | position
  side      TEXT,                               -- long | short | NULL (unspecified)
  shares    REAL,
  avg_cost  REAL,
  note      TEXT,
  ir_feed   TEXT,
  deep      INTEGER NOT NULL DEFAULT 0,          -- opt-in deep scan (IR feed discovery)
  added_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS technicals (
  ticker      TEXT PRIMARY KEY,
  asof        TEXT,                             -- when the snapshot was computed
  bar_date    TEXT,                             -- last daily bar used
  price       REAL,
  score       REAL,                             -- -100 (bearish) … +100 (bullish)
  bias        TEXT,                             -- bullish | bearish | neutral
  trend       TEXT,                             -- strong_up | up | sideways | down | strong_down
  setup       TEXT,                             -- uptrend_pullback | breakout | …
  rsi         REAL,
  atr_pct     REAL,
  payload     TEXT,                             -- full JSON snapshot (indicators, levels, narrative)
  updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS securities (
  ticker        TEXT PRIMARY KEY,
  name          TEXT,
  cik           TEXT,
  exchange      TEXT,
  sector        TEXT,        -- GICS super-sector name
  sub_industry  TEXT,        -- GICS sub-industry
  sector_key    TEXT,
  source        TEXT,        -- wikirsp500 | wiki400 | wiki600 | nasdaq | sec-sic | llm
  confidence    REAL DEFAULT 0,
  peers         TEXT,        -- JSON list of {ticker,name}
  updated_at    TEXT
);

CREATE TABLE IF NOT EXISTS gics_index (
  ticker        TEXT PRIMARY KEY,
  name          TEXT,
  sector        TEXT,
  sub_industry  TEXT,
  cik           TEXT,
  index_name    TEXT,
  updated_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_gics_sub ON gics_index(sector, sub_industry);

CREATE TABLE IF NOT EXISTS items (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  hash          TEXT UNIQUE,
  cluster_id    TEXT,
  is_dup        INTEGER NOT NULL DEFAULT 0,
  title         TEXT NOT NULL,
  url           TEXT,
  domain        TEXT,
  source        TEXT,
  source_id     TEXT,
  published_at  TEXT,
  fetched_at    TEXT,
  summary       TEXT,
  tickers       TEXT,        -- JSON list
  peers         TEXT,        -- JSON list (peer tickers that triggered it)
  sectors       TEXT,        -- JSON list of sector keys
  topics        TEXT,        -- JSON list of macro topic keys
  scope         TEXT,        -- ticker | peer | sector | macro | market
  relevance     REAL DEFAULT 0,
  sentiment     TEXT,
  impact        TEXT,
  why           TEXT,
  analyzed      INTEGER NOT NULL DEFAULT 0,
  dup_count     INTEGER NOT NULL DEFAULT 0,
  undated       INTEGER NOT NULL DEFAULT 0,   -- source gave no publish time
  source_list   TEXT         -- JSON list of extra sources that ran the same story
);
CREATE INDEX IF NOT EXISTS idx_items_pub ON items(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_items_scope ON items(scope);
CREATE INDEX IF NOT EXISTS idx_items_cluster ON items(cluster_id);
CREATE INDEX IF NOT EXISTS idx_items_analyzed ON items(analyzed);

CREATE TABLE IF NOT EXISTS item_links (
  item_id  INTEGER NOT NULL,
  ticker   TEXT NOT NULL,
  relation TEXT NOT NULL,     -- held | watch | peer | mention
  PRIMARY KEY (item_id, ticker, relation)
);
CREATE INDEX IF NOT EXISTS idx_links_ticker ON item_links(ticker);
CREATE INDEX IF NOT EXISTS idx_links_item ON item_links(item_id);

CREATE TABLE IF NOT EXISTS llm_cache (
  key        TEXT PRIMARY KEY,
  payload    TEXT,
  model      TEXT,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS source_health (
  source_id   TEXT PRIMARY KEY,
  name        TEXT,
  last_ok     TEXT,
  last_error  TEXT,
  last_ms     INTEGER,
  last_items  INTEGER,
  ok_count    INTEGER DEFAULT 0,
  err_count   INTEGER DEFAULT 0,
  total_items INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS scan_runs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  trigger     TEXT,
  started_at  TEXT,
  finished_at TEXT,
  status      TEXT,
  stats       TEXT
);

CREATE TABLE IF NOT EXISTS briefs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at  TEXT NOT NULL,
  text        TEXT NOT NULL,
  model       TEXT,
  trigger     TEXT,                 -- scan | manual-startup | manual-force
  item_marker INTEGER DEFAULT 0,    -- max item id when the note was written
  tokens_in   INTEGER DEFAULT 0,
  tokens_out  INTEGER DEFAULT 0,
  macro       TEXT                  -- JSON: macro tape line + signature at write time
);
CREATE INDEX IF NOT EXISTS idx_briefs_at ON briefs(created_at DESC, id DESC);
"""


class Database:
    def __init__(self, path: Path | str = DB_PATH) -> None:
        self.path = str(path)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=8000")
        with self.lock:
            self.conn.executescript(SCHEMA)
            self._migrate()
            self.conn.commit()
        self._ensure_defaults()

    def _migrate(self) -> None:
        """Add columns introduced after the first schema version."""
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(items)").fetchall()}
        if "undated" not in have:
            self.conn.execute("ALTER TABLE items ADD COLUMN undated INTEGER NOT NULL DEFAULT 0")
        pos_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(positions)").fetchall()}
        if "side" not in pos_cols:
            self.conn.execute("ALTER TABLE positions ADD COLUMN side TEXT")
        brief_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(briefs)").fetchall()}
        if "macro" not in brief_cols:
            self.conn.execute("ALTER TABLE briefs ADD COLUMN macro TEXT")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_items_pub ON items(published_at DESC)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_items_fetch ON items(fetched_at DESC)")

    # ---------------------------------------------------------------- settings
    def _ensure_defaults(self) -> None:
        with self.lock:
            for k, v in DEFAULTS.items():
                self.conn.execute(
                    "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO NOTHING", (k, v)
                )
            self.conn.commit()

    def settings(self) -> dict[str, str]:
        with self.lock:
            rows = self.conn.execute("SELECT key,value FROM settings").fetchall()
        out = dict(DEFAULTS)
        out.update({r["key"]: r["value"] for r in rows})
        return out

    def setting(self, key: str, default: Any = None) -> Any:
        return self.settings().get(key, default)

    def set_settings(self, updates: dict[str, Any]) -> dict[str, str]:
        with self.lock:
            for k, v in updates.items():
                if v is None:
                    continue
                self.conn.execute(
                    "INSERT INTO settings(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(k), str(v)),
                )
            self.conn.commit()
        return self.settings()

    # --------------------------------------------------------------- positions
    def list_positions(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM positions ORDER BY kind, ticker"
            ).fetchall()
        return [dict(r) for r in rows]

    def upsert_position(self, ticker: str, kind: str = "watch", shares: float | None = None,
                        avg_cost: float | None = None, note: str | None = None,
                        ir_feed: str | None = None, deep: int | None = None,
                        side: str | None = None) -> None:
        ticker = ticker.upper().strip()
        side = normalize_side(side)
        with self.lock:
            self.conn.execute(
                """INSERT INTO positions(ticker,kind,side,shares,avg_cost,note,ir_feed,deep,added_at)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(ticker) DO UPDATE SET
                     kind=excluded.kind,
                     side=COALESCE(excluded.side, positions.side),
                     shares=COALESCE(excluded.shares, positions.shares),
                     avg_cost=COALESCE(excluded.avg_cost, positions.avg_cost),
                     note=COALESCE(excluded.note, positions.note),
                     ir_feed=COALESCE(excluded.ir_feed, positions.ir_feed),
                     deep=COALESCE(excluded.deep, positions.deep)""",
                (ticker, kind, side, shares, avg_cost, note, ir_feed, 1 if deep else 0, iso()),
            )
            self.conn.commit()

    def set_position_side(self, ticker: str, side: str | None) -> bool:
        """Set (or clear, with None) the long/short tagging for a name."""
        with self.lock:
            cur = self.conn.execute(
                "UPDATE positions SET side=? WHERE ticker=?",
                (normalize_side(side), ticker.upper().strip()),
            )
            self.conn.commit()
            return bool(cur.rowcount)

    def set_sides(self, sides: dict[str, str | None]) -> int:
        """Bulk side update, e.g. from the manage dialog."""
        n = 0
        for ticker, side in sides.items():
            if self.set_position_side(ticker, side):
                n += 1
        return n

    def delete_position(self, ticker: str) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM positions WHERE ticker=?", (ticker.upper(),))
            self.conn.commit()

    def set_position_many(self, tickers: Iterable[str], kind: str = "watch") -> None:
        for t in tickers:
            t = t.strip().upper()
            if t:
                self.upsert_position(t, kind=kind)

    # -------------------------------------------------------------- technicals
    def upsert_technical(self, ticker: str, snap: dict) -> None:
        """Store the latest computed technical snapshot (payload is the whole thing)."""
        with self.lock:
            self.conn.execute(
                """INSERT INTO technicals(ticker,asof,bar_date,price,score,bias,trend,setup,
                                          rsi,atr_pct,payload,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(ticker) DO UPDATE SET
                     asof=excluded.asof, bar_date=excluded.bar_date, price=excluded.price,
                     score=excluded.score, bias=excluded.bias, trend=excluded.trend,
                     setup=excluded.setup, rsi=excluded.rsi, atr_pct=excluded.atr_pct,
                     payload=excluded.payload, updated_at=excluded.updated_at""",
                (
                    ticker.upper(), snap.get("asof"), snap.get("bar_date"), snap.get("price"),
                    float(snap.get("score") or 0), snap.get("bias"), snap.get("trend"),
                    snap.get("setup"), (snap.get("momentum") or {}).get("rsi14"),
                    (snap.get("volatility") or {}).get("atr_pct"),
                    json.dumps(snap), iso(),
                ),
            )
            self.conn.commit()

    @staticmethod
    def _decode_technical(row: sqlite3.Row) -> dict:
        d = dict(row)
        try:
            payload = json.loads(d.pop("payload") or "{}")
        except (json.JSONDecodeError, TypeError):
            payload = {}
        # table columns are authoritative when an older payload is missing a field
        for k in ("asof", "bar_date", "price", "score", "bias", "trend", "setup"):
            if d.get(k) is not None:
                payload[k] = d[k]
        payload["ticker"] = d["ticker"]
        payload["updated_at"] = d.get("updated_at")
        return payload

    def get_technical(self, ticker: str) -> dict | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM technicals WHERE ticker=?", (ticker.upper(),)
            ).fetchone()
        return self._decode_technical(row) if row else None

    def list_technicals(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM technicals ORDER BY ticker").fetchall()
        return [self._decode_technical(r) for r in rows]

    def delete_technical(self, ticker: str) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM technicals WHERE ticker=?", (ticker.upper(),))
            self.conn.commit()

    def prune_technicals(self, keep: Iterable[str]) -> int:
        """Drop snapshots for names no longer on the watchlist or in positions."""
        keep_set = {t.upper() for t in keep}
        with self.lock:
            rows = self.conn.execute("SELECT ticker FROM technicals").fetchall()
            gone = [r["ticker"] for r in rows if r["ticker"].upper() not in keep_set]
            if gone:
                self.conn.executemany("DELETE FROM technicals WHERE ticker=?",
                                      [(t,) for t in gone])
                self.conn.commit()
        return len(gone)

    # -------------------------------------------------------------- securities
    def get_security(self, ticker: str) -> dict | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM securities WHERE ticker=?", (ticker.upper(),)
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["peers"] = json.loads(d["peers"]) if d.get("peers") else []
        return d

    def upsert_security(self, sec: dict) -> None:
        with self.lock:
            self.conn.execute(
                """INSERT INTO securities(ticker,name,cik,exchange,sector,sub_industry,sector_key,
                                          source,confidence,peers,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(ticker) DO UPDATE SET
                     name=COALESCE(excluded.name, securities.name),
                     cik=COALESCE(excluded.cik, securities.cik),
                     exchange=COALESCE(excluded.exchange, securities.exchange),
                     sector=COALESCE(excluded.sector, securities.sector),
                     sub_industry=COALESCE(excluded.sub_industry, securities.sub_industry),
                     sector_key=COALESCE(excluded.sector_key, securities.sector_key),
                     source=excluded.source,
                     confidence=excluded.confidence,
                     peers=excluded.peers,
                     updated_at=excluded.updated_at""",
                (
                    sec["ticker"].upper(), sec.get("name"), sec.get("cik"), sec.get("exchange"),
                    sec.get("sector"), sec.get("sub_industry"), sec.get("sector_key"),
                    sec.get("source"), sec.get("confidence", 0),
                    json.dumps(sec.get("peers") or []), iso(),
                ),
            )
            self.conn.commit()

    # ---------------------------------------------------------------- gics idx
    def replace_gics_index(self, rows: list[dict]) -> int:
        with self.lock:
            self.conn.execute("DELETE FROM gics_index")
            self.conn.executemany(
                """INSERT OR REPLACE INTO gics_index(ticker,name,sector,sub_industry,cik,index_name,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                [(r["ticker"], r.get("name"), r.get("sector"), r.get("sub_industry"),
                  r.get("cik"), r.get("index_name"), iso()) for r in rows],
            )
            self.conn.commit()
        return len(rows)

    def gics_get(self, ticker: str) -> dict | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM gics_index WHERE ticker=?", (ticker.upper(),)
            ).fetchone()
        return dict(row) if row else None

    def gics_count(self) -> int:
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) c FROM gics_index").fetchone()["c"]

    def gics_peers(self, sector: str | None, sub_industry: str | None,
                   exclude: Iterable[str] = (), limit: int = 6) -> list[dict]:
        if not sub_industry:
            return []
        ex = {t.upper() for t in exclude}
        with self.lock:
            rows = self.conn.execute(
                """SELECT ticker,name,sector,sub_industry,index_name FROM gics_index
                   WHERE sector=? AND sub_industry=?
                   ORDER BY CASE index_name WHEN 'S&P 500' THEN 0 WHEN 'S&P 400' THEN 1 ELSE 2 END,
                            ticker""",
                (sector, sub_industry),
            ).fetchall()
        out = []
        for r in rows:
            if r["ticker"].upper() in ex:
                continue
            out.append(dict(r))
            if len(out) >= limit:
                break
        return out

    def gics_search(self, query: str, limit: int = 20) -> list[dict]:
        q = f"%{query.strip()}%"
        with self.lock:
            rows = self.conn.execute(
                """SELECT ticker,name,sector,sub_industry FROM gics_index
                   WHERE ticker LIKE ? OR name LIKE ? OR sub_industry LIKE ? LIMIT ?""",
                (q.upper(), q, q, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------- items
    def insert_item(self, item: dict) -> tuple[int, bool]:
        """Insert unless the hash already exists. Returns (id, inserted)."""
        cols = ("hash", "cluster_id", "is_dup", "title", "url", "domain", "source", "source_id",
                "published_at", "fetched_at", "summary", "tickers", "peers", "sectors", "topics",
                "scope", "relevance", "sentiment", "impact", "why", "analyzed", "dup_count",
                "undated", "source_list")
        values = []
        for c in cols:
            v = item.get(c)
            if c in ("tickers", "peers", "sectors", "topics", "source_list") and not isinstance(v, str):
                v = json.dumps(v or [])
            values.append(v if v is not None else (0 if c in ("is_dup", "analyzed", "dup_count", "undated") else None))
        with self.lock:
            try:
                cur = self.conn.execute(
                    f"INSERT INTO items({','.join(cols)}) VALUES({','.join('?' * len(cols))})", values
                )
                self.conn.commit()
                return int(cur.lastrowid), True
            except sqlite3.IntegrityError:
                row = self.conn.execute("SELECT id FROM items WHERE hash=?", (item["hash"],)).fetchone()
                return (int(row["id"]) if row else 0), False

    def bump_cluster(self, cluster_id: str, source: str) -> None:
        with self.lock:
            row = self.conn.execute(
                "SELECT id,source_list,dup_count FROM items WHERE cluster_id=? AND is_dup=0 LIMIT 1",
                (cluster_id,),
            ).fetchone()
            if not row:
                return
            sources = []
            if row["source_list"]:
                try:
                    sources = json.loads(row["source_list"])
                except json.JSONDecodeError:
                    sources = []
            if source and source not in sources:
                sources.append(source)
            self.conn.execute(
                "UPDATE items SET dup_count=?, source_list=? WHERE id=?",
                (int(row["dup_count"] or 0) + 1, json.dumps(sources[-8:]), row["id"]),
            )
            self.conn.commit()

    def merge_item_tags(self, item_hash: str, *, sectors: list[str] | None = None,
                        topics: list[str] | None = None, tickers: list[str] | None = None,
                        scope: str | None = None) -> bool:
        """Enrich an existing row when the same story arrives from another feed.

        A headline first seen in a macro feed may later be matched by a sector or
        ticker query; merging keeps the sector/ticker attribution accurate.
        """
        with self.lock:
            row = self.conn.execute(
                "SELECT id, sectors, topics, tickers, scope FROM items WHERE hash=?",
                (item_hash,),
            ).fetchone()
            if not row:
                return False
            changed = False

            def merge(col: str, extra: list[str] | None) -> list[str]:
                nonlocal changed
                try:
                    current = json.loads(row[col]) if row[col] else []
                except (json.JSONDecodeError, TypeError):
                    current = []
                if not isinstance(current, list):
                    current = []
                merged = list(dict.fromkeys(current + [x for x in (extra or []) if x]))
                if merged != current:
                    changed = True
                return merged

            sectors_m = merge("sectors", sectors)[:4]
            topics_m = merge("topics", topics)[:4]
            tickers_m = merge("tickers", tickers)[:8]
            new_scope = row["scope"]
            rank = {"ticker": 4, "peer": 3, "sector": 2, "macro": 1, "market": 0}
            if scope and rank.get(scope, 0) > rank.get(new_scope or "market", 0):
                new_scope = scope
                changed = True
            if not changed:
                return False
            self.conn.execute(
                "UPDATE items SET sectors=?, topics=?, tickers=?, scope=? WHERE id=?",
                (json.dumps(sectors_m), json.dumps(topics_m), json.dumps(tickers_m),
                 new_scope, row["id"]),
            )
            self.conn.commit()
            return True

    def get_item_by_hash(self, item_hash: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM items WHERE hash=?", (item_hash,)).fetchone()
        return self._decode_item(dict(row)) if row else None

    def add_links(self, item_id: int, links: Iterable[tuple[str, str]]) -> None:
        with self.lock:
            self.conn.executemany(
                "INSERT OR IGNORE INTO item_links(item_id,ticker,relation) VALUES(?,?,?)",
                [(item_id, t.upper(), rel) for t, rel in links],
            )
            self.conn.commit()

    def get_item(self, item_id: int) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        return self._decode_item(dict(row)) if row else None

    @staticmethod
    def _decode_item(d: dict) -> dict:
        for k in ("tickers", "peers", "sectors", "topics", "source_list"):
            if d.get(k):
                try:
                    d[k] = json.loads(d[k])
                except (json.JSONDecodeError, TypeError):
                    d[k] = []
            else:
                d[k] = []
        d["undated"] = int(d.get("undated") or 0)
        return d

    ORDER_SQL = {
        # newest stories first; items with no publish time sink to the bottom
        "published": "ORDER BY (i.published_at IS NULL), i.published_at DESC, i.id DESC",
        "published_asc": "ORDER BY (i.published_at IS NULL), i.published_at ASC, i.id ASC",
        # when we pulled it, regardless of when it was published
        "added": "ORDER BY COALESCE(i.fetched_at,'') DESC, i.id DESC",
        "relevance": "ORDER BY i.relevance DESC, COALESCE(i.published_at,'') DESC",
        "smart": "ORDER BY i.relevance DESC, COALESCE(i.published_at,'') DESC",
    }

    def list_items(self, *, limit: int = 150, scope: str | None = None,
                   sector: str | None = None, topic: str | None = None, ticker: str | None = None,
                   query: str | None = None, since_id: int | None = None,
                   min_relevance: float = 0.0, include_dups: bool = False,
                   max_age_hours: float | None = None, sort: str = "published") -> list[dict]:
        where = ["1=1"]
        args: list[Any] = []
        if not include_dups:
            where.append("i.is_dup=0")
        if scope:
            where.append("i.scope=?")
            args.append(scope)
        if min_relevance:
            where.append("i.relevance>=?")
            args.append(min_relevance)
        if since_id:
            where.append("i.id>?")
            args.append(since_id)
        if max_age_hours:
            # undated items have no publish time; bound them by when we pulled them
            cutoff = iso(now_utc() - timedelta(hours=max_age_hours))
            where.append("(i.published_at>=? OR (i.published_at IS NULL AND COALESCE(i.fetched_at,'')>=?))")
            args += [cutoff, cutoff]
        if sector:
            where.append("i.sectors LIKE ?")
            args.append(f'%"{sector}"%')
        if topic:
            where.append("i.topics LIKE ?")
            args.append(f'%"{topic}"%')
        if ticker:
            where.append("EXISTS(SELECT 1 FROM item_links l WHERE l.item_id=i.id AND l.ticker=?)")
            args.append(ticker.upper())
        if query:
            where.append("(i.title LIKE ? OR i.summary LIKE ?)")
            args += [f"%{query}%", f"%{query}%"]
        order = self.ORDER_SQL.get(sort or "published", self.ORDER_SQL["published"])
        sql = (f"SELECT i.* FROM items i WHERE {' AND '.join(where)} {order} LIMIT ?")
        args.append(int(limit))
        with self.lock:
            rows = self.conn.execute(sql, args).fetchall()
        return [self._decode_item(dict(r)) for r in rows]

    def items_for_ticker(self, ticker: str, limit: int = 60) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                """SELECT i.*, l.relation FROM items i JOIN item_links l ON l.item_id=i.id
                   WHERE l.ticker=? AND i.is_dup=0
                   ORDER BY COALESCE(i.published_at,'') DESC, i.id DESC LIMIT ?""",
                (ticker.upper(), limit),
            ).fetchall()
        return [self._decode_item(dict(r)) for r in rows]

    def unanalyzed(self, limit: int, min_relevance: float = 0.0) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                """SELECT * FROM items WHERE analyzed=0 AND is_dup=0 AND relevance>=?
                   ORDER BY relevance DESC, COALESCE(published_at,'') DESC LIMIT ?""",
                (float(min_relevance), int(limit)),
            ).fetchall()
        return [self._decode_item(dict(r)) for r in rows]

    def max_item_id(self) -> int:
        with self.lock:
            row = self.conn.execute("SELECT COALESCE(MAX(id),0) m FROM items").fetchone()
        return int(row["m"])

    def count_new_relevant_since(self, since_id: int, min_relevance: float = 45.0) -> int:
        with self.lock:
            row = self.conn.execute(
                "SELECT COUNT(*) c FROM items WHERE is_dup=0 AND analyzed=1 "
                "AND id>? AND relevance>=?", (int(since_id), float(min_relevance))
            ).fetchone()
        return int(row["c"])

    def mark_analyzed(self, item_id: int, fields: dict) -> None:
        allowed = {k: fields[k] for k in
                   ("scope", "relevance", "sentiment", "impact", "why", "tickers", "sectors", "topics")
                   if k in fields}
        if not allowed:
            allowed = {}
        sets = ", ".join(f"{k}=?" for k in allowed) + (", " if allowed else "") + "analyzed=1"
        vals = [json.dumps(v) if isinstance(v, (list, tuple)) else v for v in allowed.values()]
        with self.lock:
            self.conn.execute(f"UPDATE items SET {sets} WHERE id=?", (*vals, item_id))
            self.conn.commit()

    def recent_titles(self, hours: float = 72, limit: int = 3000) -> list[tuple[int, str, str]]:
        cutoff = iso(now_utc() - timedelta(hours=hours))
        with self.lock:
            rows = self.conn.execute(
                "SELECT id,title,COALESCE(cluster_id,hash) c FROM items "
                "WHERE COALESCE(published_at,fetched_at)>=? AND is_dup=0 ORDER BY id DESC LIMIT ?",
                (cutoff, limit),
            ).fetchall()
        return [(r["id"], r["title"], r["c"]) for r in rows]

    def apply_cluster(self, item_id: int, cluster_id: str) -> None:
        with self.lock:
            self.conn.execute("UPDATE items SET cluster_id=? WHERE id=?", (cluster_id, item_id))
            self.conn.commit()

    def counts_by_sector(self) -> dict[str, int]:
        out = {}
        with self.lock:
            rows = self.conn.execute(
                "SELECT sectors FROM items WHERE is_dup=0 AND sectors IS NOT NULL AND sectors!='[]'"
            ).fetchall()
        for r in rows:
            try:
                for s in json.loads(r["sectors"]):
                    out[s] = out.get(s, 0) + 1
            except (json.JSONDecodeError, TypeError):
                continue
        return out

    def counts_by_topic(self) -> dict[str, int]:
        out: dict[str, int] = {}
        with self.lock:
            rows = self.conn.execute(
                "SELECT topics FROM items WHERE is_dup=0 AND topics IS NOT NULL AND topics!='[]'"
            ).fetchall()
        for r in rows:
            try:
                for t in json.loads(r["topics"]):
                    out[t] = out.get(t, 0) + 1
            except (json.JSONDecodeError, TypeError):
                continue
        return out

    def stats(self) -> dict:
        with self.lock:
            total = self.conn.execute("SELECT COUNT(*) c FROM items WHERE is_dup=0").fetchone()["c"]
            last24 = self.conn.execute(
                "SELECT COUNT(*) c FROM items WHERE is_dup=0 AND COALESCE(published_at,fetched_at)>=?",
                (iso(now_utc() - timedelta(hours=24)),),
            ).fetchone()["c"]
            by_scope = {r["scope"]: r["c"] for r in self.conn.execute(
                "SELECT COALESCE(scope,'?') scope, COUNT(*) c FROM items WHERE is_dup=0 GROUP BY scope"
            ).fetchall()}
            analyzed = self.conn.execute("SELECT COUNT(*) c FROM items WHERE analyzed=1").fetchone()["c"]
        return {"total": total, "last24h": last24, "by_scope": by_scope, "analyzed": analyzed}

    def prune(self, keep: int = 4000) -> int:
        with self.lock:
            cutoff = self.conn.execute(
                "SELECT id FROM items ORDER BY id DESC LIMIT 1 OFFSET ?", (int(keep),)
            ).fetchone()
            if not cutoff:
                return 0
            cur = self.conn.execute("DELETE FROM items WHERE id<=?", (cutoff["id"],))
            self.conn.execute(
                "DELETE FROM item_links WHERE item_id NOT IN (SELECT id FROM items)"
            )
            self.conn.commit()
            return cur.rowcount

    # ------------------------------------------------------------------ briefs
    @staticmethod
    def _decode_brief(row: sqlite3.Row | dict) -> dict:
        d = dict(row)
        raw = d.get("macro")
        if raw:
            try:
                d["macro"] = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                d["macro"] = None
        return d

    def add_brief(self, text: str, *, model: str = "", trigger: str = "",
                  item_marker: int = 0, tokens_in: int = 0, tokens_out: int = 0,
                  created_at: str | None = None, macro: dict | None = None) -> int:
        """Archive a desk note. Returns the new row id."""
        with self.lock:
            cur = self.conn.execute(
                """INSERT INTO briefs(created_at,text,model,trigger,item_marker,tokens_in,
                                      tokens_out,macro)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (created_at or iso(), text, model, trigger, int(item_marker),
                 int(tokens_in), int(tokens_out),
                 json.dumps(macro, ensure_ascii=False) if macro else None),
            )
            self.conn.commit()
            return int(cur.lastrowid)

    def list_briefs(self, limit: int = 50, before: str | None = None) -> list[dict]:
        """Newest first. `before` is an ISO timestamp for paging (rows older than it)."""
        where, args = ["1=1"], []
        if before:
            where.append("created_at < ?")
            args.append(before)
        with self.lock:
            rows = self.conn.execute(
                f"""SELECT id,created_at,text,model,trigger,item_marker,tokens_in,tokens_out,
                           macro
                    FROM briefs WHERE {' AND '.join(where)}
                    ORDER BY created_at DESC, id DESC LIMIT ?""",
                (*args, int(limit)),
            ).fetchall()
        return [self._decode_brief(r) for r in rows]

    def get_brief(self, brief_id: int) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM briefs WHERE id=?", (int(brief_id),)).fetchone()
        return self._decode_brief(row) if row else None

    def latest_brief(self) -> dict | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM briefs ORDER BY created_at DESC, id DESC LIMIT 1"
            ).fetchone()
        return self._decode_brief(row) if row else None

    def count_briefs(self) -> int:
        with self.lock:
            return int(self.conn.execute("SELECT COUNT(*) c FROM briefs").fetchone()["c"])

    def prune_briefs(self, keep: int = 200) -> int:
        """Keep only the newest `keep` notes (0 disables pruning)."""
        if int(keep) <= 0:
            return 0
        with self.lock:
            cutoff = self.conn.execute(
                "SELECT id FROM briefs ORDER BY id DESC LIMIT 1 OFFSET ?", (int(keep),)
            ).fetchone()
            if not cutoff:
                return 0
            cur = self.conn.execute("DELETE FROM briefs WHERE id<=?", (cutoff["id"],))
            self.conn.commit()
            return cur.rowcount

    # ------------------------------------------------------------------ caches
    def cache_get(self, key: str) -> dict | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT payload,created_at FROM llm_cache WHERE key=?", (key,)
            ).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row["payload"])
        except json.JSONDecodeError:
            return None
        created = parse_dt(row["created_at"])
        if created and (now_utc() - created).total_seconds() > 14 * 86400:
            return None
        return payload

    def cache_put(self, key: str, payload: Any, model: str = "") -> None:
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO llm_cache(key,payload,model,created_at) VALUES(?,?,?,?)",
                (key, json.dumps(payload), model, iso()),
            )
            self.conn.commit()

    # ------------------------------------------------------------------ health
    def record_health(self, source_id: str, name: str, ok: bool, ms: int = 0,
                      items: int = 0, error: str | None = None) -> None:
        with self.lock:
            self.conn.execute(
                """INSERT INTO source_health(source_id,name,last_ok,last_error,last_ms,last_items,
                                             ok_count,err_count,total_items)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(source_id) DO UPDATE SET
                     name=excluded.name,
                     last_ok=CASE WHEN excluded.ok_count=1 THEN excluded.last_ok ELSE source_health.last_ok END,
                     last_error=CASE WHEN excluded.err_count=1 THEN excluded.last_error ELSE source_health.last_error END,
                     last_ms=excluded.last_ms,
                     last_items=excluded.last_items,
                     ok_count=source_health.ok_count+excluded.ok_count,
                     err_count=source_health.err_count+excluded.err_count,
                     total_items=source_health.total_items+excluded.total_items""",
                (source_id, name, iso() if ok else None, error, ms, items,
                 1 if ok else 0, 0 if ok else 1, items if ok else 0),
            )
            self.conn.commit()

    def health(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM source_health ORDER BY err_count DESC, name"
            ).fetchall()
        return [dict(r) for r in rows]

    # --------------------------------------------------------------- scan runs
    def start_run(self, trigger: str) -> int:
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO scan_runs(trigger,started_at,status) VALUES(?,?,'running')",
                (trigger, iso()),
            )
            self.conn.commit()
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, stats: dict) -> None:
        with self.lock:
            self.conn.execute(
                "UPDATE scan_runs SET finished_at=?,status=?,stats=? WHERE id=?",
                (iso(), status, json.dumps(stats), run_id),
            )
            self.conn.commit()

    def last_runs(self, limit: int = 10) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM scan_runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["stats"] = json.loads(d["stats"]) if d.get("stats") else {}
            out.append(d)
        return out


db = Database()
