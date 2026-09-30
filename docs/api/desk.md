# Sim desk — `/desk/*` (module `desk`, `addon/NT8BridgeDesk.cs`)

Repair verbs for the Strategies grid after a platform crash, plus two local reads.
Same conventions as `API.md`: JSON, UTF-8, `{"error":"…"}` on 4xx/5xx.

**Why.** After a crash NinjaTrader comes back with every strategy disabled. Re-enabling rows by hand
can also turn on stale rows of the same strategy, and a strategy disabled with
`CancelEntriesOnStrategyDisable=false` leaves its working orders at the broker with nobody managing
them. These verbs find those orders and rows by **strategy Id** and clean them up.

**Gates.** Every write goes through the order module's one door (`Ord_Guarded` / `Ord_Approve` in
`NT8BridgeOrders.cs`): `orders.enabled` must exist, the account must resolve to exactly one
`Provider.Simulator` / `Provider.Playback` account (a live account is refused; there is no switch),
the call without `confirm` is a dry run, and the confirm is a signed, one-shot token over the exact
plan, valid 30 s. An intent line is written to `orders.jsonl` before the act and one audit line per
call. The MCP tools add a **local lock** check (below) before anything is sent.

## Alive or dead

NinjaTrader enables a **clone** that shares the strategy Id, so the instance a grid row or an order
points at can read `Finalized` while its clone trades. Liveness is therefore judged per **Id** over
every instance in `StrategyBase.All`: an Id is **alive** when any instance with it is in Configure,
Active, DataLoaded, Historical, Transition or Realtime, and **dead** otherwise.

An order's owner is `Order.GetOwnerStrategy()`. When that answers null for an automated order, every
instance's own `Orders` collection is searched. An automated order whose owner still cannot be
found is listed as `ownerUnknown` and is never cancelled by `/desk/cancelOrphans`.

## Endpoints

| Method | Path | Body / query |
|---|---|---|
| GET | `/desk/orphans` | `?account=&instrument=` (optional). Per Simulator/Playback account: `orphans[]`, `ownerUnknown[]`; `hiddenNonSimulator` counts the rest |
| POST | `/desk/cancelOrphans` | `{account, instrument?, confirm?, issuedAt?}` |
| POST | `/desk/gridRemove` | `{account, strategyId, confirm?, issuedAt?}` |
| POST | `/desk/gridEnable` | `{account, strategyId, confirm?, issuedAt?}` |

`strategyId` is the `strategyId` of a `GET /strategies/running` row, sent as a string.

**cancelOrphans.** The plan lists every orphan (order id, owner id, owner state, order state) and the
`ownerUnknown` orders it will not touch. One `Account.Cancel` for all of them; each order is re-read
for up to 1.2 s. `ok` only when none is still live. Nothing is flattened. `409 nothingToDo` when there
are no orphans.

**gridRemove.** `StrategiesGrid.StrategyRemove` on one master row. Refused: the row is enabled or its
Id is alive (`409 rowEnabled`), the row was started by this AddOn (`409 bridgeRun` — use
`POST /strategy/stop`), the row is on another account (`409 accountMismatch`). Removing a row cancels
none of its orders.

**gridEnable.** The grid's own `StrategyEnable(strategy, ControlCenter, entry)` on one existing row,
then the state of the Id is polled for up to 8 s and the instance's own account is read back (a
moved instance is disabled and refused, like `/strategy/start`). Refused: no allowlist file
(`403 noAllowlist`), (strategy, account, instrument) not in it (`403 notAllowlisted`), the row is
enabled or alive (`409 alreadyEnabled`), another row or running instance of the same (strategy type,
account) is enabled or alive (`409 duplicate`).

## Files (outside the repo)

| File | Default path | Override | Read by |
|---|---|---|---|
| allowlist / expected legs | `Documents\NinjaTrader 8\nt8mcp\desk_legs.json` | `NT8MCP_DESK_LEGS` | the AddOn (its own process environment) for gridEnable; the MCP server for `nt_desk_status` |
| NT8 lock | `Documents\NinjaTrader 8\nt8mcp\nt8.lock` | `NT8MCP_LOCK_FILE` | the MCP server only |
| per-leg decisions | `Documents\NinjaTrader 8\nt8mcp\decisions\<strategy>_<account>.csv` | `NT8MCP_DECISIONS_DIR` (the folder) | the MCP server only (`nt_leg_decisions`, `nt_desk_status`) |

`desk_legs.json` (never created by this code; no file = every enable refused):

```json
{"legs": [
  {"strategy": "<StrategyTypeName>", "account": "<AccountName>", "instrument": "ES 12-26", "qty": 1,
   "decision_time": "09:35"}
]}
```

