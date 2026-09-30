"""NT8 trade match — the pure math behind nt_match. See ../../docs/api/match.md.

Compares a research reference trade list with NinjaTrader's own trade list for the same rules:
per entry, by session date + side (+ entry time), then entry price, exit type and exit price.
Exit mismatches are checked against 1-minute bars for "same-bar ambiguous" (the target and the
stop both inside one bar, so a 1-minute fill model cannot know the order).

Standard library only. Reads CSV files it is handed; writes nothing, starts nothing.
`tools_match.py` loads the NT8 side (a CSV/JSON file, a status doc, a backtest) and calls match().
"""

from __future__ import annotations

import csv
import json
import re
from datetime import datetime, timedelta

EXIT_TYPES = ("target", "stop", "time", "other")

# canonical field -> column names tried in order (case-insensitive). NT8's own names (status doc,
# Strategy Analyzer "Trades" grid export) are in the same lists, so one normalizer reads both sides.
DEFAULT_COLUMNS = {
    "date": ["session_date", "date", "trade_date"],
    "side": ["dir", "side", "direction", "market pos.", "market pos", "position"],
    "entry_time": ["entry_time_et", "entry_time", "entrytime", "entry time"],
    "entry_price": ["entry_px", "entry_price", "entryprice", "entry price"],
    "exit_time": ["exit_time_et", "exit_time", "exittime", "exit time"],
    "exit_price": ["exit_px", "exit_price", "exitprice", "exit price"],
    "exit_type": ["exit_reason", "exit_type", "reason", "exitname", "exit name", "reason_raw"],
    "entry_name": ["entryname", "entry name", "entry_name"],
    "qty": ["qty", "quantity", "contracts"],
    "stop": ["stop_px", "stop_price", "stop"],
    "target": ["target_px", "target_price", "target"],
    "stop_pts": ["stop_pts"],
    "target_pts": ["target_pts"],
}

_TIME_FORMATS = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M",
                 "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M",
                 "%Y-%m-%d", "%m/%d/%Y", "%H:%M:%S", "%H:%M")


