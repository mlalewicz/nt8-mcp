"""nt_match — NT8 trade match against a research reference list. See ../../docs/api/match.md.

This file only LOADS the inputs (reference CSV; the NT8 side from a file, a doc, a finished backtest
or a new nt_backtest run; 1-minute bars from a CSV or nt_bars) and calls match.py.
"""

from nt8_mcp import match as m
from nt8_mcp.app import mcp
from nt8_mcp.tools_backtest import nt_backtest, nt_backtest_status
from nt8_mcp.tools_core import nt_bars
from nt8_mcp.tools_runs import nt_run


def _nt8_rows(nt8_trades, backtest, backtest_id, run_id):
    given = sum([nt8_trades is not None, bool(backtest), bool(backtest_id), bool(run_id)])
    if given != 1:
        return None, {"error": "give exactly one of nt8_trades, backtest, backtest_id, run_id"}
    if isinstance(nt8_trades, str):
        try:
            return m.read_trades_file(nt8_trades), None
        except OSError as e:
            return None, {"error": f"cannot read nt8_trades: {e}"}
    if nt8_trades is not None:
        return (nt8_trades.get("trades", []) if isinstance(nt8_trades, dict) else nt8_trades), None
    if backtest:
        doc = nt_backtest(**backtest)
    elif backtest_id:
        doc = nt_backtest_status(backtest_id)
    else:
        doc = nt_run(run_id)
    if not isinstance(doc, dict) or doc.get("error") or doc.get("state", "done") != "done":
        return None, {"error": "NT8 side not usable", "doc": {k: doc.get(k) for k in ("id", "state", "error", "note")}
                      if isinstance(doc, dict) else doc}
    return doc.get("trades") or [], None


@mcp.tool(name="nt_match")
def nt_match(
    reference_csv: str,
    nt8_trades: str | list | dict | None = None,
    backtest: dict | None = None,
    backtest_id: str = "",
    run_id: str = "",
    columns: dict | None = None,
    nt8_columns: dict | None = None,
    exit_map: dict | None = None,
    from_date: str = "",
    to_date: str = "",
    tick_size: float = 0.25,
    price_tol_ticks: float = 1,
    entry_tol_min: float | None = 5,
    nt8_time_shift_min: int = -1,
    session_start: str = "18:00",
    bars_csv: str = "",
    bars_time_is_close: bool = False,
    bars_chart: str = "",
    bars_n: int = 5000,
    news_minutes: list | None = None,
    threshold: float = 95.0,
    gate: str = "date_side",
    max_rows: int = 500,
) -> dict:
    """NT8 trade match: does NinjaTrader trade the same entries as the research reference? (The >=95%
    gate before a strategy goes to Sim.) Reads only; the one exception is `backtest`, which runs
    nt_backtest (Backtest account only).

    reference_csv: the research trade list. `columns` maps canonical fields to its column names when the
    defaults miss (date, side, entry_time, entry_price, exit_time, exit_price, exit_type, qty, stop,
    target, stop_pts, target_pts), e.g. {"side": "direction", "exit_type": "why"}.
    NT8 side, exactly ONE of: `nt8_trades` (a path to a .csv/.json export, a trades[] list or a status
    doc), `backtest` (nt_backtest kwargs: runs it now), `backtest_id` (nt_backtest_status), `run_id`
    (nt_run). `nt8_columns` maps its columns the same way. NT8 times are moved by `nt8_time_shift_min`
    (default -1: NT8 stamps a bar at its close, references use the bar-open minute). Scale-out rows
    (same side + entry minute + entry name) are grouped into one entry.

    Match: same session date + side, entry time within `entry_tol_min` minutes (None = any time that
    day), then entry price, exit type (target/stop/time/other; `exit_map` {raw name: type} overrides the
    keyword rule) and exit price, within `price_tol_ticks` * `tick_size`. Exit mismatches are checked on
    the 1-minute bar(s) at both exit minutes: `ambiguous` = one bar holds both the stop and the target
    (levels from the reference's stop/target columns, else the two exit prices). Bars from `bars_csv`
    (bar-open stamps unless bars_time_is_close) or `bars_chart` (nt_bars, last `bars_n` bars only).
    `news` lists the entry/exit minutes that fall on `news_minutes` (default 08:30, 10:00, 14:00).

    Returns verdict PASS/FAIL (`gate` "date_side" | "full" | "full_ex_ambiguous" >= `threshold`), pct
    {date_side, full, full_ex_ambiguous}, counts, a one-line summary and the mismatch table."""
    try:
        ref_rows = m.read_csv(reference_csv)
    except OSError as e:
        return {"error": f"cannot read reference_csv: {e}"}
    nt_rows, err = _nt8_rows(nt8_trades, backtest, backtest_id, run_id)
    if err:
        return err
    ref = m.in_window(m.normalize(ref_rows, columns, 0, session_start, exit_map), from_date, to_date)
    nt8 = m.in_window(m.group_scale_outs(m.normalize(nt_rows, nt8_columns, nt8_time_shift_min, session_start,
                                                     exit_map)), from_date, to_date)
    if not ref:
        return {"error": "no reference trades in the window (check columns / from_date / to_date)"}

    bars = None
    if bars_csv:
        wanted = {x.strftime("%Y-%m-%d %H:%M") for t in ref + nt8 for x in (t["exit"],) if x}
        try:
            bars = m.load_bars(bars_csv, wanted, bars_time_is_close)
        except OSError as e:
            return {"error": f"cannot read bars_csv: {e}"}
    elif bars_chart:
        got = nt_bars(bars_chart, bars_n)
        if isinstance(got, dict) and got.get("error"):
            return got
        bars = m.bars_from_list(got)

    news = ("08:30", "10:00", "14:00") if news_minutes is None else news_minutes
    return m.match(ref, nt8, tick_size, price_tol_ticks, entry_tol_min, bars, news, threshold, gate, max_rows)
