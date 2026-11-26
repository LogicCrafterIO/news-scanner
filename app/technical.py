"""Technical analysis of daily price action.

Turns a year of daily OHLCV bars into a structured, side-aware read of a ticker:
trend structure, momentum, volatility regime, participation, relative strength,
support/resistance levels, a composite bias score, and a plain-English narrative.

Everything here is deterministic and dependency-free on purpose: the numbers are
computed locally, so the technical read still works with no LLM key. An optional
LLM pass (see ``llm.tech_read``) can add a second opinion, but it never replaces
the computed facts.

Vocabulary used across the app:

    trend      strong_up | up | sideways | down | strong_down
    bias       bullish | bearish | neutral        (from the -100…+100 score)
    setup      breakout | uptrend_pullback | extended_uptrend | breakdown |
               oversold_downtrend | downtrend_bounce | coiled_range | range |
               trending | unknown
    side       long | short | None                (portfolio context)
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Iterable

from .util import clean_text, iso, now_utc, trim_sentence

TREND_LABELS = {
    "strong_up": "strong uptrend", "up": "uptrend", "sideways": "sideways",
    "down": "downtrend", "strong_down": "strong downtrend",
}
SETUP_LABELS = {
    "breakout": "breakout",
    "uptrend_pullback": "uptrend pullback",
    "extended_uptrend": "extended uptrend",
    "breakdown": "breakdown",
    "oversold_downtrend": "oversold downtrend",
    "downtrend_bounce": "downtrend bounce",
    "coiled_range": "coiled range",
    "range": "range",
    "trending": "trending",
    "unknown": "insufficient data",
}
ALIGN_LABELS = {
    "supportive": "with the position",
    "mixed": "mixed",
    "hostile": "against the position",
}


# --------------------------------------------------------------------------- #
# indicator maths — each helper returns a list aligned to the input length,
# with None for warm-up periods.
# --------------------------------------------------------------------------- #
def sma(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    window: list[float] = []
    for i, v in enumerate(values):
        if v is None:
            window = []
            continue
        window.append(v)
        if len(window) > n:
            window.pop(0)
        if len(window) == n:
            out[i] = sum(window) / n
    return out


def ema(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < n:
        return out
    seed = sum(values[:n]) / n
    out[n - 1] = seed
    k = 2.0 / (n + 1)
    for i in range(n, len(values)):
        prev = out[i - 1]
        out[i] = values[i] * k + (prev or 0.0) * (1 - k)
    return out


def rsi(closes: list[float], n: int = 14) -> list[float | None]:
    """Wilder's RSI."""
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / n, losses / n

    def value(up: float, down: float) -> float:
        if down == 0:
            return 100.0
        rs = up / down
        return 100.0 - 100.0 / (1.0 + rs)

    out[n] = value(ag, al)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0.0)) / n
        al = (al * (n - 1) + max(-d, 0.0)) / n
        out[i] = value(ag, al)
    return out


def true_ranges(highs: list[float], lows: list[float], closes: list[float]) -> list[float]:
    out = [highs[0] - lows[0]] if highs else []
    for i in range(1, len(closes)):
        pc = closes[i - 1]
        out.append(max(highs[i] - lows[i], abs(highs[i] - pc), abs(lows[i] - pc)))
    return out