`strategy` matches the row's strategy type name or its grid display name; account and instrument
match case-insensitively. `qty` is reported by `nt_desk_status` and signed into the enable plan.
`decision_time` (optional, `"HH:MM"` Eastern) is when the leg's strategy writes its daily decision
line; only the MCP server reads it (the AddOn ignores it). A leg without it is never flagged silent.

The decisions file is appended by the strategy itself, one line per session, no header required
(a first field `date_et` marks a header line, which is skipped):

```
date_et,time_et,decision,reason,inputs
2026-09-30,09:35:01,ENTER_LONG,<reason text>,<free text, commas allowed>
```

`decision` is `ENTER_LONG` / `ENTER_SHORT` / `SKIP` / `NO_LABEL` / `OUTSIDE_HOURS` or other text;
`inputs` is everything after the 4th comma, kept as one string. `date_et` may be `YYYY-MM-DD`,
`MM/DD/YYYY` or `YYYYMMDD`.

`nt8.lock` is `{"holder": "<caller>", "expires": <epoch seconds>}`, written by `nt_lock(caller,
minutes)`. The three MCP writes send nothing unless it names their `caller` and has not expired. It
coordinates agents sharing one NinjaTrader; it is not a security gate.

## Restart recovery: `nt_desk_recover(caller, account=None, confirm=None, issued_at=None)`

MCP only, no new AddOn path: it is `nt_grid_enable` run once per leg under one plan.

1. Dry run. Read `desk_legs.json` (only the legs on `account`, when given) and
   `GET /strategies/running`. A leg with an enabled, alive or Realtime master row is `on` and left
   alone. For every other leg, the candidates are its disabled rows whose Id is dead (Terminated,
   Finalized or unreadable). **0 or more than 1 candidate = `refused`** with the ids: it never
   guesses. With exactly one, the AddOn's own `/desk/gridEnable` dry run is asked (so the lock,
   `orders.enabled`, Simulator/Playback only, allowlist and the duplicate guard all apply); its
   refusal makes the leg `refused`, and `duplicateGuard` shows `passed (AddOn dry run)`, the
   duplicate refusal, `not reached` or `not checked`. Answer: `{dryRun, legs[], planned, confirm,
   issuedAt}` (no confirm when nothing is planned).
2. Confirm. The same `confirm` and `issuedAt` within 30 s. The confirm is an HMAC made by the MCP
   server (a per-process key) over the caller, the account filter, `issuedAt` and the planned
   (strategy, account, instrument, strategyId) list; the plan is rebuilt NOW and must sign the
   same, and the confirm is accepted ONCE. Then each planned leg gets a fresh `/desk/gridEnable` dry
   run and its own one-shot AddOn confirm, in order. A second leg of the same (strategy, account)
   is refused there as a duplicate.
3. Read back. `GET /strategies/running` once more. Per leg `result` = `enabled` only when its row
   reads `Realtime`; otherwise `failed` with `reason` (the AddOn refusal, or the state seen).
   `ok` = every planned leg enabled. Legs not planned come back under `notTouched`.

## MCP reads (no AddOn write)

- `nt_desk_status()` — process start (`Get-Process NinjaTrader`), the last `Session Break` line in the
  NinjaTrader log, `connections` from `/health`, each leg of `desk_legs.json` against the grid
  (`rows`, `enabled`, `realtime`, `ok` = exactly one of each), duplicates by (strategy, account,
  instrument) among enabled or alive rows, orphans from `/desk/orphans`, enabled rows not in
  Realtime, the newest trace file's age, and `silentLegs` (legs past their `decision_time` today
  with no line for today, as in `nt_leg_decisions`). A part that could not be read is `null`, never
  empty.
- `nt_leg_decisions(strategy=None, days=5)`: per leg of `desk_legs.json` (or only `strategy`), the
  last `days` lines of its decisions file as `{date_et, time_et, decision, reason, inputs}`,
  `fileError` (`missing` / `unreadable: ...`), and `noLineToday`: `true` when it is a weekday, the
  leg's `decision_time` has passed (Eastern time) and no line is dated today; `false` otherwise;
  `null` for a leg without `decision_time`. Read in Python, not the AddOn: the server runs on the
  NinjaTrader PC and the files are plain text.
- `nt_crash_report(n)` — the last `n` trace lines stamped before the most recent `Session Break`,
  plus Windows System events 41/1074/6008 and Application events 1000/1026 within ±15 minutes of it.

## Not verified against a running NinjaTrader yet

Compile-checked only. What `GetOwnerStrategy()` and `StrategyBase.Orders` return for a terminated
instance, and what `StrategyEnable` does with an existing row's entry, are undocumented NinjaTrader
behaviour: check `nt_desk_status` and a dry run on your Simulator account before the first confirm.
