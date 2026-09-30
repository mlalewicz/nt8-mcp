# Trade match — `nt_match`

Module: `server/nt8_mcp/match.py` (pure functions, standard library only) + `server/nt8_mcp/tools_match.py`
(the one MCP tool). No AddOn endpoint of its own. Reads files and existing tools; writes nothing.
The only thing it can start is a Backtest-account run, and only when you pass `backtest=`.

The question this answers: **does NinjaTrader trade the same entries as the research reference?**
A strategy ported to NinjaScript should reproduce the research trade list before it goes to Sim.
The usual gate is 95% or more on date + side.

## `nt_match(...)`

```
nt_match(reference_csv, nt8_trades=None, backtest=None, backtest_id="", run_id="",
         columns=None, nt8_columns=None, exit_map=None, from_date="", to_date="",
         tick_size=0.25, price_tol_ticks=1, entry_tol_min=5, nt8_time_shift_min=-1,
         session_start="18:00", bars_csv="", bars_time_is_close=False, bars_chart="", bars_n=5000,
         news_minutes=None, threshold=95.0, gate="date_side", max_rows=500)
```

### Inputs

`reference_csv` is the research trade list, one row per entry.

The NT8 side is **exactly one** of these (zero or two is an `{"error"}`):

| Argument | Loads |
|---|---|
| `nt8_trades` | a path to a `.json` status doc / saved run / bare trades list, or to a `.csv` export; or a trades list / status doc passed in directly |
| `backtest` | a dict of `nt_backtest` keyword arguments: the run happens now (Backtest account only) |
| `backtest_id` | a finished backtest, through `nt_backtest_status` |
| `run_id` | a saved run, through `nt_run` |

A backtest that is not `state: "done"` is refused with `{"error": "NT8 side not usable", "doc": {...}}`.

### Columns

Both sides go through one normalizer. Each canonical field is looked up by a list of default column
names (case-insensitive), NinjaTrader's own names included:

| Field | Default names |
|---|---|
| `date` | `session_date`, `date`, `trade_date` |
| `side` | `dir`, `side`, `direction`, `Market pos.`, `position` (long/short, buy/sell, L/S, 1/-1) |
| `entry_time` / `exit_time` | `entry_time_et`, `entry_time`, `entryTime`, `Entry time` (and the exit twins) |
| `entry_price` / `exit_price` | `entry_px`, `entry_price`, `entryPrice`, `Entry price` (and the exit twins) |
| `exit_type` | `exit_reason`, `exit_type`, `reason`, `exitName`, `Exit name` |
| `entry_name`, `qty` | `entryName` / `Entry name`, `qty` / `Quantity` |
| `stop`, `target` | `stop_px`, `target_px` (price levels, used for the ambiguity check) |
| `stop_pts`, `target_pts` | distance from the entry, turned into levels by side |

`columns` (reference) and `nt8_columns` (NT8 side) map a field to another column name, for example
`{"side": "direction", "exit_type": "outcome"}`. A **number** instead of a name is a constant for
every row: `{"stop_pts": 10, "target_pts": 10}` for a fixed bracket.

Times may be full datetimes or `HH:MM[:SS]` with a separate date column. With no date column the
session date is the entry date, plus one day when the entry is at or after `session_start` (ETH
evening; `""` turns that off).

NT8 times move by `nt8_time_shift_min` (default `-1`): NinjaTrader stamps a bar, and a fill on it,
at the bar's **close**; research lists use the bar's **open** minute.

### Exit types

`target`, `stop`, `time` or `other`, from the raw exit name: a name with "stop" or the word "SL" is
a stop; "target", "profit", "limit" or "TP" is a target; "time", "flat", "exit", "close", "eod",
"session", "rule" or a clock time (`15:30 rule`) is a time exit; anything else is `other`.
`exit_map` (`{"raw name": "type"}`) wins over the keywords.

### Scale-outs