def _parse_dt(s) -> datetime | None:
    s = str(s or "").strip().replace("Z", "").split(".")[0].split("+")[0]
    if not s:
        return None
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _num(x):
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    try:
        return float(str(x).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def norm_side(x) -> str | None:
    s = str(x or "").strip().lower()
    if s in ("long", "buy", "l", "1", "+1", "1.0"):
        return "long"
    if s in ("short", "sell", "sellshort", "s", "-1", "-1.0"):
        return "short"
    return None


def exit_type(raw, exit_map: dict | None = None) -> str:
    """Raw exit name -> target / stop / time / other. exit_map {raw name: type} wins over the keywords."""
    if exit_map and str(raw) in exit_map:
        return exit_map[str(raw)]
    s = str(raw or "").lower()
    words = set(re.findall(r"[a-z]+", s))
    if "stop" in s or "sl" in words:
        return "stop"
    if "target" in s or "profit" in s or "limit" in s or "tp" in words:
        return "target"
    if words & {"time", "flat", "exit", "close", "eod", "session", "rule"} or re.search(r"\d\d:?\d\d", s):
        return "time"
    return "other"


def _pick(row: dict, field: str, columns: dict | None):
    """Value of one canonical field: the mapped column if given, else the first default name present."""
    lower = {k.strip().lower(): k for k in row}
    if columns and isinstance(columns.get(field), (int, float)):  # a number = the same value on every row
        return columns[field]
    names = [columns[field]] if columns and field in columns else DEFAULT_COLUMNS[field]
    for name in names:
        key = lower.get(str(name).strip().lower())
        if key is not None and str(row[key]).strip() != "":
            return row[key]
    return None


def normalize(rows, columns: dict | None = None, time_shift_min: int = 0, session_start: str = "18:00",
              exit_map: dict | None = None) -> list[dict]:
    """Rows (CSV dicts or NT8 trades[] dicts) -> [{date, side, entry, exit, entry_price, ...}].
    time_shift_min moves every time (NT8 stamps at the bar CLOSE; -1 gives the bar-open minute).
    With no date column the session date is the entry date, +1 day when the entry is at/after
    session_start (ETH evening). A row with no side or no entry time is skipped."""
    shift = timedelta(minutes=time_shift_min)
    out = []
    for r in rows:
        side = norm_side(_pick(r, "side", columns))
        date_raw = _pick(r, "date", columns)
        date = _parse_dt(date_raw) if date_raw is not None else None
        entry = _parse_dt(_pick(r, "entry_time", columns))
        exit_ = _parse_dt(_pick(r, "exit_time", columns))
        if side is None or entry is None:
            continue
        if entry.year == 1900 and date:  # time-only column: take the date column's day
            entry = datetime.combine(date.date(), entry.time())
        if exit_ is not None and exit_.year == 1900:
            exit_ = datetime.combine(entry.date(), exit_.time())
        entry += shift
        exit_ = exit_ + shift if exit_ else None
        if date is None:
            d = entry.date()
            if session_start and entry.strftime("%H:%M") >= session_start:
                d += timedelta(days=1)
        else:
            d = date.date()
        raw_exit = _pick(r, "exit_type", columns)
        t = {
            "date": d.isoformat(), "side": side, "entry": entry, "exit": exit_,
            "entry_price": _num(_pick(r, "entry_price", columns)),
            "exit_price": _num(_pick(r, "exit_price", columns)),
            "exit_raw": raw_exit, "exit_type": exit_type(raw_exit, exit_map),
            "entry_name": _pick(r, "entry_name", columns) or "",
            "qty": _num(_pick(r, "qty", columns)) or 1.0,
            "stop": _num(_pick(r, "stop", columns)), "target": _num(_pick(r, "target", columns)),
        }
        sign = 1 if side == "long" else -1
        for lvl, pts, s in (("stop", "stop_pts", -sign), ("target", "target_pts", sign)):
            p = _num(_pick(r, pts, columns))
            if t[lvl] is None and p is not None and t["entry_price"] is not None:
                t[lvl] = t["entry_price"] + s * p
        t["legs"] = 1
        out.append(t)
    return out


def group_scale_outs(trades: list[dict]) -> list[dict]:
    """NT8 writes one row per exit leg. Rows with the same side, entry minute and entry name are one
    entry: qty summed, entry price qty-weighted, exit time/price/type from the LAST leg (the runner)."""
    groups: dict = {}
    for t in trades:
        groups.setdefault((t["side"], t["entry"], t["entry_name"]), []).append(t)
    out = []
    for legs in groups.values():
        if len(legs) == 1:
            out.append(legs[0])
            continue
        legs = sorted(legs, key=lambda x: x["exit"] or x["entry"])
        qty = sum(x["qty"] for x in legs)
        g = dict(legs[-1])
        prices = [(x["entry_price"], x["qty"]) for x in legs if x["entry_price"] is not None]
        if prices:
            g["entry_price"] = sum(p * q for p, q in prices) / sum(q for _, q in prices)
        g.update(qty=qty, legs=len(legs))
        out.append(g)
    return sorted(out, key=lambda x: x["entry"])


def in_window(trades, from_date: str = "", to_date: str = ""):
    return [t for t in trades if (not from_date or t["date"] >= from_date) and (not to_date or t["date"] <= to_date)]


def _pair(refs: list[dict], nts: list[dict], tol_min):
    """Greedy nearest-entry-time pairing inside one (date, side) bucket."""
    pairs, used = [], set()
    for r in sorted(refs, key=lambda x: x["entry"]):
        best = None
        for i, n in enumerate(nts):
            if i in used:
                continue
            gap = abs((n["entry"] - r["entry"]).total_seconds()) / 60
            if tol_min is not None and gap > tol_min + 1e-9:
                continue
            if best is None or gap < best[0]:
                best = (gap, i)
        if best is None:
            pairs.append((r, None))
        else:
            used.add(best[1])
            pairs.append((r, nts[best[1]]))
    return pairs, [n for i, n in enumerate(nts) if i not in used]


def load_bars(path: str, minutes: set[str], time_is_close: bool = False) -> dict:
    """Stream a 1-minute OHLC CSV, keep only the wanted 'YYYY-MM-DD HH:MM' bar-open minutes.
    Time column: time/datetime/timestamp/date_time, or date + time. Prices: open/high/low/close or o/h/l/c."""
    shift = timedelta(minutes=-1 if time_is_close else 0)
    out = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            low = {k.strip().lower(): v for k, v in row.items() if k}
            ts = next((low[k] for k in ("time", "datetime", "timestamp", "date_time", "ts") if low.get(k)), None)
            if ts is not None and len(str(ts).strip()) <= 8 and low.get("date"):
                ts = f"{low['date']} {ts}"
            elif ts is None and low.get("date"):
                ts = low["date"]
            dt = _parse_dt(ts)
            if dt is None:
                continue
            key = (dt + shift).strftime("%Y-%m-%d %H:%M")
            if key in minutes:
                out[key] = bar_of(low)
    return out


def bar_of(d: dict) -> dict:
    g = lambda *ks: next((_num(d[k]) for k in ks if k in d), None)  # noqa: E731
    return {"h": g("high", "h"), "l": g("low", "l")}


def bars_from_list(bars: list[dict], time_is_close: bool = True) -> dict:
    """nt_bars rows ({time, o, h, l, c}) -> {bar-open minute: bar}. NT8 stamps bars at the close."""
    shift = timedelta(minutes=-1 if time_is_close else 0)
    out = {}
    for b in bars or []:
        dt = _parse_dt(b.get("time"))
        if dt is not None:
            out[(dt + shift).strftime("%Y-%m-%d %H:%M")] = bar_of({k.lower(): v for k, v in b.items()})
    return out


def _minute(dt) -> str | None:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else None


def exit_minutes(r, n) -> list[str]:
    return sorted({m for m in (_minute(r["exit"]), _minute(n["exit"])) if m})


def _levels(r, n):
    stop = r["stop"] if r["stop"] is not None else next(
        (x["exit_price"] for x in (r, n) if x["exit_type"] == "stop"), None)
    target = r["target"] if r["target"] is not None else next(
        (x["exit_price"] for x in (r, n) if x["exit_type"] == "target"), None)
    return stop, target


def ambiguous(r, n, bars: dict | None):
    """True when one bar at either side's exit minute holds both the stop and the target; False when
    the bars are there and none does; None when a level or every bar is unknown."""
    stop, target = _levels(r, n)
    if bars is None or stop is None or target is None:
        return None
    lo, hi = min(stop, target), max(stop, target)
    seen = False
    for m in exit_minutes(r, n):
        b = bars.get(m)
        if b and b["h"] is not None and b["l"] is not None:
            seen = True
            if b["l"] <= lo and b["h"] >= hi:
                return True
    return False if seen else None


def _row(status, r, n, amb=None, news=None):
    def side(t, p):
        if t is None:
            return {f"{p}_side": None}
        return {f"{p}_side": t["side"], f"{p}_entry": t["entry"].strftime("%H:%M"), f"{p}_entry_price": t["entry_price"],
                f"{p}_exit": t["exit"].strftime("%H:%M") if t["exit"] else None, f"{p}_exit_price": t["exit_price"],
                f"{p}_exit_type": t["exit_type"], f"{p}_exit_raw": t["exit_raw"]}
    date = (r or n)["date"]
    return {"date": date, "status": status, **side(r, "ref"), **side(n, "nt8"),
            "ambiguous": amb, "news": news or []}


def _pct(a, b):
    return round(100.0 * a / b, 2) if b else None


def match(ref: list[dict], nt8: list[dict], tick_size: float = 0.25, price_tol_ticks: float = 1,
          entry_tol_min: float | None = 5, bars: dict | None = None,
          news_minutes=("08:30", "10:00", "14:00"), threshold: float = 95.0, gate: str = "date_side",
          max_rows: int = 500) -> dict:
    """ref / nt8: normalized trades (normalize(); nt8 already grouped). Returns counts, the three match
    percentages, a per-mismatch table and a PASS/FAIL on `gate` ("date_side" | "full" | "full_ex_ambiguous")."""
    tol = price_tol_ticks * tick_size + 1e-9
    close = lambda a, b: a is not None and b is not None and abs(a - b) <= tol  # noqa: E731
    news_set = set(news_minutes or ())

    buckets: dict = {}
    for t in ref:
        buckets.setdefault((t["date"], t["side"]), ([], []))[0].append(t)
    for t in nt8:
        buckets.setdefault((t["date"], t["side"]), ([], []))[1].append(t)

    pairs, ref_left, nt_left = [], [], []
    for key in sorted(buckets):
        p, left = _pair(*buckets[key], entry_tol_min)
        pairs += [x for x in p if x[1] is not None]
        ref_left += [x[0] for x in p if x[1] is None]
        nt_left += left

    rows = []
    # a ref entry with no same-side partner but an opposite-side NT8 entry that day = side flip
    flips = []
    for r in ref_left:
        other = next((n for n in nt_left if n["date"] == r["date"] and n["side"] != r["side"]), None)
        if other is None:
            rows.append(_row("ref_only", r, None))
        else:
            nt_left.remove(other)
            flips.append((r, other))
            rows.append(_row("side_flip", r, other))
    rows += [_row("nt8_only", None, n) for n in nt_left]

    full = amb_n = entry_ok = 0
    for r, n in pairs:
        if not close(r["entry_price"], n["entry_price"]):
            rows.append(_row("entry_price", r, n))
            continue
        entry_ok += 1
        # a reference with no exit price (outcome-only lists) is judged on the exit type alone
        if r["exit_type"] == n["exit_type"] and (r["exit_price"] is None or close(r["exit_price"], n["exit_price"])):
            full += 1
            continue
        status = "exit_type" if r["exit_type"] != n["exit_type"] else "exit_price"
        amb = ambiguous(r, n, bars)
        amb_n += bool(amb)
        mins = {m[-5:] for m in exit_minutes(r, n)} | {t["entry"].strftime("%H:%M") for t in (r, n)}
        rows.append(_row(status, r, n, amb, sorted(mins & news_set)))
    for row in rows:  # news flag on every row, not just exit mismatches
        if not row["news"]:
            mins = {row.get(k) for k in ("ref_entry", "ref_exit", "nt8_entry", "nt8_exit")}
            row["news"] = sorted(mins & news_set)

    n_ref = len(ref)
    pct = {"date_side": _pct(len(pairs), n_ref), "full": _pct(full, n_ref),
           "full_ex_ambiguous": _pct(full, n_ref - amb_n)}
    if gate not in pct:
        return {"error": f"gate must be one of {sorted(pct)}"}
    rows.sort(key=lambda x: (x["date"], x["status"]))
    status_counts: dict = {}
    for row in rows:
        status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1
    passed = pct[gate] is not None and pct[gate] >= threshold
    return {
        "verdict": "PASS" if passed else "FAIL",
        "gate": gate, "threshold": threshold, "pct": pct,
        "counts": {"ref": n_ref, "nt8": len(nt8), "date_side": len(pairs), "entry_ok": entry_ok, "full": full,
                   "ambiguous": amb_n, "side_flip": len(flips), **{k: v for k, v in status_counts.items() if k != "side_flip"}},
        "summary": (f"date+side {len(pairs)}/{n_ref} ({pct['date_side']}%), full {full}/{n_ref} ({pct['full']}%), "
                    f"full without {amb_n} ambiguous {full}/{n_ref - amb_n} ({pct['full_ex_ambiguous']}%) -> "
                    f"{'PASS' if passed else 'FAIL'} on {gate} >= {threshold}%"),
        "mismatches": rows[:max_rows] if max_rows else rows,
        "mismatchesTruncated": bool(max_rows) and len(rows) > max_rows,
    }


def read_csv(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def read_trades_file(path: str) -> list[dict]:
    """A .json status doc / run record / bare trades list, or a CSV."""
    if path.lower().endswith(".json"):
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        return doc.get("trades", []) if isinstance(doc, dict) else doc
    return read_csv(path)