def atr(highs: list[float], lows: list[float], closes: list[float],
        n: int = 14) -> list[float | None]:
    tr = true_ranges(highs, lows, closes)
    out: list[float | None] = [None] * len(closes)
    if len(tr) < n:
        return out
    prev = sum(tr[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(tr)):
        prev = (prev * (n - 1) + tr[i]) / n
        out[i] = prev
    return out


def macd(closes: list[float], fast: int = 12, slow: int = 26,
         signal: int = 9) -> tuple[list[float | None], list[float | None], list[float | None]]:
    ef, es = ema(closes, fast), ema(closes, slow)
    line = [None if ef[i] is None or es[i] is None else ef[i] - es[i]
            for i in range(len(closes))]
    sig: list[float | None] = [None] * len(closes)
    points = [(i, v) for i, v in enumerate(line) if v is not None]
    if len(points) >= signal:
        smoothed = ema([v for _, v in points], signal)
        for j, (i, _v) in enumerate(points):
            sig[i] = smoothed[j]
    hist = [None if line[i] is None or sig[i] is None else line[i] - sig[i]
            for i in range(len(closes))]
    return line, sig, hist


def bollinger(closes: list[float], n: int = 20,
              k: float = 2.0) -> tuple[list[float | None], ...]:
    mid = sma(closes, n)
    upper: list[float | None] = [None] * len(closes)
    lower: list[float | None] = [None] * len(closes)
    width: list[float | None] = [None] * len(closes)
    for i in range(n - 1, len(closes)):
        window = closes[i - n + 1: i + 1]
        m = mid[i]
        if m is None:
            continue
        sd = math.sqrt(sum((x - m) ** 2 for x in window) / n)
        upper[i] = m + k * sd
        lower[i] = m - k * sd
        width[i] = (upper[i] - lower[i]) / m * 100 if m else None
    return mid, upper, lower, width


def stochastic(highs: list[float], lows: list[float], closes: list[float],
               n: int = 14, d: int = 3) -> tuple[list[float | None], list[float | None]]:
    k_line: list[float | None] = [None] * len(closes)
    for i in range(n - 1, len(closes)):
        hh = max(highs[i - n + 1: i + 1])
        ll = min(lows[i - n + 1: i + 1])
        k_line[i] = 100.0 * (closes[i] - ll) / (hh - ll) if hh > ll else 50.0
    d_line = sma([v for v in k_line], d)  # None-aware by construction
    return k_line, d_line


def adx(highs: list[float], lows: list[float], closes: list[float],
        n: int = 14) -> tuple[float | None, float | None, float | None]:
    """Wilder's ADX with +DI / -DI at the last bar."""
    if len(closes) < 2 * n + 1:
        return None, None, None
    tr = true_ranges(highs, lows, closes)
    plus_dm = [0.0] * len(closes)
    minus_dm = [0.0] * len(closes)
    for i in range(1, len(closes)):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm[i] = up if (up > down and up > 0) else 0.0
        minus_dm[i] = down if (down > up and down > 0) else 0.0

    tr_s = sum(tr[1: n + 1])
    pdm_s = sum(plus_dm[1: n + 1])
    mdm_s = sum(minus_dm[1: n + 1])
    dxs: list[float] = []

    def di() -> tuple[float, float, float]:
        if tr_s == 0:
            return 0.0, 0.0, 0.0
        pdi = 100.0 * pdm_s / tr_s
        mdi = 100.0 * mdm_s / tr_s
        denom = pdi + mdi
        return pdi, mdi, (100.0 * abs(pdi - mdi) / denom if denom else 0.0)

    pdi, mdi, dx = di()
    dxs.append(dx)
    for i in range(n + 1, len(closes)):
        tr_s = tr_s - tr_s / n + tr[i]
        pdm_s = pdm_s - pdm_s / n + plus_dm[i]
        mdm_s = mdm_s - mdm_s / n + minus_dm[i]
        pdi, mdi, dx = di()
        dxs.append(dx)
    if not dxs:
        return None, None, None
    adx_v = sum(dxs[:n]) / min(n, len(dxs))
    for x in dxs[n:]:
        adx_v = (adx_v * (n - 1) + x) / n
    return adx_v, pdi, mdi


def obv(closes: list[float], volumes: list[float]) -> list[float]:
    out = [0.0] * len(closes)
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            out[i] = out[i - 1] + volumes[i]
        elif closes[i] < closes[i - 1]:
            out[i] = out[i - 1] - volumes[i]
        else:
            out[i] = out[i - 1]
    return out


def percentile_rank(values: Iterable[float | None], x: float | None) -> float | None:
    clean = [v for v in values if v is not None]
    if not clean or x is None:
        return None
    return 100.0 * sum(1 for v in clean if v <= x) / len(clean)


def swing_points(highs: list[float], lows: list[float], left: int = 3,
                 right: int = 3) -> list[dict]:
    """Fractal swing highs/lows: local extremes with `left`/`right` neighbours."""
    out: list[dict] = []
    for i in range(left, len(highs) - right):
        h = highs[i]
        if h >= max(highs[i - left: i + right + 1]):
            out.append({"i": i, "kind": "high", "price": h})
        l = lows[i]
        if l <= min(lows[i - left: i + right + 1]):
            out.append({"i": i, "kind": "low", "price": l})
    return out


def _last(series: list[float | None]) -> float | None:
    return series[-1] if series else None


def _r(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(float(value), digits)


def _pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:+.1f}%"


def _num(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:,.{digits}f}"


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


# --------------------------------------------------------------------------- #
# quote merge — make the latest quote part of the daily series when possible
# --------------------------------------------------------------------------- #
def merge_quote(bars: list[dict], quote: dict | None) -> tuple[list[dict], bool]:
    """Append/replace the last bar with the live quote.

    Returns (bars, intraday). The input list is never mutated.
    """
    if not bars:
        return [], False
    price = (quote or {}).get("price")
    if not price or price <= 0:
        return [dict(b) for b in bars], False
    out = [dict(b) for b in bars]
    qdate = (quote.get("bar_date") or (quote.get("asof_iso") or "")[:10] or "")
    last = out[-1]
    if qdate and qdate > last["t"]:
        out.append({
            "t": qdate, "o": quote.get("open") or price,
            "h": quote.get("high") or price, "l": quote.get("low") or price,
            "c": float(price), "v": int(quote.get("volume") or 0),
        })
        return out, True
    if qdate and qdate == last["t"]:
        state = str(quote.get("market_state") or "").lower()
        live_session = state in ("open", "pre-market", "premarket", "after-hours",
                                 "after hours", "post-market")
        intraday = abs(float(price) - float(last["c"])) > 1e-6 or live_session
        last["c"] = float(price)
        if quote.get("high"):
            last["h"] = max(last["h"], float(quote["high"]))
        if quote.get("low"):
            last["l"] = min(last["l"], float(quote["low"]))
        if quote.get("open"):
            last["o"] = float(quote["open"])
        if quote.get("volume"):
            last["v"] = int(quote["volume"])
        return out, intraday
    return out, False


def _returns_from_dates(closes: dict[str, float], n: int) -> float | None:
    dates = sorted(closes)
    if len(dates) <= n:
        return None
    a, b = closes[dates[-1 - n]], closes[dates[-1]]
    return (b / a - 1.0) * 100.0 if a else None


def relative_strength(bars: list[dict], bench_bars: list[dict] | None) -> dict:
    """Return relative performance vs a benchmark over 20/60 sessions."""
    if not bench_bars:
        return {"benchmark": None, "rs20": None, "rs60": None, "state": None}
    mine = {b["t"]: b["c"] for b in bars}
    bench = {b["t"]: b["c"] for b in bench_bars}
    common = sorted(set(mine) & set(bench))
    if len(common) < 22:
        return {"benchmark": None, "rs20": None, "rs60": None, "state": None}
    ms = {d: mine[d] for d in common}
    bs = {d: bench[d] for d in common}
    r20s, r20b = _returns_from_dates(ms, 20), _returns_from_dates(bs, 20)
    r60s, r60b = _returns_from_dates(ms, 60), _returns_from_dates(bs, 60)
    rs20 = None if r20s is None or r20b is None else r20s - r20b
    rs60 = None if r60s is None or r60b is None else r60s - r60b
    state = None
    if rs20 is not None:
        state = "outperforming" if rs20 > 2 else "underperforming" if rs20 < -2 else "in line"
    return {"benchmark": None, "rs20": _r(rs20), "rs60": _r(rs60), "state": state,
            "stock_ret20": _r(r20s), "bench_ret20": _r(r20b)}


def benchmark_read(bars: list[dict] | None, ticker: str = "SPY") -> dict | None:
    """Trend + volatility sketch of the benchmark index, for market context."""
    if not bars or len(bars) < 60:
        return None
    closes = [b["c"] for b in bars]
    highs = [b["h"] for b in bars]
    lows = [b["l"] for b in bars]
    s20, s50, s200 = sma(closes, 20), sma(closes, 50), sma(closes, 200)
    a = atr(highs, lows, closes)
    price = closes[-1]
    px20, px50, px200 = _last(s20), _last(s50), _last(s200)
    label = "mixed"
    if px20 and px50:
        if price > px20 > px50 and (px200 is None or px50 > px200):
            label = "up"
        elif price < px20 < px50 and (px200 is None or px50 < px200):
            label = "down"
    return {
        "ticker": ticker.upper(), "price": _r(price), "trend": label,
        "sma50": _r(px50), "sma200": _r(px200),
        "atr_pct": _r((a[-1] / price * 100) if a[-1] and price else None),
        "ret20": _r(_returns_from_dates({b["t"]: b["c"] for b in bars}, 20)),
        "ret60": _r(_returns_from_dates({b["t"]: b["c"] for b in bars}, 60)),
    }


# --------------------------------------------------------------------------- #
# levels
# --------------------------------------------------------------------------- #
def _levels(price: float, highs: list[float], lows: list[float], closes: list[float],
            atr_v: float | None, smas: dict[str, float | None],
            donchian: tuple[float | None, float | None],
            high52: float | None, low52: float | None) -> dict:
    # a broken swing low above price now acts as resistance, and a prior high below
    # price now acts as support (role flip); name them accordingly.
    candidates: list[tuple[float, str]] = []
    recent_highs = highs[-180:]
    recent_lows = lows[-180:]
    for p in swing_points(recent_highs, recent_lows, 3, 3):
        pivot = p["price"]
        if p["kind"] == "high":
            kind = "swing high" if pivot > price else "prior high"
        else:
            kind = "swing low" if pivot < price else "broken low"
        candidates.append((pivot, kind))
    for name, val in smas.items():
        if val:
            candidates.append((val, name.replace("sma", "") + "-day MA"))
    if donchian[0]:
        candidates.append((donchian[0], "20-day high"))
    if donchian[1]:
        candidates.append((donchian[1], "20-day low"))
    if high52:
        candidates.append((high52, "52-week high"))
    if low52:
        candidates.append((low52, "52-week low"))

    def dedupe(items: list[tuple[float, str]]) -> list[dict]:
        out: list[dict] = []
        for p, kind in items:
            if not p or p <= 0:
                continue
            if out and abs(p - out[-1]["price"]) / price < 0.007:
                if kind not in out[-1]["kinds"]:
                    out[-1]["kinds"].append(kind)
                continue
            out.append({"price": p, "kinds": [kind]})
        for row in out:
            p = row["price"]
            row["price"] = _r(p)
            row["kind"] = " + ".join(row.pop("kinds")[:2])
            row["distance_pct"] = _r((p / price - 1) * 100)
            row["atr_mult"] = _r((p - price) / atr_v, 1) if atr_v else None
        return out[:3]

    supports = dedupe(sorted([(p, k) for p, k in candidates if p < price * 0.999],
                             key=lambda x: -x[0]))
    resistances = dedupe(sorted([(p, k) for p, k in candidates if p > price * 1.001],
                                key=lambda x: x[0]))
    return {
        "support": supports,
        "resistance": resistances,
        "sma20": _r(smas.get("sma20")), "sma50": _r(smas.get("sma50")),
        "sma200": _r(smas.get("sma200")),
        "donchian_high20": _r(donchian[0]), "donchian_low20": _r(donchian[1]),
        "high_52w": _r(high52), "low_52w": _r(low52),
        "from_high_pct": _r((price / high52 - 1) * 100) if high52 else None,
        "from_low_pct": _r((price / low52 - 1) * 100) if low52 else None,
    }


# --------------------------------------------------------------------------- #
# the main analysis
# --------------------------------------------------------------------------- #
def analyse(ticker: str, bars: list[dict], *, quote: dict | None = None,
            benchmark_bars: list[dict] | None = None,
            benchmark: str = "SPY", name: str | None = None) -> dict:
    """Build a full technical snapshot for one ticker."""
    snap: dict[str, Any] = {
        "ticker": ticker.upper(), "name": name, "asof": iso(),
        "bar_date": None, "bars": 0, "price": None, "change_pct": None,
        "provisional": False, "error": None, "score": 0.0, "bias": "neutral",
        "trend": "sideways", "setup": "unknown", "headline": "",
        "narrative": "", "signals": [], "risks": [], "read": None,
    }
    series, intraday = merge_quote(bars, quote)
    if len(series) < 30:
        snap["error"] = f"only {len(series)} daily bars available (need 30)"
        snap["bar_date"] = series[-1]["t"] if series else None
        snap["narrative"] = "Not enough price history to compute a technical read."
        snap["context"] = _contexts(snap)
        return snap

    snap["bars"] = len(series)
    snap["bar_date"] = series[-1]["t"]
    snap["provisional"] = bool(intraday)
    closes = [float(b["c"]) for b in series]
    highs = [float(b["h"]) for b in series]
    lows = [float(b["l"]) for b in series]
    vols = [float(b["v"] or 0) for b in series]
    price = closes[-1]
    prev_close = closes[-2] if len(closes) > 1 else None
    snap["price"] = _r(price)
    snap["change_pct"] = _r((price / prev_close - 1) * 100 if prev_close else None)
    if quote and quote.get("change_pct") is not None:
        snap["change_pct"] = _r(quote["change_pct"])

    sma10, sma20, sma50, sma200 = (sma(closes, 10), sma(closes, 20),
                                   sma(closes, 50), sma(closes, 200))
    s20, s50, s200 = _last(sma20), _last(sma50), _last(sma200)
    slope50 = None
    if len(sma50) >= 21 and sma50[-21] not in (None, 0) and sma50[-1] is not None:
        slope50 = (sma50[-1] - sma50[-21]) / sma50[-21] * 100
    atr_s = atr(highs, lows, closes)
    atr_v = _last(atr_s)
    atr_pct = (atr_v / price * 100) if atr_v and price else None

    # ---- trend ------------------------------------------------------------
    trend_pts = 0.0
    if s20:
        trend_pts += 12 if price > s20 else -12
    if s20 and s50:
        trend_pts += 10 if s20 > s50 else -10
    if s50 and s200:
        trend_pts += 10 if s50 > s200 else -10
    if s200:
        trend_pts += 8 if price > s200 else -8
    if slope50 is not None:
        trend_pts += _clamp(slope50 * 4, -12, 12)
    adx_v, pdi, mdi = adx(highs, lows, closes)
    if adx_v is not None and pdi is not None and mdi is not None and adx_v >= 20:
        trend_pts += 10 if pdi > mdi else -10

    swings = swing_points(highs[-180:], lows[-180:], 3, 3)
    recent_highs = [p["price"] for p in swings if p["kind"] == "high"][-2:]
    recent_lows = [p["price"] for p in swings if p["kind"] == "low"][-2:]
    structure = "mixed"
    if len(recent_highs) == 2 and len(recent_lows) == 2:
        if recent_highs[-1] > recent_highs[0] and recent_lows[-1] > recent_lows[0]:
            structure = "higher highs / higher lows"
            trend_pts += 8
        elif recent_highs[-1] < recent_highs[0] and recent_lows[-1] < recent_lows[0]:
            structure = "lower highs / lower lows"
            trend_pts -= 8
    trend_raw = _clamp(trend_pts, -72, 72)
    trend_score = trend_raw / 72 * 100

    if trend_raw >= 22:
        trend = "strong_up"
    elif trend_raw >= 7:
        trend = "up"
    elif trend_raw <= -22:
        trend = "strong_down"
    elif trend_raw <= -7:
        trend = "down"
    else:
        trend = "sideways"

    snap["trend"] = trend
    snap["trend_label"] = TREND_LABELS[trend]
    snap["trend_detail"] = {
        "score": _r(trend_score, 1), "structure": structure,
        "sma10": _r(_last(sma10)), "sma20": _r(s20), "sma50": _r(s50),
        "sma200": _r(s200),
        "stack": ("bullish" if (s20 and s50 and s200 and s20 > s50 > s200)
                  else "bearish" if (s20 and s50 and s200 and s20 < s50 < s200)
                  else "mixed"),
        "slope50_pct": _r(slope50), "above_sma20_pct": _r((price / s20 - 1) * 100) if s20 else None,
        "above_sma50_pct": _r((price / s50 - 1) * 100) if s50 else None,
        "above_sma200_pct": _r((price / s200 - 1) * 100) if s200 else None,
        "adx": _r(adx_v, 1), "plus_di": _r(pdi, 1), "minus_di": _r(mdi, 1),
    }

    # ---- momentum ---------------------------------------------------------
    rsi_s = rsi(closes)
    rsi_v = _last(rsi_s)
    rsi_prev5 = rsi_s[-6] if len(rsi_s) >= 6 else None
    macd_line, macd_sig, macd_hist = macd(closes)
    m_line, m_sig, m_hist = _last(macd_line), _last(macd_sig), _last(macd_hist)
    hist_prev = macd_hist[-2] if len(macd_hist) > 1 else None
    hist_prev3 = macd_hist[-4] if len(macd_hist) > 3 else None
    k_line, d_line = stochastic(highs, lows, closes)
    stoch_k, stoch_d = _last(k_line), _last(d_line)
    roc = {}
    for n in (5, 20, 60, 120):
        roc[n] = ((closes[-1] / closes[-1 - n] - 1) * 100
                  if len(closes) > n and closes[-1 - n] else None)

    mom_pts = 0.0
    if rsi_v is not None:
        mom_pts += _clamp((rsi_v - 50) * 0.6, -18, 18)
    if rsi_v is not None and rsi_prev5 is not None:
        mom_pts += _clamp((rsi_v - rsi_prev5) * 1.2, -10, 10)
    if m_line is not None and m_sig is not None:
        mom_pts += 15 if m_line > m_sig else -15
    if m_hist is not None and hist_prev is not None:
        mom_pts += 5 if m_hist > hist_prev else -5
    if roc[20] is not None:
        mom_pts += _clamp(roc[20] * 1.8, -20, 20)
    if stoch_k is not None and stoch_d is not None:
        if stoch_k > stoch_d and stoch_k < 80:
            mom_pts += 5
        elif stoch_k < stoch_d and stoch_k > 20:
            mom_pts -= 5
    mom_score = _clamp(mom_pts, -78, 78) / 78 * 100

    if m_line is None or m_sig is None:
        macd_state = None
    elif m_line > m_sig:
        macd_state = "bull expanding" if (m_hist or 0) > (hist_prev or 0) else "bull cooling"
    else:
        macd_state = "bear expanding" if (m_hist or 0) < (hist_prev or 0) else "bear fading"

    if rsi_v is None:
        rsi_zone = None
    elif rsi_v >= 70:
        rsi_zone = "overbought"
    elif rsi_v >= 55:
        rsi_zone = "positive"
    elif rsi_v <= 30:
        rsi_zone = "oversold"
    elif rsi_v <= 45:
        rsi_zone = "negative"
    else:
        rsi_zone = "neutral"

    snap["momentum"] = {
        "score": _r(mom_score, 1), "rsi14": _r(rsi_v, 1), "rsi_zone": rsi_zone,
        "rsi_chg5": _r(rsi_v - rsi_prev5, 1) if rsi_v is not None and rsi_prev5 is not None else None,
        "macd": _r(m_line, 3), "macd_signal": _r(m_sig, 3), "macd_hist": _r(m_hist, 3),
        "macd_state": macd_state,
        "macd_hist_3ago": _r(hist_prev3, 3),
        "stoch_k": _r(stoch_k, 1), "stoch_d": _r(stoch_d, 1),
        "roc5_pct": _r(roc[5]), "roc20_pct": _r(roc[20]),
        "roc60_pct": _r(roc[60]), "roc120_pct": _r(roc[120]),
    }

    # ---- volatility -------------------------------------------------------
    mid, upper, lower, bb_width = bollinger(closes)
    width_now = _last(bb_width)
    width_pctile = percentile_rank(bb_width[-120:], width_now)
    log_ret = [math.log(closes[i] / closes[i - 1])
               for i in range(max(1, len(closes) - 20), len(closes)) if closes[i - 1] > 0]
    realized = (math.sqrt(sum((x - sum(log_ret) / len(log_ret)) ** 2 for x in log_ret)
                          / max(1, len(log_ret) - 1)) * math.sqrt(252) * 100) if len(log_ret) > 2 else None
    atr_pctile = percentile_rank(atr_s[-120:], atr_v)
    if atr_pctile is None:
        vol_regime = None
    elif atr_pctile >= 85:
        vol_regime = "extreme"
    elif atr_pctile >= 65:
        vol_regime = "elevated"
    elif atr_pctile <= 20:
        vol_regime = "compressed"
    else:
        vol_regime = "normal"
    snap["volatility"] = {
        "atr14": _r(atr_v), "atr_pct": _r(atr_pct), "atr_pctile": _r(atr_pctile, 0),
        "bb_width_pct": _r(width_now), "bb_width_pctile": _r(width_pctile, 0),
        "bb_upper": _r(_last(upper)), "bb_lower": _r(_last(lower)),
        "realized_vol20_ann": _r(realized, 1), "regime": vol_regime,
        "extension_atr": _r((price - s20) / atr_v, 1) if s20 and atr_v else None,
    }

    # ---- participation ----------------------------------------------------
    avg20 = sum(vols[-20:]) / min(20, len(vols)) if vols else 0
    vol_last = vols[-1] if vols else 0
    vol_ratio = vol_last / avg20 if avg20 else None
    up_vol = down_vol = 0.0
    for i in range(max(1, len(closes) - 20), len(closes)):
        if closes[i] > closes[i - 1]:
            up_vol += vols[i]
        elif closes[i] < closes[i - 1]:
            down_vol += vols[i]
    updown = (up_vol / down_vol) if down_vol else None
    obv_s = obv(closes, vols)
    obv_chg = (obv_s[-1] - obv_s[-11]) / avg20 if len(obv_s) > 11 and avg20 else None
    volume_pts = 0.0
    if obv_chg is not None:
        volume_pts += _clamp(obv_chg * 3, -10, 10)
    if updown is not None:
        volume_pts += _clamp((updown - 1) * 6, -10, 10)
    vol_score = _clamp(volume_pts, -20, 20) / 20 * 100
    snap["volume"] = {
        "last": int(vol_last), "avg20": int(avg20), "ratio": _r(vol_ratio),
        "state": (None if vol_ratio is None else
                  "heavy" if vol_ratio >= 1.5 else "light" if vol_ratio <= 0.6 else "normal"),
        "obv_chg10": _r(obv_chg), "obv_trend": (None if obv_chg is None else
                                               "rising" if obv_chg > 0.5 else
                                               "falling" if obv_chg < -0.5 else "flat"),
        "up_down_vol_ratio20": _r(updown), "score": _r(vol_score, 1),
    }

    # ---- relative strength / market --------------------------------------
    rs = relative_strength(series, benchmark_bars)
    rs["benchmark"] = benchmark.upper()
    rs_score = 0.0
    if rs.get("rs20") is not None:
        rs_score += _clamp(rs["rs20"] * 4, -50, 50)
    if rs.get("rs60") is not None:
        rs_score += _clamp(rs["rs60"] * 2, -50, 50)
    rs_score = _clamp(rs_score, -100, 100)
    snap["relative"] = {**rs, "score": _r(rs_score, 1)}

    # ---- levels -----------------------------------------------------------
    donchian = (max(highs[-20:]) if len(highs) >= 20 else None,
                min(lows[-20:]) if len(lows) >= 20 else None)
    high52 = max(highs[-252:]) if highs else None
    low52 = min(lows[-252:]) if lows else None
    levels = _levels(price, highs, lows, closes, atr_v,
                     {"sma20": s20, "sma50": s50, "sma200": s200},
                     donchian, high52, low52)
    snap["levels"] = levels

    # ---- composite score & setup -----------------------------------------
    score = (0.45 * trend_score + 0.30 * mom_score
             + 0.10 * vol_score + 0.15 * rs_score)
    score = round(_clamp(score, -100, 100), 1)
    snap["score"] = score
    snap["bias"] = "bullish" if score >= 18 else "bearish" if score <= -18 else "neutral"
    snap["setup"] = _classify_setup(trend, price, s20, donchian, vol_ratio,
                                    atr_v, rsi_v, width_pctile)
    snap["setup_label"] = SETUP_LABELS[snap["setup"]]

    # ---- signals & risks --------------------------------------------------
    snap["signals"] = _signals(snap)
    snap["risks"] = _risks(snap)
    snap["headline"] = _headline(snap)
    snap["narrative"] = _narrative(snap)
    snap["context"] = _contexts(snap)
    snap["spark"] = [_r(c, 2) for c in closes[-60:]]
    snap["spark_start"] = series[-60]["t"] if len(series) >= 60 else series[0]["t"]
    snap["benchmark"] = benchmark_read(benchmark_bars, benchmark)
    return snap


def _classify_setup(trend: str, price: float, s20: float | None,
                    donchian: tuple[float | None, float | None], vol_ratio: float | None,
                    atr_v: float | None, rsi_v: float | None,
                    width_pctile: float | None) -> str:
    dh, dl = donchian
    ext = (price - s20) / atr_v if s20 and atr_v else 0.0
    up = trend in ("up", "strong_up")
    down = trend in ("down", "strong_down")
    if up and rsi_v is not None and rsi_v >= 70 and ext >= 2.0:
        return "extended_uptrend"
    if up and dh and price >= dh * 0.998 and (vol_ratio or 0) >= 1.15:
        return "breakout"
    if up and s20 and price < s20 and (rsi_v or 50) >= 38:
        return "uptrend_pullback"
    if down and rsi_v is not None and rsi_v <= 32:
        return "oversold_downtrend"
    if down and s20 and price > s20:
        return "downtrend_bounce"
    if down and dl and price <= dl * 1.002:
        return "breakdown"
    if trend == "sideways" and width_pctile is not None and width_pctile <= 25:
        return "coiled_range"
    if trend == "sideways":
        return "range"
    if up or down:
        return "trending"
    return "unknown"


def _signals(s: dict) -> list[dict]:
    t, m, v, r = s["trend_detail"], s["momentum"], s["volume"], s["relative"]
    lv = s["levels"]
    out: list[dict] = []
    if t.get("stack") == "bullish":
        out.append({"key": "sma_stack", "tone": "bull", "weight": 3,
                    "label": "20 > 50 > 200 DMA stacked up"})
    elif t.get("stack") == "bearish":
        out.append({"key": "sma_stack", "tone": "bear", "weight": 3,
                    "label": "20 < 50 < 200 DMA stacked down"})
    if t.get("above_sma200_pct") is not None:
        out.append({"key": "sma200", "tone": "bull" if t["above_sma200_pct"] > 0 else "bear",
                    "weight": 2,
                    "label": f"{abs(t['above_sma200_pct']):.1f}% "
                             f"{'above' if t['above_sma200_pct'] > 0 else 'below'} the 200-day"})
    if t.get("adx") is not None:
        tone = "neutral" if t["adx"] < 20 else ("bull" if (t.get("plus_di") or 0) > (t.get("minus_di") or 0) else "bear")
        out.append({"key": "adx", "tone": tone, "weight": 2,
                    "label": f"ADX {t['adx']:.0f} "
                             f"({'trending' if t['adx'] >= 20 else 'no trend'})"})
    if t.get("structure") not in (None, "mixed"):
        out.append({"key": "structure", "tone": "bull" if t["structure"].startswith("higher") else "bear",
                    "weight": 2, "label": t["structure"]})
    if m.get("rsi14") is not None:
        tone = ("bull" if m.get("rsi_zone") in ("positive", "overbought") else
                "bear" if m.get("rsi_zone") in ("negative", "oversold") else "neutral")
        out.append({"key": "rsi", "tone": tone, "weight": 2,
                    "label": f"RSI {m['rsi14']:.0f} · {m.get('rsi_zone')}"})
    if m.get("macd_state"):
        tone = "bull" if m["macd_state"].startswith("bull") else "bear"
        out.append({"key": "macd", "tone": tone, "weight": 2,
                    "label": f"MACD {m['macd_state']}"})
    if v.get("ratio") is not None:
        out.append({"key": "volume", "tone": "neutral", "weight": 1,
                    "label": f"volume {v['ratio']:.2f}× 20-day avg ({v.get('state')})"})
    if v.get("obv_trend"):
        out.append({"key": "obv", "tone": "bull" if v["obv_trend"] == "rising" else
                    "bear" if v["obv_trend"] == "falling" else "neutral", "weight": 1,
                    "label": f"OBV {v['obv_trend']}"})
    if r.get("rs20") is not None:
        out.append({"key": "rs", "tone": "bull" if r["rs20"] > 0 else "bear", "weight": 2,
                    "label": f"{_pct(r['rs20'])} vs {r['benchmark']} over 20 days"})
    if lv.get("from_high_pct") is not None:
        out.append({"key": "52w", "tone": "neutral", "weight": 1,
                    "label": f"{abs(lv['from_high_pct']):.1f}% from the 52-week high"})
    if s["volatility"].get("regime"):
        out.append({"key": "vol", "tone": "neutral", "weight": 1,
                    "label": f"volatility {s['volatility']['regime']} "
                             f"(ATR {s['volatility'].get('atr_pct') or 0:.1f}% of price)"})
    return out


def _risks(s: dict) -> list[str]:
    m, v, lv = s["momentum"], s["volatility"], s["levels"]
    out: list[str] = []
    if (m.get("rsi14") or 0) >= 70:
        out.append(f"Overbought: RSI {m['rsi14']:.0f} — chasing here risks a mean-reversion snapback.")
    if (v.get("extension_atr") or 0) >= 2.5:
        out.append(f"Extended {v['extension_atr']:.1f} ATR above the 20-day — pullback risk is elevated.")
    if (v.get("atr_pctile") or 0) >= 85:
        out.append("Volatility sits in the top 15% of the past year — size positions accordingly.")
    if (m.get("macd_state") or "").startswith("bear") and s["trend"] in ("up", "strong_up"):
        out.append("Momentum is diverging from price: MACD is below its signal while the trend is up.")
    if (s["relative"].get("rs20") or 0) < -2:
        out.append(f"Lagging {s['relative']['benchmark']} by {abs(s['relative']['rs20']):.1f}pp over 20 sessions.")
    r1 = (lv.get("resistance") or [None])[0]
    if r1 and r1.get("distance_pct") is not None and abs(r1["distance_pct"]) <= 1.5:
        out.append(f"Resistance {r1['price']:.2f} ({r1['kind']}) is only {r1['distance_pct']:+.1f}% away.")
    s1 = (lv.get("support") or [None])[0]
    if s1 and s1.get("distance_pct") is not None and abs(s1["distance_pct"]) <= 1.5:
        out.append(f"Support {s1['price']:.2f} ({s1['kind']}) is only {s1['distance_pct']:+.1f}% away — a break opens air.")
    return out[:4]


def _headline(s: dict) -> str:
    trend = TREND_LABELS.get(s["trend"], "sideways")
    setup = SETUP_LABELS.get(s["setup"], "")
    bits: list[str] = []
    if setup and setup not in (trend, "unknown"):
        bits.append(f"{trend} · {setup}")
    else:
        bits.append(trend)
    m = s["momentum"]
    if m.get("rsi_zone") in ("overbought", "oversold"):
        bits.append(f"RSI {m['rsi14']:.0f} {m['rsi_zone']}")
    elif m.get("macd_state"):
        bits.append(f"MACD {m['macd_state']}")
    lv = s["levels"]
    fh, fl = lv.get("from_high_pct"), lv.get("from_low_pct")
    if fh is not None and fh >= -1.5:
        bits.append("at 52-week highs")
    elif fl is not None and fl <= 1.5:
        bits.append("at 52-week lows")
    return ", ".join(bits)


def _narrative(s: dict) -> str:
    t, m, v, r, lv = (s["trend_detail"], s["momentum"], s["volume"],
                      s["relative"], s["levels"])
    price = s["price"]
    parts: list[str] = []

    chg = f" ({s['change_pct']:+.2f}%)" if s.get("change_pct") is not None else ""
    loc = []
    if lv.get("from_high_pct") is not None:
        loc.append(f"{abs(lv['from_high_pct']):.1f}% below its 52-week high")
    if t.get("above_sma200_pct") is not None:
        ap = t["above_sma200_pct"]
        loc.append(f"{abs(ap):.1f}% {'above' if ap >= 0 else 'below'} the 200-day average")
    parts.append(f"{s['ticker']} closed at {_num(price)} on {s['bar_date']}{chg}"
                 + ((", " + ", ".join(loc)) if loc else "") + ".")

    trend_txt = f"Trend is a {TREND_LABELS.get(s['trend'], 'sideways')}"
    extras = []
    if t.get("stack") not in (None, "mixed"):
        extras.append(f"{t['stack']} 20/50/200 stack")
    if t.get("adx") is not None:
        extras.append(f"ADX {t['adx']:.0f}")
    if t.get("structure") not in (None, "mixed"):
        extras.append(t["structure"])
    parts.append(trend_txt + (" (" + ", ".join(extras) + ")" if extras else "") + ".")

    mom = []
    if m.get("rsi14") is not None:
        rsi_txt = f"RSI {m['rsi14']:.0f} ({m.get('rsi_zone')})"
        if m.get("rsi_chg5") is not None:
            rsi_txt += f", {m['rsi_chg5']:+.0f} over five sessions"
        mom.append(rsi_txt)
    if m.get("macd_state"):
        mom.append(f"MACD {m['macd_state']}")
    if m.get("roc20_pct") is not None:
        mom.append(f"20-day change {_pct(m['roc20_pct'])}")
    if mom:
        parts.append("Momentum: " + "; ".join(mom) + ".")
    vol = []
    if v.get("atr_pct") is not None:
        vol.append(f"ATR {v['atr_pct']:.1f}% of price")
    if v.get("regime"):
        vol.append(f"volatility {v['regime']}")
    if v.get("bb_width_pctile") is not None:
        vol.append(f"Bollinger width in the {v['bb_width_pctile']:.0f}th percentile")
    if vol:
        parts.append("Volatility: " + ", ".join(vol) + ".")

    part = []
    if v.get("ratio") is not None:
        part.append(f"volume {v['ratio']:.2f}× the 20-day average ({v.get('state')})")
    if v.get("obv_trend"):
        part.append(f"OBV {v['obv_trend']}")
    if r.get("rs20") is not None:
        part.append(f"{_pct(r['rs20'])} vs {r['benchmark']} over 20 sessions")
    if part:
        parts.append("Participation: " + ", ".join(part) + ".")

    levels = []
    sup = lv.get("support") or []
    res = lv.get("resistance") or []
    if sup:
        levels.append("support " + ", ".join(f"{x['price']:.2f} ({x['kind']}, {x['distance_pct']:+.1f}%)"
                                             for x in sup[:2]))
    if res:
        levels.append("resistance " + ", ".join(f"{x['price']:.2f} ({x['kind']}, {x['distance_pct']:+.1f}%)"
                                                for x in res[:2]))
    if levels:
        parts.append("Nearest " + " · ".join(levels) + ".")
    return " ".join(parts)


def _side_verdict(s: dict, side: str) -> dict:
    score = s["score"]
    trend = TREND_LABELS.get(s["trend"], "sideways")
    setup = SETUP_LABELS.get(s["setup"], "no clear setup")
    lv = s["levels"]
    sup_list = lv.get("support") or []
    res_list = lv.get("resistance") or []
    long_side = side == "long"
    aligned = (score >= 18 and long_side) or (score <= -18 and not long_side)
    hostile = (score <= -18 and long_side) or (score >= 18 and not long_side)
    alignment = "supportive" if aligned else "hostile" if hostile else "mixed"

    def meaningful(items: list[dict], atr_floor: float) -> dict | None:
        """Prefer a level far enough away to mean something; else the nearest one."""
        for row in items:
            if abs(row.get("atr_mult") or 0) >= atr_floor:
                return row
        return items[0] if items else None

    atr_v = s["volatility"].get("atr14")
    price = s["price"]

    def synthetic(mult: float, direction: int) -> dict | None:
        """ATR-derived fallback when the chart has no usable swing level."""
        if not atr_v or not price:
            return None
        level = price + direction * mult * atr_v
        return {"price": _r(level),
                "kind": f"{mult:g}×ATR {'above' if direction > 0 else 'below'}",
                "distance_pct": _r((level / price - 1) * 100),
                "atr_mult": _r(direction * mult, 1), "synthetic": True}

    stop_ref = meaningful(sup_list if long_side else res_list, 1.0)
    if not stop_ref or abs(stop_ref.get("atr_mult") or 0) < 1.0:
        stop_ref = synthetic(1.5, -1 if long_side else 1) or stop_ref
    target_ref = meaningful(res_list if long_side else sup_list, 1.0)
    if not target_ref or abs(target_ref.get("atr_mult") or 0) < 1.0:
        target_ref = synthetic(2.0, 1 if long_side else -1) or target_ref
    trigger_ref = meaningful(res_list if long_side else sup_list, 1.0)
    if not trigger_ref or abs(trigger_ref.get("atr_mult") or 0) < 1.0:
        trigger_ref = synthetic(1.0, 1 if long_side else -1) or trigger_ref

    if aligned:
        invalidation = (f"{stop_ref['price']:.2f} ({_level_kind(stop_ref)})" if stop_ref
                        else "the recent swing")
        verdict = (f"{side.title()} is aligned with the tape: {trend}, {setup}, composite "
                   f"{score:+.0f}. Let it work; {invalidation} is the first invalidation.")
    elif hostile:
        reclaim = (f"{'reclaims' if long_side else 'loses'} {trigger_ref['price']:.2f} "
                   f"({_level_kind(trigger_ref)})" if trigger_ref
                   else "reclaims the 20-day")
        verdict = (f"{side.title()} is fighting the tape: {trend} with a {score:+.0f} composite. "
                   f"This is counter-trend until price {reclaim}; expect squeezes and weak "
                   f"follow-through.")
    else:
        trigger = (f"a close {'above' if long_side else 'below'} {trigger_ref['price']:.2f} "
                   f"({_level_kind(trigger_ref)})" if trigger_ref
                   else "a clean break of the range")
        verdict = (f"No directional edge for a {side}: {trend}, {setup}, composite {score:+.0f}. "
                   f"Wait for {trigger} before leaning in.")
    risk = None
    if stop_ref:
        risk = (f"Invalidation {stop_ref['price']:.2f} ({_level_kind(stop_ref)}, "
                f"{stop_ref['distance_pct']:+.1f}%).")
    reward = None
    if target_ref:
        reward = (f"First objective {target_ref['price']:.2f} ({_level_kind(target_ref)}, "
                  f"{target_ref['distance_pct']:+.1f}%).")
    return {
        "side": side, "alignment": alignment, "alignment_label": ALIGN_LABELS[alignment],
        "favored": aligned, "score": _r(score), "verdict": verdict,
        "risk": risk, "reward": reward,
        "stop": stop_ref["price"] if stop_ref else None,
        "stop_kind": stop_ref["kind"] if stop_ref else None,
        "target": target_ref["price"] if target_ref else None,
        "target_kind": target_ref["kind"] if target_ref else None,
        "trigger": trigger_ref["price"] if trigger_ref else None,
        "trigger_kind": trigger_ref["kind"] if trigger_ref else None,
    }


def _level_kind(row: dict | None) -> str:
    return (row or {}).get("kind") or "level"


def _contexts(s: dict) -> dict:
    """Both sides are pre-computed so the UI can switch instantly when tagged."""
    if s.get("error"):
        msg = "Not enough price history for a side-aware read."
        return {side: {"side": side, "alignment": "mixed", "alignment_label": "mixed",
                       "favored": False, "verdict": msg, "risk": None, "reward": None,
                       "stop": None, "target": None} for side in ("long", "short")}
    return {side: _side_verdict(s, side) for side in ("long", "short")}


def apply_read(snap: dict, read: Any) -> dict:
    """Attach an LLM second opinion alongside the computed narrative."""
    text = clean_read(read)
    if text:
        snap["read"] = text
    return snap


# --------------------------------------------------------------------------- #
# presentation helpers
# --------------------------------------------------------------------------- #
def light(snap: dict) -> dict:
    """Compact snapshot for /api/state and SSE (full payload lives in /api/technicals)."""
    m = snap.get("momentum") or {}
    v = snap.get("volatility") or {}
    return {
        "ticker": snap.get("ticker"), "asof": snap.get("asof"),
        "bar_date": snap.get("bar_date"), "price": snap.get("price"),
        "change_pct": snap.get("change_pct"), "score": snap.get("score"),
        "bias": snap.get("bias"), "trend": snap.get("trend"),
        "trend_label": TREND_LABELS.get(snap.get("trend"), snap.get("trend")),
        "setup": snap.get("setup"),
        "setup_label": SETUP_LABELS.get(snap.get("setup"), snap.get("setup")),
        "headline": snap.get("headline"), "rsi": m.get("rsi14"),
        "macd_state": m.get("macd_state"), "atr_pct": v.get("atr_pct"),
        "context": snap.get("context") or {}, "read": snap.get("read"),
        "provisional": snap.get("provisional"), "error": snap.get("error"),
    }


def book_summary(snapshots: dict[str, dict], sides: dict[str, str | None],
                 kinds: dict[str, str] | None = None) -> dict:
    """Breadth + side-alignment summary across the whole book.

    ``kinds`` maps ticker -> "position" | "watch" so ideas can be split into
    live positions and watchlist ideas; when omitted everything is grouped as
    watchlist.
    """
    kinds = kinds or {}
    by_bias = {"bullish": 0, "neutral": 0, "bearish": 0}
    by_trend: dict[str, int] = {}
    sided = {"long": 0, "short": 0, "none": 0}
    align = {"long": {"supportive": 0, "mixed": 0, "hostile": 0},
             "short": {"supportive": 0, "mixed": 0, "hostile": 0}}
    by_kind = {
        "position": {"total": 0, "tagged": 0, "none": 0,
                     "long": {"supportive": 0, "mixed": 0, "hostile": 0},
                     "short": {"supportive": 0, "mixed": 0, "hostile": 0}},
        "watch": {"total": 0, "tagged": 0, "none": 0,
                  "long": {"supportive": 0, "mixed": 0, "hostile": 0},
                  "short": {"supportive": 0, "mixed": 0, "hostile": 0}},
    }
    overbought, oversold, near_highs, near_lows, conflicts, errors = [], [], [], [], [], []
    ideas: list[dict] = []
    for ticker, snap in snapshots.items():
        if snap.get("error"):
            errors.append(ticker)
            continue
        by_bias[snap.get("bias") or "neutral"] = by_bias.get(snap.get("bias") or "neutral", 0) + 1
        tr = snap.get("trend") or "sideways"
        by_trend[tr] = by_trend.get(tr, 0) + 1
        side = sides.get(ticker) or sides.get(ticker.upper())
        side = side if side in ("long", "short") else "none"
        sided[side] += 1
        kind = kinds.get(ticker) or kinds.get(ticker.upper()) or "watch"
        if kind not in by_kind:
            kind = "watch"
        by_kind[kind]["total"] += 1
        rsi_v = (snap.get("momentum") or {}).get("rsi14")
        if rsi_v is not None and rsi_v >= 70:
            overbought.append(ticker)
        if rsi_v is not None and rsi_v <= 30:
            oversold.append(ticker)
        lv = snap.get("levels") or {}
        fh, fl = lv.get("from_high_pct"), lv.get("from_low_pct")
        if fh is not None and fh >= -1.5:
            near_highs.append(ticker)
        if fl is not None and fl <= 1.5:
            near_lows.append(ticker)
        if side in ("long", "short"):
            ctx = (snap.get("context") or {}).get(side) or {}
            a = ctx.get("alignment") or "mixed"
            align[side][a] = align[side].get(a, 0) + 1
            by_kind[kind]["tagged"] += 1
            by_kind[kind][side][a] = by_kind[kind][side].get(a, 0) + 1
            idea = {
                "ticker": ticker, "kind": kind, "side": side, "alignment": a,
                "score": snap.get("score"), "trend": tr,
                "trend_label": TREND_LABELS.get(tr, tr),
                "setup": snap.get("setup"),
                "setup_label": SETUP_LABELS.get(snap.get("setup"), snap.get("setup")),
            }
            ideas.append(idea)
            if a == "hostile":
                conflicts.append({**idea})
        else:
            by_kind[kind]["none"] += 1
    total = sum(by_bias.values())
    tagged = sided["long"] + sided["short"]
    support = align["long"]["supportive"] + align["short"]["supportive"]
    mixed = align["long"]["mixed"] + align["short"]["mixed"]
    against = align["long"]["hostile"] + align["short"]["hostile"]
    parts = []
    if total:
        parts.append(f"{by_bias['bullish']} bullish · {by_bias['neutral']} neutral · "
                     f"{by_bias['bearish']} bearish")
        if tagged:
            parts.append(f"{support} of {tagged} tagged ideas with the tape "
                         f"({mixed} mixed, {against} against)")
        if overbought:
            parts.append(f"{len(overbought)} overbought")
        if oversold:
            parts.append(f"{len(oversold)} oversold")
    order = {"position": 0, "watch": 1}
    rank = {"supportive": 0, "mixed": 1, "hostile": 2}
    ideas.sort(key=lambda r: (order.get(r["kind"], 2), r["side"],
                              rank.get(r["alignment"], 3), -(abs(r.get("score") or 0))))
    conflicts.sort(key=lambda x: -(abs(x.get("score") or 0)))
    return {
        "total": total, "by_bias": by_bias, "by_trend": by_trend, "sides": sided,
        "tagged": tagged,
        "alignment": align, "alignment_by_kind": by_kind, "ideas": ideas,
        "overbought": overbought, "oversold": oversold,
        "near_highs": near_highs, "near_lows": near_lows, "conflicts": conflicts[:8],
        "errors": errors, "headline": " · ".join(parts) or "No technical snapshots yet.",
    }

def parse_bar_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d")
    except ValueError:
        return None


def is_fresh(snap: dict, max_age_minutes: float) -> bool:
    """True when the snapshot was computed recently enough to reuse."""
    from .util import parse_dt
    at = parse_dt(snap.get("asof"))
    if not at:
        return False
    return (now_utc() - at).total_seconds() / 60.0 <= max_age_minutes


def clean_read(text: Any, limit: int = 480) -> str | None:
    t = trim_sentence(clean_text(str(text or ""), None), limit)
    return t or None
