# `nt_lint` — NinjaScript hazard linter (Python only, no AddOn)

Module: `server/nt8_mcp/lint.py` (rules + scanner) · `server/nt8_mcp/tools_lint.py` (MCP tool) ·
`scripts/nt_lint.py` (CLI, needs no MCP install).

A static, read-only scan of NinjaScript `.cs` files for patterns that hurt a running NinjaTrader:
frozen charts, hung Replay, orders left working after a strategy is disabled, disk thrash on
backfill, and the calls a third-party script should never make without a review. It reads text
only: no NinjaTrader, no AddOn, no compile.

## Use

```
python scripts/nt_lint.py Indicators/ Strategies/MyStrategy.cs "AddOns/**/*.cs"
python scripts/nt_lint.py --rule NT05 Strategies/       # one rule (repeatable)
python scripts/nt_lint.py --json Indicators/            # the full result as JSON
python scripts/nt_lint.py --list                        # the rule table
nt8 nt_lint --paths '["Indicators/"]'                   # the same through the nt8 CLI
```

Paths are files, folders (every `*.cs` below, recursively) or globs. CLI exit code: `0` clean,
`1` findings or unreadable files.

MCP: `nt_lint(paths, rules=None)` returns
`{files, counts: {rule: n}, findings: [...], errors: [...], truncated}`. Each finding is
`{file, line, rule, name, severity, detail, hint, code}`; `code` is the source line. Findings are
capped at 500 (`truncated: true`). An unknown rule id returns `{"error": ...}`.

## Rules

| Id | Severity | Flags | Fix hint |
|---|---|---|---|
| NT01 | high | An **indicator** calls `AddDataSeries`/`AddVolumetric` for another instrument (first argument a string literal, a `string` field/property, or an instrument name before a bars period) | Load the other instrument with `BarsRequest`. A cross-instrument series in a chart indicator froze every chart |
| NT02 | high | `MarketDepth.Update +=` inside `Task.Run` / `Task.Factory.StartNew` / `ThreadPool.QueueUserWorkItem` (directly or one call deep), and no `Dispatcher.Run()` in the file | Subscribe on a dedicated thread that runs `Dispatcher.Run()`. A pool-thread subscribe hung Playback |
| NT03 | high | `Suspend` / `Resume` / `Abort` / `ResetAbort` on `Thread`, `Thread.CurrentThread` or a variable declared as a `Thread` | Signal the thread to exit (a flag or a `CancellationToken`) |
| NT04 | medium | A file write (`File.Write*/Append*/Create*/OpenWrite/Copy/Move/Replace`, `new StreamWriter`, `new FileStream` not read-only) in an indicator or strategy, reached from a data event (up to 3 calls deep) with no `State.Realtime`/`State.Historical` (or `*Realtime*`/`*Historical*` flag) check on the way | Guard it with `State == State.Realtime`. `OnMarketDepth` does not count: it never runs on historical data |
| NT04N | medium | Any network call: `new WebClient/HttpClient/TcpClient/UdpClient/TcpListener/Socket/HttpListener/SmtpClient`, `WebRequest.Create`, `Dns.GetHost*` | Review where it sends data; never from a data event without a timeout |
| NT05 | high | A **strategy** calls `Enter*` or `SubmitOrderUnmanaged` and nothing cancels its working orders when it leaves Realtime: no `CancelOrder(` / `Account.Cancel(` in a block (or a braceless `if`) keyed on `State.Terminated` or `State != State.Realtime`, directly or one call deep. Reported at the first entry | Cancel them yourself (for example `CancelOrder` on each working order in `State.Terminated`), or have whoever starts the strategy set the global option `NinjaTrader.Core.Globals.StrategiesOptions.CancelEntriesOnStrategyDisable`. That option is not a `StrategyBase` member: `CancelEntriesOnStrategyDisable = true;` in a strategy does not compile (CS0103). NinjaTrader's default leaves the orders working |
| NT06 | low | `Print(` in `OnBarUpdate` / `OnMarketData` / `OnMarketDepth` with no guard: no enclosing `if` that mentions `State`, `Realtime`, `Historical`, a once flag (`once`, `printed`, `warned`, `logged`), `Debug`/`Verbose`/`Trace`/`Log*`/`Print*`, `IsLastBarOfChart` or `Count -`, not in a `catch`, and no earlier `if (State ...) return;` | Guard it; unguarded prints run on every historical bar and flood the Output window |
| NT07 | high | `Process.Start`, `ProcessStartInfo`, `Registry` / `RegistryKey`, `File.Delete`, `Directory.Delete`, `Environment.Exit` / `FailFast` | Third-party code needs a review before install; own code must justify it |
| NT08 | high | Synchronous `Dispatcher.Invoke(` reached from a data event (up to 3 calls deep) | Use `Dispatcher.InvokeAsync`; a synchronous invoke from a data thread can deadlock against the UI thread |
| NT09 | high | Invisible Unicode (category `Cf`: zero-width, bidi controls, tag characters; a leading BOM is fine) or an encoded blob (mixed-case base64 run of 120+ characters, or 48+ `0x..,` bytes) | Decode it and read it before install |

"Data event" = `OnBarUpdate`, `OnMarketData`, `OnMarketDepth`, `OnExecutionUpdate`,
`OnOrderUpdate`, `OnPositionUpdate`, `OnAccountItemUpdate`, `OnFundamentalData`, or any method
that takes a `MarketData/MarketDepth/Execution/Order/Position/AccountItem/BarsUpdate/Fundamental…EventArgs`.

The kind of file (indicator / strategy / AddOn) comes from the class base (`: Indicator`,
`: Strategy`, `: AddOnBase`), else from the folder name (`Indicators`, `Strategies`, `AddOns`).

## How it avoids false hits

Before any rule runs, comments, string contents, char literals and preprocessor lines are blanked
out (same length and line breaks, string quotes kept). A call in a comment or inside a log message
is never a hit. Only NT09 reads the raw text.

The call graph is by method name and three levels deep; overloads merge. A guard anywhere in a
method on the path counts. These are deliberate limits: the linter points at code to read, it does
not prove a bug. There is no inline suppression: fix the code, or accept the finding.

## Adding a rule

Add a `check_*(src)` generator that yields `(offset, detail)` and one row to `RULES` in
`lint.py`, plus a positive and a negative fixture in `server/tests/test_lint.py`.