NinjaTrader writes one trade row per exit leg. Rows with the same side, entry minute and entry name
are one entry: quantities add up, the entry price is the quantity-weighted mean, and the exit time,
price and type are those of the **last** leg (the runner). `legs` is not shown in the output; the
NT8 count is entries, not rows.

## Matching

1. Filter both sides to `from_date..to_date` (session dates, inclusive).
2. Per (session date, side): pair each reference entry with the NT8 entry nearest in time, within
   `entry_tol_min` minutes (`None` = any time that day). Paired = **date + side match**.
3. A reference entry left over, with a spare NT8 entry of the other side that day, is a
   `side_flip`. Other leftovers are `ref_only` / `nt8_only`.
4. A pair is a **full match** when the entry price, the exit type and the exit price all agree,
   prices within `price_tol_ticks * tick_size`. A reference with no exit price (an outcome-only
   list) is judged on the exit type alone. A failed pair is `entry_price`, `exit_type` or
   `exit_price`.

## Same-bar ambiguity and news minutes

For every `exit_type` / `exit_price` row, the tool reads the 1-minute bar at each side's exit minute.
The row is `ambiguous: true` when one of those bars holds both the stop and the target (low at or
under the lower level, high at or over the higher). A 1-minute fill model cannot know which one
filled first, so a difference there is the data, not the rule. `false` = the bars are there and no
bar holds both. `null` = no bars, or a level is unknown. Levels come from the reference's
`stop`/`target` (or `*_pts`) columns, else from the two exit prices (the side that stopped gives the
stop, the side that hit the target gives the target).

Bars come from `bars_csv` (a 1-minute CSV: a `time`/`datetime`/`timestamp` column or `date` + `time`,
and `high`/`low` or `h`/`l`; stamped at the bar open unless `bars_time_is_close=True`; only the
needed minutes are kept) or from `bars_chart` (`nt_bars`, the last `bars_n` bars of that chart only,
so it covers recent trades only).

`news` on every row lists the entry/exit minutes that fall on `news_minutes`
(default `08:30`, `10:00`, `14:00`, the time zone of the lists).

## Output

```json
{
  "verdict": "PASS",
  "gate": "date_side", "threshold": 95.0,
  "pct": {"date_side": 99.27, "full": 97.81, "full_ex_ambiguous": 97.81},
  "counts": {"ref": 137, "nt8": 139, "date_side": 136, "entry_ok": 136, "full": 134,
             "ambiguous": 0, "side_flip": 0, "ref_only": 1, "nt8_only": 3, "exit_type": 1, "exit_price": 1},
  "summary": "date+side 136/137 (99.27%), full 134/137 (97.81%), ... -> PASS on date_side >= 95.0%",
  "mismatches": [{"date": "2020-06-12", "status": "ref_only", "ref_side": "short", "ref_entry": "09:30",
                  "ref_entry_price": 9500.0, "ref_exit": "10:12", "ref_exit_price": 9480.0,
                  "ref_exit_type": "target", "ref_exit_raw": "ema_limit", "nt8_side": null,
                  "ambiguous": null, "news": []}],
  "mismatchesTruncated": false
}
```

- `pct.date_side` = pairs / reference entries. `pct.full` = full matches / reference entries.
  `pct.full_ex_ambiguous` = full matches / (reference entries - ambiguous rows).
- `verdict` = `pct[gate] >= threshold`. `gate` is `date_side` (default), `full` or
  `full_ex_ambiguous`.
- `mismatches` is capped at `max_rows` (0 = no cap); `mismatchesTruncated` says when.

## Known limits

- Times on both sides must be in the same time zone. The tool does not convert.
- `full` compares exit prices as given. A reference that books a time exit at the last bar's close
  while NinjaTrader fills at the next bar's open will show `exit_price` rows on time exits; use
  `gate="date_side"` or an `exit_map` / column choice that fits the rule.
- Roll-date differences (NinjaTrader's merge policy vs the research data's roll day) show as entry
  price or ref/nt8-only rows. The tool does not know roll dates.
