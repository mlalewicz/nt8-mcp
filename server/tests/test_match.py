"""match.py (pure) and tools_match.nt_match (input loading) against synthetic trades. No NinjaTrader."""

import csv
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [_HERE, os.path.join(_HERE, "..")]

from fake_addon import FakeAddon  # noqa: E402
from nt8_mcp import app  # noqa: E402
from nt8_mcp.tools_match import nt_match  # noqa: E402

REF_HEADER = ["session_date", "dir", "entry_time_et", "entry_px", "exit_time_et", "exit_px", "exit_reason"]


def _ref(*rows, header=REF_HEADER):
    f = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="")
    w = csv.writer(f)
    w.writerow(header)
    w.writerows(rows)
    f.close()
    return f.name


def _nt(date, side, entry, entry_px, exit_, exit_px, exit_name, qty=1, entry_name="E"):
    """An NT8 trades[] row; times are NT8 bar-CLOSE stamps (reference minute + 1)."""
    return {"side": side, "qty": qty, "entryName": entry_name, "exitName": exit_name,
            "entryTime": f"{date}T{entry}:00", "exitTime": f"{date}T{exit_}:00",
            "entryPrice": entry_px, "exitPrice": exit_px}


R1 = ["2026-01-05", "long", "09:30", 100.0, "10:00", 110.0, "target"]
R2 = ["2026-01-06", "short", "09:30", 200.0, "09:45", 205.0, "stop"]
N1 = _nt("2026-01-05", "Long", "09:31", 100.0, "10:01", 110.0, "Profit target")
N2 = _nt("2026-01-06", "Short", "09:31", 200.0, "09:46", 205.0, "Stop loss")


def test_exact_match():
    r = nt_match(_ref(R1, R2), nt8_trades=[N1, N2])
    assert r["verdict"] == "PASS" and r["pct"] == {"date_side": 100.0, "full": 100.0, "full_ex_ambiguous": 100.0}, r
    assert r["mismatches"] == [] and r["counts"]["full"] == 2


def test_missing_trade_fails_gate():
    r = nt_match(_ref(R1, R2), nt8_trades=[N1])
    assert r["verdict"] == "FAIL" and r["pct"]["date_side"] == 50.0, r
    assert [x["status"] for x in r["mismatches"]] == ["ref_only"] and r["mismatches"][0]["date"] == "2026-01-06"


def test_extra_nt8_trade_is_listed_not_counted():
    extra = _nt("2026-01-07", "Long", "09:31", 1.0, "09:40", 2.0, "Profit target")
    r = nt_match(_ref(R1, R2), nt8_trades=[N1, N2, extra])
    assert r["pct"]["date_side"] == 100.0 and r["counts"]["nt8_only"] == 1, r


def test_side_flip():
    flipped = _nt("2026-01-06", "Long", "09:31", 200.0, "09:46", 195.0, "Stop loss")
    r = nt_match(_ref(R1, R2), nt8_trades=[N1, flipped])
    assert r["counts"]["side_flip"] == 1 and r["pct"]["date_side"] == 50.0, r
    assert r["mismatches"][0]["status"] == "side_flip" and "nt8_only" not in r["counts"]


def test_scale_out_rows_are_one_entry():
    legs = [_nt("2026-01-05", "Long", "09:31", 100.0, "09:50", 105.0, "Profit target", qty=1),
            _nt("2026-01-05", "Long", "09:31", 100.0, "10:01", 110.0, "Profit target", qty=1)]
    r = nt_match(_ref(R1), nt8_trades=legs)
    assert r["counts"]["nt8"] == 1 and r["counts"]["full"] == 1, r  # judged on the runner's exit


def test_ambiguous_bar():
    # ref says stop, NT8 says target; the 09:45 bar spans both 205 (stop) and 190 (target) -> ambiguous
    nt_target = _nt("2026-01-06", "Short", "09:31", 200.0, "09:46", 190.0, "Profit target")
    bars = _ref(["2026-01-06 09:45", 201, 206, 189, 195], header=["time", "open", "high", "low", "close"])
    r = nt_match(_ref(R1, R2), nt8_trades=[N1, nt_target], bars_csv=bars)
    row = r["mismatches"][0]
    assert row["status"] == "exit_type" and row["ambiguous"] is True, row
    assert r["counts"]["ambiguous"] == 1 and r["pct"]["full"] == 50.0 and r["pct"]["full_ex_ambiguous"] == 100.0
    # a bar that holds only one level is not ambiguous
    bars = _ref(["2026-01-06 09:45", 201, 206, 195, 195], header=["time", "open", "high", "low", "close"])
    assert nt_match(_ref(R1, R2), nt8_trades=[N1, nt_target], bars_csv=bars)["mismatches"][0]["ambiguous"] is False
    # no bars -> unknown, never guessed
    assert nt_match(_ref(R1, R2), nt8_trades=[N1, nt_target])["mismatches"][0]["ambiguous"] is None


