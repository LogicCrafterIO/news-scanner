"""Unit + integration checks for app/macro.py and the macro CSV parsers.

Run: .venv/bin/python scripts/test_macro.py [--live]
No pytest dependency; plain assertions so it runs in the project venv.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import macro as M                                   # noqa: E402
from app.sources import (parse_cboe_vix, parse_fred_csv,    # noqa: E402
                         parse_treasury_csv)

PASS = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    if not cond:
        raise AssertionError(f"{name} failed {detail}")
    PASS += 1


def eq(name: str, got, want) -> None:
    check(name, got == want, f"got {got!r}, want {want!r}")


TREASURY_CSV = '''Date,"1 Mo","2 Mo","3 Mo","6 Mo","1 Yr","2 Yr","3 Yr","5 Yr","7 Yr","10 Yr","20 Yr","30 Yr"
10/23/2026,4.11,4.16,4.22,4.30,4.44,4.69,4.75,4.82,4.89,5.00,5.37,5.33
10/22/2026,4.11,4.16,4.22,4.29,4.41,4.65,4.72,4.78,4.86,4.96,5.32,5.29
'''

FRED_CSV = """observation_date,DGS10
2026-10-21,4.94
2026-10-22,4.96
2026-10-23,.
"""

CBOE_CSV = """DATE,OPEN,HIGH,LOW,CLOSE
10/21/2026,18.90,19.50,18.60,19.03
10/22/2026,19.10,19.80,18.95,19.45
"""


def test_parsers() -> None:
    rows = parse_treasury_csv(TREASURY_CSV)
    eq("treasury rows", len(rows), 2)
    eq("treasury newest date", rows[0]["date"], "2026-10-23")
    eq("treasury 10y", rows[0]["10 Yr"], 5.00)
    eq("treasury 2y", rows[0]["2 Yr"], 4.69)

    fred = parse_fred_csv(FRED_CSV)
    eq("fred rows", len(fred), 3)
    eq("fred last date", fred[-1][0], "2026-10-23")
    eq("fred missing value", fred[-1][1], None)

    vix = parse_cboe_vix(CBOE_CSV)
    eq("vix rows", len(vix), 2)
    eq("vix last", vix[-1], ("2026-10-22", 19.45))


def fred_fixture() -> dict:
    monthly_cpi = [("2025-08-01", 318.0), ("2025-09-01", 320.0),
                   ("2026-08-01", 334.131), ("2026-09-01", 336.298)]
    core_cpi = [("2025-08-01", 330.0), ("2025-09-01", 331.0),
                ("2026-08-01", 337.765), ("2026-09-01", 338.896)]
    return {
        "DFII10": [("2026-10-21", 2.76), ("2026-10-22", 2.64)],
        "T10YIE": [("2026-10-21", 2.32), ("2026-10-22", 2.34)],
        "BAMLH0A0HYM2": [("2026-10-21", 3.27), ("2026-10-22", 3.40)],
        "BAMLC0A0CM": [("2026-10-21", 0.88), ("2026-10-22", 0.89)],
        "DTWEXBGS": [("2026-10-15", 121.56), ("2026-10-16", 121.4048)],
        "VIXCLS": [("2026-10-21", 18.9), ("2026-10-22", 19.03)],
        "DCOILWTICO": [("2026-10-19", 109.98), ("2026-10-20", 107.89)],
        "DCOILBRENTEU": [("2026-10-19", 118.82), ("2026-10-20", 115.22)],
        "DHHNGSP": [("2026-10-19", 3.28), ("2026-10-20", 3.10)],
        "MORTGAGE30US": [("2026-10-15", 7.40), ("2026-10-22", 7.32)],
        "ICSA": [("2026-10-10", 200000), ("2026-10-17", 194000)],
        "CPIAUCSL": monthly_cpi,
        "CPILFESL": core_cpi,
        "UNRATE": [("2026-08-01", 4.1), ("2026-09-01", 4.2)],
        "PAYEMS": [("2026-07-01", 158800), ("2026-08-01", 158894),
                   ("2026-09-01", 159044)],
        "DFEDTARL": [("2026-10-23", 3.75)],
        "DFEDTARU": [("2026-10-23", 4.00)],
    }


def treasury_fixture() -> list[dict]:
    return parse_treasury_csv(TREASURY_CSV)


def test_rates() -> None:
    metrics = {m["key"]: m for m in M.build_rates(treasury_fixture(), fred_fixture())}
    eq("2y display", metrics["us2y"]["display"], "4.69%")
    eq("2y chg", metrics["us2y"]["chg_display"], "+4bp")
    eq("2y dir", metrics["us2y"]["dir"], "up")
    eq("10y display", metrics["us10y"]["display"], "5.00%")
    eq("2s10s display", metrics["curve_2s10s"]["display"], "+31bp")
    eq("2s10s chg", metrics["curve_2s10s"]["chg_display"], "+0bp")
    eq("3m10y display", metrics["curve_3m10y"]["display"], "+78bp")
    eq("3m10y chg", metrics["curve_3m10y"]["chg_display"], "+4bp")
    eq("real 10y", metrics["real_10y"]["chg_display"], "-12bp")
    eq("breakeven 10y", metrics["breakeven_10y"]["chg_display"], "+2bp")
    eq("fed funds", metrics["fed_funds"]["display"], "3.75–4.00%")


def test_credit_commodities_economy() -> None:
    fred = fred_fixture()
    credit = {m["key"]: m for m in M.build_credit(fred, [("2026-10-21", 19.03),
                                                          ("2026-10-22", 19.45)])}
    eq("vix display", credit["vix"]["display"], "19.45")
    eq("vix chg", credit["vix"]["chg_display"], "+0.42")
    eq("hy oas", credit["hy_oas"]["display"], "340bp")
    eq("hy oas chg", credit["hy_oas"]["chg_display"], "+13bp")
    eq("ig oas", credit["ig_oas"]["display"], "89bp")
    eq("broad dollar", credit["broad_dollar"]["display"], "121.40")
    eq("broad dollar chg", credit["broad_dollar"]["chg_display"], "-0.16")

    metals = {"XAU": {"price": 3996.8999, "at": "2026-10-24T04:58:46Z"},
              "XAG": {"price": 54.046, "at": "2026-10-24T04:58:46Z"}}
    comm = {m["key"]: m for m in M.build_commodities(fred, metals)}
    eq("wti display", comm["wti"]["display"], "$107.89")
    eq("wti chg", comm["wti"]["chg_display"], "-1.9%")
    eq("gold display", comm["gold"]["display"], "$3,996.90")
    eq("gold chg", comm["gold"]["chg_display"], None)

    econ = {m["key"]: m for m in M.build_economy(fred)}
    check("cpi yoy roughly 5.1", abs(econ["cpi_yoy"]["value"] - 5.093) < 0.02,
          str(econ["cpi_yoy"]["value"]))
    eq("cpi chg", econ["cpi_yoy"]["chg_display"], "+0.0pp")
    eq("unemployment", econ["unemployment"]["display"], "4.2%")
    eq("payrolls", econ["payrolls"]["display"], "+150k")
    eq("payrolls chg", econ["payrolls"]["chg_display"], "Δ+56k")
    eq("claims", econ["claims"]["display"], "194k")
    eq("claims chg", econ["claims"]["chg_display"], "-6k")
    eq("mortgage", econ["mortgage30"]["display"], "7.32%")
    eq("mortgage chg", econ["mortgage30"]["chg_display"], "-0.08pp")


def test_markets() -> None:
    quotes = {
        "SPY": {"price": 759.97, "change_pct": -0.52},
        "QQQ": {"price": 704.40, "change_pct": -0.01},
        "IWM": {"price": 275.75, "change_pct": -0.33},
    }
    metrics = {m["key"]: m for m in M.build_markets(quotes)}
    eq("spy display", metrics["spy"]["display"], "$759.97")
    eq("spy chg", metrics["spy"]["chg_display"], "-0.52%")
    eq("market count", len(metrics), 3)


def test_calendar() -> None:
    now = datetime(2026, 10, 23, 6, 0, tzinfo=timezone.utc)
    raw = [
        {"country": "USD", "impact": "High", "title": "FOMC Statement",
         "date": "2026-10-28T14:00:00-04:00", "forecast": "3.75-4.00%", "previous": "4.00%"},
        {"country": "USD", "impact": "High", "title": "CPI",
         "date": "2026-10-23T14:00:00-04:00", "forecast": "3.1%", "previous": "3.0%"},
        {"country": "EUR", "impact": "High", "title": "ECB decision",
         "date": "2026-10-24T09:00:00-04:00", "forecast": "", "previous": ""},
        {"country": "USD", "impact": "Low", "title": "Minor survey",
         "date": "2026-10-24T10:00:00-04:00", "forecast": "", "previous": ""},
        {"country": "USD", "impact": "High", "title": "Already printed",
         "date": "2026-10-20T08:30:00-04:00", "forecast": "", "previous": ""},
    ]
    cal = M.build_calendar(raw, now=now)
    eq("calendar filters to 2", len(cal), 2)
    eq("calendar soonest", cal[0]["title"], "CPI")
    check("calendar soon", cal[0]["soon"] is True)
    eq("calendar impact", cal[0]["impact"], "high")
    eq("calendar next", cal[1]["title"], "FOMC Statement")


def test_desk_payload_and_line() -> None:
    treasury = treasury_fixture()
    fred = fred_fixture()
    groups = [
        {"key": "rates", "label": "Rates", "metrics": M.build_rates(treasury, fred)},
        {"key": "credit", "label": "Risk & credit",
         "metrics": M.build_credit(fred, [("2026-10-21", 19.03), ("2026-10-22", 19.45)])},
        {"key": "commodities", "label": "Commodities",
         "metrics": M.build_commodities(fred, {"XAU": {"price": 3996.9, "at": "2026-10-24T04:58:46Z"}})},
        {"key": "economy", "label": "Economy", "metrics": M.build_economy(fred)},
    ]
    snap = {"as_of": "2026-10-23", "at": "2026-10-23T20:00:00+00:00", "groups": groups,
            "calendar": [{"when_label": "Wed 28 Oct 14:00", "title": "FOMC Statement",
                          "forecast": "3.75-4.00%"}],
            "line": ""}
    payload = M.desk_payload(snap)
    eq("payload 10y", payload["rates"]["10Y"], "5.00% (+4bp)")
    eq("payload 2s10s", payload["rates"]["2s10s"], "+31bp (+0bp)")
    eq("payload calendar", payload["calendar"], ["Wed 28 Oct 14:00 FOMC Statement (forecast 3.75-4.00%)"])
    eq("payload calendar status", payload["calendar_status"], "ok")
    line = M.desk_line(snap)
    check("line has 10y", "10Y 5.00% (+4bp)" in line, line)
    check("line has calendar", "next Wed 28 Oct 14:00 FOMC Statement" in line, line)

    empty = {**snap, "calendar": [], "calendar_status": "ok"}
    payload = M.desk_payload(empty)
    eq("empty calendar status", payload["calendar_status"], "ok")
    check("empty calendar note", "no US high/medium-impact releases" in payload["calendar_note"])

    blind = {**snap, "calendar": [], "calendar_status": "unavailable"}
    payload = M.desk_payload(blind)
    eq("payload calendar status", payload.get("calendar_status"), "unavailable")
    eq("blind calendar note", payload.get("calendar_note"), "release calendar unavailable")

    disabled = {**snap, "calendar": [], "calendar_status": "off"}
    payload = M.desk_payload(disabled)
    eq("disabled calendar note", payload.get("calendar_note"), "release calendar disabled")

    legacy = {**snap, "calendar": [],
              "errors": [{"source": "macro:calendar", "error": "429"}]}
    legacy.pop("calendar_status", None)
    payload = M.desk_payload(legacy)
    eq("legacy blind status", payload.get("calendar_status"), "unavailable")


def test_moved() -> None:
    base = {"us10y": 4.96, "us2y": 4.65, "curve_2s10s": 0.31, "vix": 19.0}
    check("4bp move triggers", M.moved(base, {**base, "us10y": 5.00}))
    check("2bp move is quiet", not M.moved(base, {**base, "us10y": 4.98}))
    check("vix move triggers", M.moved(base, {**base, "vix": 20.2}))
    check("missing macro is quiet", not M.moved({}, base))


def test_live() -> None:
    from app.sources import Sources

    async def run() -> dict:
        async with Sources() as src:
            return await M.fetch(src, force=True)

    snap = asyncio.run(run())
    check("live has groups", bool(snap.get("groups")), str(snap.get("errors")))
    keys = {m["key"] for g in snap["groups"] for m in g.get("metrics", [])}
    for key in ("us2y", "us10y", "curve_2s10s", "vix", "hy_oas", "broad_dollar", "wti"):
        check(f"live has {key}", key in keys, str(sorted(keys)))
    check("live line", bool(snap.get("line")), snap.get("line"))
    print(M.render_text(snap))


def main() -> None:
    test_parsers()
    test_rates()
    test_credit_commodities_economy()
    test_markets()
    test_calendar()
    test_desk_payload_and_line()
    test_moved()
    if "--live" in sys.argv:
        test_live()
    print(f"OK — {PASS} checks passed" + (" (incl. live)" if "--live" in sys.argv else ""))


if __name__ == "__main__":
    main()
