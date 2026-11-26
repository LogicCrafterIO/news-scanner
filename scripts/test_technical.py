"""Unit + integration checks for app/technical.py.

Run: .venv/bin/python scripts/test_technical.py [--live]
No pytest dependency; plain assertions so it runs in the project venv.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import technical as T  # noqa: E402

PASS = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    if not cond:
        raise AssertionError(f"{name} failed {detail}")
    PASS += 1


def approx(a: float | None, b: float, tol: float = 1e-6) -> bool:
    return a is not None and abs(a - b) <= tol


def uptrend_bars(n: int = 260, start: float = 100.0, step: float = 0.0015) -> list[dict]:
    bars = []
    for i in range(n):
        drift = start * (1 + step) ** i
        wobble = math.sin(i / 7.0) * start * 0.012
        close = drift + wobble
        bars.append({
            "t": f"2025-{1 + i // 28:02d}-{1 + i % 28:02d}",
            "o": close * 0.998, "h": close * 1.006, "l": close * 0.992,
            "c": close, "v": 1_000_000 + (i % 9) * 20_000,
        })
    return bars


def downtrend_bars(n: int = 260, start: float = 200.0, step: float = 0.0015) -> list[dict]:
    bars = []
    for i in range(n):
        drift = start * (1 - step) ** i
        wobble = math.sin(i / 6.0) * start * 0.01
        close = drift + wobble
        bars.append({
            "t": f"2025-{1 + i // 28:02d}-{1 + i % 28:02d}",
            "o": close * 1.001, "h": close * 1.008, "l": close * 0.994,
            "c": close, "v": 900_000 + (i % 7) * 15_000,
        })
    return bars


def test_indicators() -> None:
    check("sma", T.sma([1, 2, 3, 4], 2) == [None, 1.5, 2.5, 3.5], T.sma([1, 2, 3, 4], 2))
    check("sma warmup none", T.sma([1, 2], 3) == [None, None])
    check("ema", T.ema([1, 2, 3], 2) == [None, 1.5, 2.5], T.ema([1, 2, 3], 2))
    check("ema flat", all(x == 5 for x in T.ema([5] * 10, 4)[3:]))

    up = T.rsi([float(i) for i in range(1, 30)])
    check("rsi monotonic up = 100", approx(up[-1], 100.0), str(up[-1]))
    down = T.rsi([float(30 - i) for i in range(1, 30)])
    check("rsi monotonic down = 0", approx(down[-1], 0.0), str(down[-1]))
    flat = T.rsi([50.0] * 30)
    check("rsi flat = 100 (no losses)", approx(flat[-1], 100.0))

    highs = [10.0 + i * 0.1 for i in range(40)]
    lows = [9.0 + i * 0.1 for i in range(40)]
    closes = [9.5 + i * 0.1 for i in range(40)]
    atr = T.atr(highs, lows, closes, 14)
    check("atr positive", atr[-1] is not None and atr[-1] > 0, str(atr[-1]))

    line, sig, hist = T.macd([float(i) for i in range(1, 80)])
    check("macd shape", len(line) == len(sig) == len(hist) == 79)
    check("macd trending up", line[-1] is not None and line[-1] > 0, str(line[-1]))

    mid, upper, lower, width = T.bollinger([float(i % 10 + 100) for i in range(60)], 20)
    check("bollinger bands ordered", lower[-1] < mid[-1] < upper[-1], str((lower[-1], mid[-1], upper[-1])))
    check("bollinger width", width[-1] is not None and width[-1] > 0)

    k, d = T.stochastic(highs, lows, closes, 14)
    check("stoch in range", 0 <= k[-1] <= 100, str(k[-1]))
    check("stoch d present", d[-1] is not None)

    adx_v, pdi, mdi = T.adx([100 + i for i in range(60)], [99 + i for i in range(60)],
                            [99.5 + i for i in range(60)])
    check("adx computed", adx_v is not None and pdi is not None and mdi is not None)
    check("adx +di > -di", pdi > mdi, f"{pdi} vs {mdi}")

    obv = T.obv([1, 2, 1.5, 2.5], [100, 200, 300, 400])
    check("obv", obv == [0, 200, -100, 300], str(obv))

    check("percentile rank", approx(T.percentile_rank([1, 2, 3, 4], 3), 75.0))
    check("percentile none", T.percentile_rank([], 1) is None)

    swings = T.swing_points([1, 2, 3, 2, 1, 5, 1], [1, 2, 3, 2, 1, 5, 1], 2, 2)
    check("swing high at 2", any(p["kind"] == "high" and p["i"] == 2 for p in swings), str(swings))


def test_quote_merge() -> None:
    bars = [
        {"t": "2026-10-19", "o": 10, "h": 11, "l": 9, "c": 10, "v": 100},
        {"t": "2026-10-20", "o": 10, "h": 11, "l": 9, "c": 10.5, "v": 100},
    ]
    quote = {"price": 11.0, "bar_date": "2026-10-20", "open": 10.1, "high": 11.2,
             "low": 9.9, "volume": 150}
    merged, intraday = T.merge_quote(bars, quote)
    check("merge same day", merged[-1]["c"] == 11.0 and len(merged) == 2)
    check("merge updates high", merged[-1]["h"] == 11.2)
    check("merge marks intraday", intraday is True)
    check("merge does not mutate", bars[-1]["c"] == 10.5)

    quote2 = {**quote, "bar_date": "2026-10-21"}
    merged2, intraday2 = T.merge_quote(bars, quote2)
    check("merge appends today", len(merged2) == 3 and merged2[-1]["t"] == "2026-10-21")
    check("append marks intraday", intraday2 is True)

    merged3, intraday3 = T.merge_quote(bars, None)
    check("no quote", len(merged3) == 2 and intraday3 is False)


def test_relative_strength() -> None:
    mine = [{"t": f"2026-01-{i + 1:02d}", "c": 100 * (1.002 ** i)} for i in range(70)]
    bench = [{"t": f"2026-01-{i + 1:02d}", "c": 100 * (1.001 ** i)} for i in range(70)]
    rs = T.relative_strength(mine, bench)
    check("rs outperforming", rs["state"] == "outperforming", str(rs))
    check("rs20 positive", rs["rs20"] > 0)
    check("rs no bench", T.relative_strength(mine, None)["rs20"] is None)


def test_uptrend_snapshot() -> None:
    snap = T.analyse("TEST", uptrend_bars())
    check("snapshot ticker", snap["ticker"] == "TEST")
    check("snapshot bars", snap["bars"] == 260)
    check("uptrend classified", snap["trend"] in ("up", "strong_up"), str(snap["trend"]))
    check("uptrend score positive", snap["score"] > 0, str(snap["score"]))
    check("bullish bias", snap["bias"] == "bullish", str(snap["score"]))
    check("long supportive", snap["context"]["long"]["alignment"] == "supportive",
          snap["context"]["long"]["alignment"])
    check("short hostile", snap["context"]["short"]["alignment"] == "hostile")
    check("has narrative", len(snap["narrative"]) > 120)
    check("narrative mentions ticker", "TEST" in snap["narrative"])
    check("has signals", len(snap["signals"]) >= 5)
    check("has spark", len(snap["spark"]) == 60)
    check("levels present", bool(snap["levels"]["support"]) or bool(snap["levels"]["resistance"]))
    check("score bounded", -100 <= snap["score"] <= 100)
    check("bar date", snap["bar_date"] == "2025-10-08", str(snap["bar_date"]))
    light = T.light(snap)
    check("light keys", {"score", "trend", "context", "headline"} <= set(light))


def test_downtrend_snapshot() -> None:
    snap = T.analyse("DOWN", downtrend_bars())
    check("downtrend classified", snap["trend"] in ("down", "strong_down"), str(snap["trend"]))
    check("downtrend score negative", snap["score"] < 0, str(snap["score"]))
    check("bearish bias", snap["bias"] == "bearish", str(snap["score"]))
    check("short supportive", snap["context"]["short"]["alignment"] == "supportive")
    check("long hostile", snap["context"]["long"]["alignment"] == "hostile")
    check("short invalidation above", snap["context"]["short"]["stop"]
          >= snap["price"], str(snap["context"]["short"]))


def test_errors_and_summary() -> None:
    snap = T.analyse("SHORT", [{"t": "2026-01-01", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}])
    check("short history error", bool(snap["error"]))
    check("error narrative", "Not enough" in snap["narrative"])
    check("error context", "Not enough" in snap["context"]["long"]["verdict"])

    up = T.analyse("UP", uptrend_bars())
    down = T.analyse("DN", downtrend_bars())
    summary = T.book_summary({"UP": up, "DN": down}, {"UP": "long", "DN": "short"})
    check("summary total", summary["total"] == 2)
    check("summary bullish count", summary["by_bias"]["bullish"] == 1)
    check("summary alignment", summary["alignment"]["long"]["supportive"] == 1)
    check("summary no conflicts", summary["conflicts"] == [])
    summary2 = T.book_summary({"UP": up, "DN": down}, {"UP": "short", "DN": "long"})
    check("summary conflicts", len(summary2["conflicts"]) == 2, str(summary2["conflicts"]))
    check("summary headline", "bullish" in summary2["headline"])
    at_high = dict(up)
    at_high["levels"] = {**up["levels"], "from_high_pct": 0.0}
    summary3 = T.book_summary({"UP": at_high}, {})
    check("zero distance is a 52w high", summary3["near_highs"] == ["UP"],
          str(summary3["near_highs"]))

    summary4 = T.book_summary({"UP": up, "DN": down}, {"UP": "long", "DN": "long"},
                              {"UP": "position", "DN": "watch"})
    check("summary tagged", summary4["tagged"] == 2)
    check("summary position split",
          summary4["alignment_by_kind"]["position"]["long"]["supportive"] == 1
          and summary4["alignment_by_kind"]["position"]["total"] == 1)
    check("summary watch split",
          summary4["alignment_by_kind"]["watch"]["long"]["hostile"] == 1
          and summary4["alignment_by_kind"]["watch"]["tagged"] == 1)
    check("summary ideas", len(summary4["ideas"]) == 2
          and summary4["ideas"][0]["kind"] == "position")
    check("summary headline alignment", "1 of 2 tagged ideas with the tape" in summary4["headline"],
          summary4["headline"])


def test_live() -> None:
    import asyncio

    from app.sources import Sources

    async def fetch() -> None:
        async with Sources() as src:
            info = await src.stockanalysis_history("NVDA")
            spy = await src.stockanalysis_history("SPY")
            quote = await src.stockanalysis_quote("NVDA")
        check("live bars", len(info["bars"]) > 100, str(len(info["bars"])))
        check("live quote", quote and quote["price"] > 0, str(quote))
        snap = T.analyse("NVDA", info["bars"], quote=quote, benchmark_bars=spy["bars"])
        check("live no error", snap["error"] is None, str(snap["error"]))
        check("live score", -100 <= snap["score"] <= 100)
        check("live benchmark rs", snap["relative"]["benchmark"] == "SPY")
        print(f"  live NVDA: {snap['score']:+.1f} {snap['bias']} · {snap['headline']}")

    asyncio.run(fetch())


def main() -> None:
    test_indicators()
    test_quote_merge()
    test_relative_strength()
    test_uptrend_snapshot()
    test_downtrend_snapshot()
    test_errors_and_summary()
    if "--live" in sys.argv:
        test_live()
    print(f"technical checks: {PASS} passed")


if __name__ == "__main__":
    main()