def test_news_minute_flag():
    ref = _ref(["2026-01-05", "long", "10:00", 100.0, "10:30", 110.0, "target"])
    r = nt_match(ref, nt8_trades=[_nt("2026-01-05", "Long", "10:01", 101.0, "10:31", 110.0, "Profit target")])
    assert r["mismatches"][0]["status"] == "entry_price" and r["mismatches"][0]["news"] == ["10:00"], r


def test_price_tolerance_edges():
    one_tick = _nt("2026-01-05", "Long", "09:31", 100.25, "10:01", 109.75, "Profit target")
    assert nt_match(_ref(R1), nt8_trades=[one_tick])["counts"]["full"] == 1
    two_ticks = _nt("2026-01-05", "Long", "09:31", 100.0, "10:01", 110.5, "Profit target")
    r = nt_match(_ref(R1), nt8_trades=[two_ticks])
    assert r["counts"]["full"] == 0 and r["mismatches"][0]["status"] == "exit_price", r
    assert nt_match(_ref(R1), nt8_trades=[two_ticks], price_tol_ticks=2)["counts"]["full"] == 1


def test_entry_time_tolerance_edges():
    at5 = _nt("2026-01-05", "Long", "09:36", 100.0, "10:01", 110.0, "Profit target")  # 09:35 bar-open = 5 min
    at6 = _nt("2026-01-05", "Long", "09:37", 100.0, "10:01", 110.0, "Profit target")
    assert nt_match(_ref(R1), nt8_trades=[at5])["counts"]["date_side"] == 1
    assert nt_match(_ref(R1), nt8_trades=[at6])["counts"]["date_side"] == 0
    assert nt_match(_ref(R1), nt8_trades=[at6], entry_tol_min=None)["counts"]["date_side"] == 1


def test_column_mapping_and_constants():
    ref = _ref(["2026-01-06", "S", "09:30:00", 200.0, "SL"],
               header=["day", "direction", "t_in", "px_in", "outcome"])
    nt_target = _nt("2026-01-06", "Short", "09:31", 200.0, "09:31", 190.0, "Profit target")
    cols = {"date": "day", "side": "direction", "entry_time": "t_in", "entry_price": "px_in",
            "exit_type": "outcome", "stop_pts": 10, "target_pts": 10}
    bars = _ref(["2026-01-06 09:31", 200, 211, 189, 195], header=["time", "open", "high", "low", "close"])
    r = nt_match(ref, nt8_trades=[nt_target], columns=cols, bars_csv=bars, bars_time_is_close=True)
    assert r["counts"]["date_side"] == 1 and r["mismatches"][0]["ambiguous"] is True, r
    assert nt_match(ref, nt8_trades=[nt_target])["error"].startswith("no reference trades")


def test_nt8_csv_export_and_window():
    path = _ref(["1", "Long", "1", "100", "110", "1/5/2026 9:31:00 AM", "1/5/2026 10:01:00 AM", "Profit target"],
                header=["Trade number", "Market pos.", "Qty", "Entry price", "Exit price", "Entry time", "Exit time",
                        "Exit name"])
    r = nt_match(_ref(R1, R2), nt8_trades=path, to_date="2026-01-05")
    assert r["counts"]["ref"] == 1 and r["counts"]["full"] == 1, r


def test_exactly_one_nt8_source():
    assert "exactly one" in nt_match(_ref(R1))["error"]
    assert "exactly one" in nt_match(_ref(R1), nt8_trades=[N1], backtest_id="b1")["error"]


def test_backtest_mode_runs_nt_backtest():
    saved, app.POLL_S = app.POLL_S, 0
    try:
        with FakeAddon() as fake:
            fake.json("/backtest", {"id": "b1", "state": "queued"}, method="POST", status=202)
            fake.json("/backtest/b1", {"id": "b1", "state": "done", "trades": [N1, N2]})
            r = nt_match(_ref(R1, R2), backtest={"strategy": "SampleMACrossOver", "save_run": False})
            fake.json("/backtest/b2", {"id": "b2", "state": "error", "error": "no bars"})
            bad = nt_match(_ref(R1, R2), backtest_id="b2")
    finally:
        app.POLL_S = saved
    assert r["verdict"] == "PASS" and r["counts"]["full"] == 2, r
    assert bad["error"] == "NT8 side not usable" and bad["doc"]["error"] == "no bars", bad
