"""Macro desk: US yields and curve, credit, dollar, commodities, economy, calendar.

The news feed says what happened; this module says what the macro tape looks like.
Everything here is keyless and official where possible:

* US Treasury daily par yield curve (official close) — 3M/2Y/5Y/10Y/30Y yields
* FRED (`fredgraph.csv`, no key) — real yields, breakeven inflation, credit
  spreads, broad dollar, WTI/Brent/Henry Hub, claims, CPI, unemployment, payrolls
* CBOE — VIX close
* gold-api.com — spot gold and silver
* ForexFactory's public weekly JSON — upcoming US data releases

`fetch()` produces one JSON-serialisable snapshot; `desk_payload()` turns it into
the compact block the desk-note model sees, and `desk_line()` into the one-line
macro context archived with every note.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .util import iso, now_utc, parse_dt

# --------------------------------------------------------------------------- #
# What we pull and how often each kind of series actually changes
# --------------------------------------------------------------------------- #
FRED_SPECS: dict[str, dict[str, str]] = {
    "DFII10": {"label": "10Y real yield", "freq": "daily", "group": "rates"},
    "T10YIE": {"label": "10Y breakeven", "freq": "daily", "group": "rates"},
    "BAMLH0A0HYM2": {"label": "US high-yield OAS", "freq": "daily", "group": "credit"},
    "BAMLC0A0CM": {"label": "US IG OAS", "freq": "daily", "group": "credit"},
    "DTWEXBGS": {"label": "Broad dollar", "freq": "daily", "group": "credit"},
    "VIXCLS": {"label": "VIX (FRED)", "freq": "daily", "group": "credit"},
    "DCOILWTICO": {"label": "WTI spot", "freq": "daily", "group": "commodities"},
    "DCOILBRENTEU": {"label": "Brent spot", "freq": "daily", "group": "commodities"},
    "DHHNGSP": {"label": "Henry Hub gas", "freq": "daily", "group": "commodities"},
    "MORTGAGE30US": {"label": "30Y mortgage", "freq": "weekly", "group": "economy"},
    "ICSA": {"label": "Initial claims", "freq": "weekly", "group": "economy"},
    "CPIAUCSL": {"label": "CPI", "freq": "monthly", "group": "economy"},
    "CPILFESL": {"label": "Core CPI", "freq": "monthly", "group": "economy"},
    "UNRATE": {"label": "Unemployment", "freq": "monthly", "group": "economy"},
    "PAYEMS": {"label": "Nonfarm payrolls", "freq": "monthly", "group": "economy"},
    "DFEDTARL": {"label": "Fed funds lower", "freq": "daily", "group": "rates"},
    "DFEDTARU": {"label": "Fed funds upper", "freq": "daily", "group": "rates"},
}

# FRED truncates its CSV by start date; these windows cover the YoY/MoM maths.
FRED_START_DAYS = {"daily": 120, "weekly": 400, "monthly": 900}
FRED_TTL = {"daily": 15 * 60, "weekly": 60 * 60, "monthly": 6 * 60 * 60}

TREASURY_TTL = 15 * 60
VIX_TTL = 15 * 60
METALS_TTL = 5 * 60
CALENDAR_TTL = 30 * 60
QUOTE_TTL = 5 * 60

# Liquid proxies for the "markets" row of the macro tape.
QUOTE_TICKERS = ("SPY", "QQQ", "IWM", "TLT", "HYG", "UUP")
QUOTE_LABELS = {"SPY": "S&P 500", "QQQ": "Nasdaq 100", "IWM": "Russell 2000",
                "TLT": "20Y+ Treasuries", "HYG": "High yield", "UUP": "Dollar (UUP)"}

RATE_TENORS = (
    ("3 Mo", "3M", "3-month bill"),
    ("2 Yr", "2Y", "2-year Treasury"),
    ("5 Yr", "5Y", "5-year Treasury"),
    ("10 Yr", "10Y", "10-year Treasury"),
    ("30 Yr", "30Y", "30-year Treasury"),
)

GROUP_LABELS = {
    "rates": "Rates",
    "credit": "Risk & credit",
    "commodities": "Commodities",
    "economy": "Economy",
    "markets": "Markets",
}

_CACHE: dict[str, tuple[float, Any]] = {}
_CALENDAR_COOLDOWN_UNTIL = 0.0
_ET = ZoneInfo("America/New_York")  # US releases are conventionally quoted in ET


def clear_cache() -> None:
    global _CALENDAR_COOLDOWN_UNTIL
    _CACHE.clear()
    _CALENDAR_COOLDOWN_UNTIL = 0.0


async def _cached(key: str, ttl: int, force: bool,
                  factory: Callable[[], Any]) -> tuple[Any, bool]:
    """Return (value, freshly_fetched). A stale value beats no value on failure."""
    hit = _CACHE.get(key)
    now = time.time()
    if hit and not force and now - hit[0] <= ttl:
        return hit[1], False
    value = await factory()
    _CACHE[key] = (time.time(), value)
    return value, True


# --------------------------------------------------------------------------- #
# Small numeric helpers
# --------------------------------------------------------------------------- #
def _last(points: list[tuple[str, float | None]]) -> tuple[str | None, float | None]:
    for date, value in reversed(points or []):
        if value is not None:
            return date, value
    return None, None


def _last_before(points: list[tuple[str, float | None]], date: str
                 ) -> tuple[str | None, float | None]:
    for d, value in reversed(points or []):
        if value is not None and d < date:
            return d, value
    return None, None


def _yoy_at(points: list[tuple[str, float | None]], date: str) -> float | None:
    """Year-over-year % change for the observation on `date`."""
    _, current = next(((d, v) for d, v in points if d == date and v is not None), (None, None))
    if current is None:
        return None
    try:
        target = f"{int(date[:4]) - 1}{date[4:7]}"
    except ValueError:
        return None
    base = None
    for d, value in points:
        if value is None:
            continue
        if d[:7] == target:
            base = value
            break
    if base in (None, 0):
        return None
    return (current / base - 1.0) * 100.0


def _dir(change: float | None, eps: float = 0.0) -> str:
    if change is None:
        return "flat"
    if change > eps:
        return "up"
    if change < -eps:
        return "down"
    return "flat"


def _count(value: Any) -> int:
    if isinstance(value, (list, tuple, dict, set)):
        return len(value)
    return 1 if value else 0


def _calendar_status(sources: list[dict]) -> str:
    """ok | stale | unavailable | off — so the note never claims "no events" when blind."""
    for s in sources:
        if s.get("id") == "macro:calendar":
            if s.get("ok"):
                return "ok"
            return "stale" if s.get("stale") else "unavailable"
    return "off"


def _metric(key: str, label: str, kind: str, value: Any, chg: float | None,
            display: str, chg_display: str | None, *, chg_unit: str = "",
            asof: str | None = None, source: str = "", hint: str = "",
            dir_eps: float = 0.0) -> dict:
    return {
        "key": key, "label": label, "kind": kind,
        "value": None if value is None else round(float(value), 4),
        "chg": None if chg is None else round(float(chg), 2),
        "chg_unit": chg_unit,
        "display": display,
        "chg_display": chg_display,
        "dir": _dir(chg, dir_eps),
        "asof": asof,
        "source": source,
        "hint": hint,
    }


def _yield_metric(key: str, label: str, value: float | None, prev: float | None,
                  asof: str | None, note: str = "") -> dict | None:
    if value is None:
        return None
    bp = None if prev is None else (value - prev) * 100.0
    display = f"{value:.2f}%"
    chg_display = None if bp is None else f"{bp:+.0f}bp"
    hint = f"{note or label} · {display}"
    if bp is not None:
        hint += f" · {bp:+.0f}bp vs prior close"
    hint += f" · {asof or ''} · US Treasury"
    return _metric(key, label, "yield", value, bp, display, chg_display,
                   chg_unit="bp", asof=asof, source="US Treasury", hint=hint, dir_eps=0.05)


def _spread_metric(key: str, label: str, bp_value: float | None, prev_bp: float | None,
                    asof: str | None, note: str = "") -> dict | None:
    if bp_value is None:
        return None
    chg = None if prev_bp is None else bp_value - prev_bp
    display = f"{bp_value:+.0f}bp"
    chg_display = None if chg is None else f"{chg:+.0f}bp"
    hint = f"{note or label} · {display}"
    if chg is not None:
        hint += f" · {chg:+.0f}bp vs prior close"
    hint += f" · {asof or ''} · computed from US Treasury curve"
    return _metric(key, label, "spread", bp_value / 100.0, chg, display, chg_display,
                   chg_unit="bp", asof=asof, source="US Treasury", hint=hint, dir_eps=0.5)


def _level_metric(key: str, label: str, value: float | None, prev: float | None,
                  *, decimals: int = 2, prefix: str = "", suffix: str = "",
                  asof: str | None = None, source: str = "FRED", note: str = "",
                  chg_decimals: int | None = None) -> dict | None:
    if value is None:
        return None
    chg = None if prev is None else value - prev
    display = f"{prefix}{value:,.{decimals}f}{suffix}"
    chg_display = None if chg is None else f"{chg:+,.{chg_decimals or decimals}f}"
    hint = f"{note or label} · {display}"
    if chg_display:
        hint += f" · {chg_display} vs prior observation"
    hint += f" · {asof or ''} · {source}"
    return _metric(key, label, "level", value, chg, display, chg_display,
                   asof=asof, source=source, hint=hint)


def _pct_metric(key: str, label: str, value: float | None, prev: float | None,
                *, decimals: int = 2, asof: str | None = None, source: str = "FRED",
                note: str = "") -> dict | None:
    if value is None:
        return None
    chg = None if prev is None else value - prev
    display = f"{value:.{decimals}f}%"
    chg_display = None if chg is None else f"{chg:+.{decimals}f}pp"
    hint = f"{note or label} · {display}"
    if chg_display:
        hint += f" · {chg_display} vs prior observation"
    hint += f" · {asof or ''} · {source}"
    return _metric(key, label, "pct", value, chg, display, chg_display,
                   chg_unit="pp", asof=asof, source=source, hint=hint)


def _oas_metric(key: str, label: str, value: float | None, prev: float | None,
                asof: str | None) -> dict | None:
    if value is None:
        return None
    bp = value * 100.0
    chg = None if prev is None else bp - prev * 100.0
    display = f"{bp:.0f}bp"
    chg_display = None if chg is None else f"{chg:+.0f}bp"
    hint = f"{label} · {display}"
    if chg_display:
        hint += f" · {chg_display} vs prior observation"
    hint += f" · {asof or ''} · FRED"
    # value kept in percent (3.40 == 3.40%) so the payload matches the source
    return _metric(key, label, "oas", value, chg, display, chg_display,
                   chg_unit="bp", asof=asof, source="FRED", hint=hint, dir_eps=0.5)


def _pct_bp_metric(key: str, label: str, value: float | None, prev: float | None,
                   *, asof: str | None = None, source: str = "FRED",
                   note: str = "") -> dict | None:
    """A rate quoted in percent whose day-over-day move reads best in bp."""
    if value is None:
        return None
    bp = None if prev is None else (value - prev) * 100.0
    display = f"{value:.2f}%"
    chg_display = None if bp is None else f"{bp:+.0f}bp"
    hint = f"{note or label} · {display}"
    if chg_display:
        hint += f" · {chg_display} vs prior observation"
    hint += f" · {asof or ''} · {source}"
    return _metric(key, label, "pct", value, bp, display, chg_display,
                   chg_unit="bp", asof=asof, source=source, hint=hint, dir_eps=0.5)


def _usd_metric(key: str, label: str, value: float | None, prev: float | None,
                *, decimals: int = 2, asof: str | None = None, source: str = "",
                note: str = "") -> dict | None:
    if value is None:
        return None
    pct = None if not prev else (value / prev - 1.0) * 100.0
    display = f"${value:,.{decimals}f}"
    chg_display = None if pct is None else f"{pct:+.1f}%"
    hint = f"{note or label} · {display}"
    if chg_display:
        hint += f" · {chg_display} vs prior observation"
    hint += f" · {asof or ''} · {source}"
    return _metric(key, label, "usd", value, pct, display, chg_display,
                   chg_unit="%", asof=asof, source=source, hint=hint)


def _spot_metric(key: str, label: str, value: float | None, at: str | None,
                 source: str = "gold-api.com") -> dict | None:
    if value is None:
        return None
    display = f"${value:,.2f}"
    asof = (at or "")[:10] or None
    return _metric(key, label, "usd", value, None, display, None,
                   asof=asof, source=source, hint=f"{label} spot · {display} · {asof or ''} · {source}")


def _k_metric(key: str, label: str, value_k: float | None, chg_k: float | None,
              *, asof: str | None = None, signed: bool = False,
              delta_label: str = "", source: str = "FRED",
              note: str = "") -> dict | None:
    if value_k is None:
        return None
    display = f"{value_k:+,.0f}k" if signed else f"{value_k:,.0f}k"
    chg_display = None if chg_k is None else f"{delta_label}{chg_k:+,.0f}k"
    hint = f"{note or label} · {display}"
    if chg_display:
        hint += f" · {chg_display} vs prior observation"
    hint += f" · {asof or ''} · {source}"
    return _metric(key, label, "k", value_k, chg_k, display, chg_display,
                   chg_unit="k", asof=asof, source=source, hint=hint)


def _text_metric(key: str, label: str, text: str, asof: str | None,
                  source: str, note: str = "") -> dict:
    return _metric(key, label, "text", None, None, text, None,
                   asof=asof, source=source, hint=f"{note or label} · {text} · {asof or ''} · {source}")


# --------------------------------------------------------------------------- #
# Snapshot builders (pure functions — unit-testable without a network)
# --------------------------------------------------------------------------- #
def build_rates(treasury: list[dict], fred: dict[str, list]) -> list[dict]:
    metrics: list[dict] = []
    latest = treasury[0] if treasury else {}
    prev = treasury[1] if len(treasury) > 1 else {}
    asof = latest.get("date")
    for tenor, label, note in RATE_TENORS:
        m = _yield_metric(f"us{label.lower()}", label, latest.get(tenor), prev.get(tenor),
                          asof, note)
        if m:
            metrics.append(m)

    if latest.get("10 Yr") is not None and latest.get("2 Yr") is not None:
        now_bp = (latest["10 Yr"] - latest["2 Yr"]) * 100.0
        prev_bp = None
        if prev.get("10 Yr") is not None and prev.get("2 Yr") is not None:
            prev_bp = (prev["10 Yr"] - prev["2 Yr"]) * 100.0
        m = _spread_metric("curve_2s10s", "2s10s", now_bp, prev_bp, asof,
                           "10Y minus 2Y curve")
        if m:
            metrics.append(m)

    if latest.get("10 Yr") is not None and latest.get("3 Mo") is not None:
        now_bp = (latest["10 Yr"] - latest["3 Mo"]) * 100.0
        prev_bp = None
        if prev.get("10 Yr") is not None and prev.get("3 Mo") is not None:
            prev_bp = (prev["10 Yr"] - prev["3 Mo"]) * 100.0
        m = _spread_metric("curve_3m10y", "3m10y", now_bp, prev_bp, asof,
                           "10Y minus 3M curve")
        if m:
            metrics.append(m)

    for series, key, label, note in (
        ("DFII10", "real_10y", "10Y real", "10Y TIPS real yield"),
        ("T10YIE", "breakeven_10y", "10Y BE", "10Y breakeven inflation"),
    ):
        d, v = _last(fred.get(series, []))
        _, pv = _last_before(fred.get(series, []), d) if d else (None, None)
        m = _pct_bp_metric(key, label, v, pv, asof=d, note=note)
        if m:
            metrics.append(m)

    # Fed funds target range comes as two daily series
    d_low, low = _last(fred.get("DFEDTARL", []))
    _, high = _last(fred.get("DFEDTARU", []))
    if low is not None and high is not None:
        metrics.append(_text_metric(
            "fed_funds", "Fed funds", f"{low:.2f}–{high:.2f}%", d_low,
            "FRED", "Fed funds target range"))
    return metrics


def build_credit(fred: dict[str, list], vix_points: list[tuple[str, float]],
                 vix_source: str = "CBOE") -> list[dict]:
    metrics: list[dict] = []
    d, v = _last(vix_points)
    _, pv = _last_before(vix_points, d) if d else (None, None)
    m = _level_metric("vix", "VIX", v, pv, decimals=2, asof=d, source=vix_source,
                      note="CBOE volatility index")
    if m:
        metrics.append(m)

    for series, key, label in (
        ("BAMLH0A0HYM2", "hy_oas", "HY OAS"),
        ("BAMLC0A0CM", "ig_oas", "IG OAS"),
    ):
        d, v = _last(fred.get(series, []))
        _, pv = _last_before(fred.get(series, []), d) if d else (None, None)
        m = _oas_metric(key, label, v, pv, d)
        if m:
            metrics.append(m)

    d, v = _last(fred.get("DTWEXBGS", []))
    _, pv = _last_before(fred.get("DTWEXBGS", []), d) if d else (None, None)
    m = _level_metric("broad_dollar", "Broad USD", v, pv, decimals=2, asof=d,
                      note="Trade-weighted broad dollar index")
    if m:
        metrics.append(m)
    return metrics


def build_commodities(fred: dict[str, list], metals: dict[str, dict]) -> list[dict]:
    metrics: list[dict] = []
    for series, key, label in (
        ("DCOILWTICO", "wti", "WTI"),
        ("DCOILBRENTEU", "brent", "Brent"),
        ("DHHNGSP", "natgas", "Nat gas"),
    ):
        d, v = _last(fred.get(series, []))
        _, pv = _last_before(fred.get(series, []), d) if d else (None, None)
        m = _usd_metric(key, label, v, pv, asof=d, source="FRED", note=f"{label} spot")
        if m:
            metrics.append(m)

    for symbol, key, label in (("XAU", "gold", "Gold"), ("XAG", "silver", "Silver")):
        spot = metals.get(symbol) or {}
        m = _spot_metric(key, label, spot.get("price"), spot.get("at"))
        if m:
            metrics.append(m)
    return metrics


def build_economy(fred: dict[str, list]) -> list[dict]:
    metrics: list[dict] = []

    for series, key, label, note in (
        ("CPIAUCSL", "cpi_yoy", "CPI y/y", "Headline CPI, year over year"),
        ("CPILFESL", "core_cpi_yoy", "Core CPI y/y", "Core CPI, year over year"),
    ):
        points = fred.get(series, [])
        d, _ = _last(points)
        if not d:
            continue
        yoy = _yoy_at(points, d)
        d0, _ = _last_before(points, d)
        prev_yoy = _yoy_at(points, d0) if d0 else None
        m = _pct_metric(key, label, yoy, prev_yoy, decimals=1, asof=d, note=note)
        if m:
            metrics.append(m)

    points = fred.get("UNRATE", [])
    d, v = _last(points)
    _, pv = _last_before(points, d) if d else (None, None)
    m = _pct_metric("unemployment", "Unemployment", v, pv, decimals=1, asof=d,
                    note="US unemployment rate")
    if m:
        metrics.append(m)

    points = fred.get("PAYEMS", [])
    d1, v1 = _last(points)
    d0, v0 = _last_before(points, d1) if d1 else (None, None)
    if d1 and v1 is not None and d0 and v0 is not None:
        mom = v1 - v0
        prev_mom = None
        _, v_2 = _last_before(points, d0)
        if v_2 is not None:
            prev_mom = v0 - v_2
        chg = None if prev_mom is None else mom - prev_mom
        m = _k_metric("payrolls", "Payrolls", mom, chg, asof=d1, signed=True,
                      delta_label="Δ", note="Nonfarm payrolls, monthly change")
        if m:
            metrics.append(m)

    points = fred.get("ICSA", [])
    d, v = _last(points)
    _, pv = _last_before(points, d) if d else (None, None)
    if v is not None:
        chg = None if pv is None else (v - pv) / 1000.0
        m = _k_metric("claims", "Claims", v / 1000.0, chg, asof=d,
                      note="Initial jobless claims, weekly")
        if m:
            metrics.append(m)

    points = fred.get("MORTGAGE30US", [])
    d, v = _last(points)
    _, pv = _last_before(points, d) if d else (None, None)
    m = _pct_metric("mortgage30", "Mortgage 30Y", v, pv, decimals=2, asof=d,
                    note="30-year fixed mortgage rate")
    if m:
        metrics.append(m)
    return metrics


def build_markets(quotes: dict[str, dict]) -> list[dict]:
    metrics: list[dict] = []
    for ticker in QUOTE_TICKERS:
        q = quotes.get(ticker) or {}
        price = q.get("price")
        if price is None:
            continue
        pct = q.get("change_pct")
        display = f"${price:,.2f}"
        chg_display = None if pct is None else f"{pct:+.2f}%"
        hint = f"{QUOTE_LABELS.get(ticker, ticker)} ({ticker}) · {display}"
        if chg_display:
            hint += f" · {chg_display} today"
        hint += " · stockanalysis.com"
        metrics.append(_metric(ticker.lower(), ticker, "quote", price, pct,
                               display, chg_display, chg_unit="%",
                               source="stockanalysis.com", hint=hint))
    return metrics


def build_calendar(raw: list[dict], *, days: int = 7, limit: int = 8,
                   now=None) -> list[dict]:
    """Upcoming US high/medium-impact releases, soonest first."""
    now = now or now_utc()
    out: list[dict] = []
    for row in raw or []:
        if str(row.get("country") or "").upper() != "USD":
            continue
        impact = str(row.get("impact") or "").strip().lower()
        if impact not in ("high", "medium"):
            continue
        dt = parse_dt(row.get("date"))
        if not dt:
            continue
        hours = (dt - now).total_seconds() / 3600.0
        if hours < -1 or hours > days * 24:
            continue
        local = dt.astimezone(_ET)
        out.append({
            "title": str(row.get("title") or "").strip()[:80],
            "when_iso": iso(dt),
            "when_label": local.strftime("%a %d %b %H:%M %Z"),
            "impact": impact,
            "forecast": str(row.get("forecast") or "").strip()[:24],
            "previous": str(row.get("previous") or "").strip()[:24],
            "in_hours": round(hours, 1),
            "soon": 0 <= hours <= 24,
        })
    out.sort(key=lambda e: e["when_iso"])
    return out[:limit]


def _find(metrics: list[dict], key: str) -> dict | None:
    for m in metrics:
        if m["key"] == key:
            return m
    return None


def _line_metric(metrics: list[dict], key: str) -> str | None:
    m = _find(metrics, key)
    if not m:
        return None
    return m["display"] + (f" ({m['chg_display']})" if m.get("chg_display") else "")


def desk_line(snapshot: dict) -> str:
    """One-line tape summary archived alongside every desk note."""
    if not snapshot:
        return ""
    metrics = [m for g in snapshot.get("groups", []) for m in g.get("metrics", [])]
    bits = []
    for key, label in (("us10y", "10Y"), ("us2y", "2Y"), ("curve_2s10s", "2s10s"),
                       ("vix", "VIX"), ("hy_oas", "HY OAS"), ("broad_dollar", "USD"),
                       ("wti", "WTI"), ("gold", "Gold")):
        text = _line_metric(metrics, key)
        if text:
            bits.append(f"{label} {text}")
    cal = (snapshot.get("calendar") or [])
    if cal:
        nxt = cal[0]
        bits.append(f"next {nxt['when_label']} {nxt['title']}")
    return " · ".join(bits)


def desk_payload(snapshot: dict) -> dict | None:
    """Compact, string-formatted macro block for the desk-note prompt."""
    if not snapshot:
        return None
    metrics = [m for g in snapshot.get("groups", []) for m in g.get("metrics", [])]
    by_group: dict[str, dict[str, str]] = {"rates": {}, "credit": {}, "commodities": {},
                                          "economy": {}, "markets": {}}
    for group in snapshot.get("groups", []):
        bucket = by_group.setdefault(group.get("key", "other"), {})
        for m in group.get("metrics", []):
            bucket[m["label"]] = m["display"] + (
                f" ({m['chg_display']})" if m.get("chg_display") else "")
    payload = {
        "asof": snapshot.get("as_of"),
        **{k: v for k, v in by_group.items() if v},
        "calendar": [
            "{} {}{}".format(
                c["when_label"], c["title"],
                f" (forecast {c['forecast']})" if c.get("forecast") else "")
            for c in (snapshot.get("calendar") or [])[:5]
        ],
    }
    status = snapshot.get("calendar_status")
    if not status:
        # snapshots written by older versions may predate calendar_status
        status = ("unavailable"
                  if any(e.get("source") == "macro:calendar" for e in snapshot.get("errors") or [])
                  else "ok")
    payload["calendar_status"] = status
    if not payload["calendar"]:
        if status == "ok":
            payload["calendar_note"] = "no US high/medium-impact releases left this week"
        elif status == "off":
            payload["calendar_note"] = "release calendar disabled"
        else:
            payload["calendar_note"] = "release calendar unavailable"
    return payload


def render_text(snapshot: dict) -> str:
    """Terminal rendering (also handy for logs)."""
    if not snapshot:
        return "no macro snapshot"
    lines = [f"MACRO DESK · as of {snapshot.get('as_of')} · fetched {snapshot.get('at')}"]
    for group in snapshot.get("groups", []):
        bits = []
        for m in group.get("metrics", []):
            bits.append(f"{m['label']} {m['display']}"
                        + (f" ({m['chg_display']})" if m.get("chg_display") else ""))
        lines.append(f"{group['label']:<15} " + "   ".join(bits))
    for c in snapshot.get("calendar", []):
        lines.append(f"{'Calendar':<15} {c['when_label']} {c['title']}"
                     + (f" (forecast {c['forecast']})" if c.get("forecast") else ""))
    if snapshot.get("errors"):
        lines.append("errors: " + "; ".join(f"{e['source']}: {e['error']}"
                                            for e in snapshot["errors"]))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Live fetch
# --------------------------------------------------------------------------- #
async def fetch(src, *, calendar: bool = True, force: bool = False) -> dict:
    """Pull every macro source in parallel and return one snapshot.

    `src` is a `Sources` instance. Individual failures degrade to the metrics
    that did arrive; the caller only sees a raised exception when nothing works.
    """
    errors: list[dict] = []
    sources: list[dict] = []

    async def task(sid: str, name: str, ttl: int, factory, counter=None) -> Any:
        t0 = time.perf_counter()
        try:
            value, fresh = await _cached(sid, ttl, force, factory)
            sources.append({"id": sid, "name": name, "ok": True,
                            "ms": int((time.perf_counter() - t0) * 1000),
                            "items": counter(value) if counter else _count(value),
                            "fresh": fresh})
            return value
        except Exception as exc:  # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"[:180]
            stale = _CACHE.get(sid)
            sources.append({"id": sid, "name": name, "ok": False,
                            "ms": int((time.perf_counter() - t0) * 1000),
                            "items": _count(stale[1]) if stale else 0,
                            "fresh": False, "stale": bool(stale), "error": err})
            errors.append({"source": sid, "error": err})
            # a stale reading beats a blank tape: the panel and the note keep working
            return stale[1] if stale else None

    async def fred_task() -> dict[str, list]:
        # FRED is happy with a handful of concurrent requests but throttles bursts
        sem = asyncio.Semaphore(4)

        async def one(series_id: str):
            spec = FRED_SPECS[series_id]
            start = (now_utc() - timedelta(days=FRED_START_DAYS[spec["freq"]])
                     ).date().isoformat()
            async with sem:
                value, fresh = await _cached(
                    f"fred:{series_id}", FRED_TTL[spec["freq"]], force,
                    lambda: src.fred_series(series_id, start=start))
            return series_id, value, fresh

        results = await asyncio.gather(*(one(s) for s in FRED_SPECS), return_exceptions=True)
        out: dict[str, list] = {}
        failures: list[str] = []
        for series_id, result in zip(FRED_SPECS, results):
            if isinstance(result, Exception):
                failures.append(f"{series_id} ({type(result).__name__})")
                errors.append({"source": f"fred:{series_id}",
                               "error": f"{type(result).__name__}: {result}"[:160]})
                continue
            out[series_id] = result[1]
        if not out:
            raise ValueError("all FRED series failed: " + ", ".join(failures[:4]))
        return out

    async def quotes_task() -> dict[str, dict]:
        results = await asyncio.gather(
            *(src.stockanalysis_quote(t) for t in QUOTE_TICKERS), return_exceptions=True)
        out: dict[str, dict] = {}
        for ticker, quote in zip(QUOTE_TICKERS, results):
            if isinstance(quote, dict) and quote.get("price") is not None:
                out[ticker] = quote
        if not out:
            raise ValueError("no macro market quotes")
        return out

    async def calendar_task() -> list[dict]:
        global _CALENDAR_COOLDOWN_UNTIL
        if time.time() < _CALENDAR_COOLDOWN_UNTIL:
            raise RuntimeError("calendar feed rate-limited; cooling down")
        try:
            return await src.economic_calendar()
        except Exception as exc:  # noqa: BLE001
            response = getattr(exc, "response", None)
            if response is not None and getattr(response, "status_code", None) == 429:
                try:
                    wait = max(60, int(response.headers.get("retry-after") or 300))
                except (TypeError, ValueError):
                    wait = 300
                _CALENDAR_COOLDOWN_UNTIL = time.time() + wait
            raise

    treasury, fred, vix, metals, quotes, calendar_raw = await asyncio.gather(
        task("macro:treasury", "Macro · US Treasury curve", TREASURY_TTL,
             lambda: src.treasury_yields(), counter=len),
        task("macro:fred", "Macro · FRED series", FRED_TTL["daily"], fred_task,
             counter=lambda v: sum(len(points) for points in v.values())),
        task("macro:vix", "Macro · CBOE VIX", VIX_TTL, lambda: src.cboe_vix(),
             counter=len),
        task("macro:metals", "Macro · spot metals", METALS_TTL,
             lambda: src.metals_spot(), counter=len),
        task("macro:quotes", "Macro · market proxies", QUOTE_TTL, quotes_task,
             counter=len),
        task("macro:calendar", "Macro · US data calendar", CALENDAR_TTL, calendar_task,
             counter=len) if calendar else _noop(),
    )

    treasury = treasury or []
    fred = fred or {}
    metals = metals or {}
    quotes = quotes or {}

    # VIX: CBOE close first, FRED's daily close as a fallback
    vix_points = [(d, v) for d, v in (vix or []) if v is not None]
    vix_source = "CBOE"
    if not vix_points:
        fred_vix = fred.get("VIXCLS", [])
        vix_points = [(d, v) for d, v in fred_vix if v is not None]
        vix_source = "FRED"

    groups = [
        {"key": "rates", "label": GROUP_LABELS["rates"], "metrics": build_rates(treasury, fred)},
        {"key": "credit", "label": GROUP_LABELS["credit"],
         "metrics": build_credit(fred, vix_points, vix_source)},
        {"key": "commodities", "label": GROUP_LABELS["commodities"],
         "metrics": build_commodities(fred, metals)},
        {"key": "economy", "label": GROUP_LABELS["economy"], "metrics": build_economy(fred)},
        {"key": "markets", "label": GROUP_LABELS["markets"], "metrics": build_markets(quotes)},
    ]
    groups = [g for g in groups if g["metrics"]]
    if not groups:
        raise ValueError("no macro data available")

    latest_fred_date = ""
    for points in fred.values():
        d, _ = _last(points)
        if d and d > latest_fred_date:
            latest_fred_date = d

    snapshot = {
        "at": iso(),
        "as_of": (treasury[0]["date"] if treasury else
                  latest_fred_date or now_utc().date().isoformat()),
        "groups": groups,
        "calendar": build_calendar(calendar_raw or []),
        "calendar_status": _calendar_status(sources),
        "dates": {
            "yields": treasury[0]["date"] if treasury else None,
            "fred": latest_fred_date or None,
            "vix": vix_points[-1][0] if vix_points else None,
            "metals": (next((v.get("at") for v in metals.values() if v.get("at")), None) or "")[:10] or None,
            "quotes": iso()[:10] if quotes else None,
        },
        "sources": sources,
        "errors": errors,
    }
    snapshot["line"] = desk_line(snapshot)
    return snapshot


async def _noop():
    return None


# --------------------------------------------------------------------------- #
# Desk-note change detection
# --------------------------------------------------------------------------- #
def signature(snapshot: dict) -> dict:
    """The few numbers that make a desk note worth rewriting."""
    if not snapshot:
        return {}
    metrics = [m for g in snapshot.get("groups", []) for m in g.get("metrics", [])]
    sig: dict[str, float] = {}
    for key in ("us10y", "us2y", "curve_2s10s", "vix", "hy_oas", "broad_dollar"):
        m = _find(metrics, key)
        if m and m.get("value") is not None:
            sig[key] = float(m["value"])
    return sig


def moved(previous: dict, current: dict, *, bp: float = 4.0, vix: float = 1.0,
          pct: float = 0.5) -> bool:
    """True when the tape moved enough since the note was written."""
    if not previous or not current:
        return False
    for key in ("us10y", "us2y", "curve_2s10s"):
        a, b = previous.get(key), current.get(key)
        if a is not None and b is not None and abs(b - a) * 100.0 >= bp:
            return True
    a, b = previous.get("vix"), current.get("vix")
    if a is not None and b is not None and abs(b - a) >= vix:
        return True
    for key in ("hy_oas", "broad_dollar"):
        a, b = previous.get(key), current.get(key)
        if a is not None and b is not None and abs(b - a) >= pct:
            return True
    return False


# --------------------------------------------------------------------------- #
# CLI helper
# --------------------------------------------------------------------------- #
def as_json(snapshot: dict) -> str:
    return json.dumps(snapshot, indent=2, ensure_ascii=False)
