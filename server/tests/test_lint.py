"""nt_lint: one positive and one negative fixture per rule, plus the comment/string stripper."""

import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [_HERE, os.path.join(_HERE, "..")]

from nt8_mcp import lint  # noqa: E402

IND = "Indicators/SampleInd.cs"
STRAT = "Strategies/SampleStrat.cs"
ADDON = "AddOns/SampleAddOn.cs"


def _ind(body: str, extra: str = "") -> str:
    return ("namespace NinjaTrader.NinjaScript.Indicators {\n public class SampleInd : Indicator {\n"
            + extra + body + "\n }\n}\n")


def _strat(body: str) -> str:
    return "namespace NinjaTrader.NinjaScript.Strategies {\n public class SampleStrat : Strategy {\n" + body + "\n }\n}\n"


def _hits(rule: str, path: str, src: str) -> list[dict]:
    return lint.lint_source(path, src, [lint.RULES_BY_ID[rule]])


def _pos(rule, path, src, line=None):
    hits = _hits(rule, path, src)
    assert hits, f"{rule}: expected a hit"
    if line is not None:
        assert hits[0]["line"] == line, hits
    return hits


def _neg(rule, path, src):
    hits = _hits(rule, path, src)
    assert not hits, f"{rule}: expected no hit, got {hits}"


def test_rule_table_is_complete():
    ids = [r.id for r in lint.RULES]
    assert ids == ["NT01", "NT02", "NT03", "NT04", "NT04N", "NT05", "NT06", "NT07", "NT08", "NT09"], ids
    for r in lint.RULES:
        assert r.severity in ("high", "medium", "low") and r.hint and "\n" not in r.hint


def test_strip_keeps_offsets_and_blanks_comments_strings():
    src = 'a = "File.Delete(x)"; // Process.Start(y)\n/* Thread.Abort() */ c = \'"\'; d = @"x""Registry.y"; e = $"{(k ? "Environment.Exit(1)" : "z")}";\n#region Process.Start\n'
    code = lint.strip_source(src)
    assert len(code) == len(src) and code.count("\n") == src.count("\n")
    for word in ("File", "Process", "Thread", "Registry", "Environment"):
        assert word not in code, (word, code)
    assert "a =" in code and "c =" in code and "d =" in code and "e =" in code


def test_nt01_cross_instrument_series():
    body = ' protected override void OnStateChange() {\n  if (State == State.Configure)\n   AddDataSeries("RTY 12-26", BarsPeriodType.Minute, 1);\n }\n'
    _pos("NT01", IND, _ind(body), line=5)
    prop = ' public string OtherSymbol { get; set; }\n'
    _pos("NT01", IND, _ind(' void Cfg() { AddDataSeries(OtherSymbol); }\n', prop))
    _neg("NT01", IND, _ind(' void Cfg() { AddDataSeries(BarsPeriodType.Minute, 5); AddDataSeries(Instrument.FullName, BarsPeriodType.Tick, 1);\n'
                           ' AddVolumetric(null, BarsPeriodType.Minute, 1, VolumetricDeltaType.BidAsk, 1); }\n'
                           ' // AddDataSeries("RTY 12-26", BarsPeriodType.Minute, 1);\n'))
    _neg("NT01", STRAT, _strat(' void Cfg() { AddDataSeries("RTY 12-26", BarsPeriodType.Minute, 1); }\n'))  # strategies may


def test_nt02_depth_on_pool_thread():
    body = (' void Start() { Task.Run(() => Subscribe()); }\n'
            ' void Subscribe() { inst.MarketDepth.Update += OnDepth; }\n')
    _pos("NT02", ADDON, _ind(body), line=3)
    _pos("NT02", ADDON, _ind(' void S() { ThreadPool.QueueUserWorkItem(_ => { inst.MarketDepth.Update += OnDepth; }); }\n'))
    ok = (' void Start() { var t = new Thread(() => { inst.MarketDepth.Update += OnDepth; Dispatcher.Run(); }); t.Start();\n'
          '  Task.Run(() => Subscribe()); }\n void Subscribe() { inst.MarketDepth.Update += OnDepth; }\n')
    _neg("NT02", ADDON, _ind(ok))
    _neg("NT02", ADDON, _ind(' void S() { Task.Run(() => Work()); inst.MarketDepth.Update += OnDepth; }\n void Work() { }\n'))


def test_nt03_thread_suspend_abort():
    _pos("NT03", ADDON, _ind(' private Thread worker;\n void Stop() { worker.Abort(); }\n'), line=4)
    _pos("NT03", ADDON, _ind(' void Stop() { Thread.CurrentThread.Suspend(); }\n'))
    _neg("NT03", ADDON, _ind(' void Stop() { playback.Resume(); cts.Cancel(); /* worker.Abort(); */ }\n'))


def test_nt04_unguarded_file_write():
    bad = (' protected override void OnBarUpdate() { Save(); }\n'
           ' void Save() { File.AppendAllText(path, Close[0].ToString()); }\n')
    _pos("NT04", IND, _ind(bad), line=4)
    _pos("NT04", STRAT, _strat(' protected override void OnBarUpdate() { using (var w = new StreamWriter(p, true)) w.WriteLine(1); }\n'))
    good = (' protected override void OnBarUpdate() { if (State != State.Realtime) return; Save(); }\n'
            ' void Save() { File.AppendAllText(path, "x"); }\n'
            ' protected override void OnStateChange() { if (State == State.Terminated) File.WriteAllText(p, "done"); }\n'
            ' protected override void OnMarketData(MarketDataEventArgs e) { var r = new FileStream(p, FileMode.Open, FileAccess.Read); }\n'
            ' protected override void OnMarketDepth(MarketDepthEventArgs e) { Snap(); }\n void Snap() { File.WriteAllText(p, "d"); }\n')
    _neg("NT04", IND, _ind(good))
    _neg("NT04", ADDON, "public class SampleAddOn : AddOnBase { void OnExecutionUpdate(object s, ExecutionEventArgs e) { File.AppendAllText(p, \"x\"); } }")


def test_nt04n_network_call():
    _pos("NT04N", IND, _ind(' void Send() { using (var c = new HttpClient()) { } var w = WebRequest.Create(url); }\n'))
    _neg("NT04N", IND, _ind(' void Send() { var s = "new WebClient()"; } // new HttpClient()\n'))


def test_nt05_no_cancel_on_disable():
    entry = ' protected override void OnBarUpdate() { if (Close[0] > Open[0]) entryOrder = EnterLong(); }\n'
    _pos("NT05", STRAT, _strat(entry), line=3)
    wrong_state = ' protected override void OnStateChange() { if (State == State.DataLoaded) { CancelOrder(entryOrder); } }\n'
    _pos("NT05", STRAT, _strat(wrong_state + entry))
    block = ' protected override void OnStateChange() { if (State == State.Terminated) { if (entryOrder != null) CancelOrder(entryOrder); } }\n'
    _neg("NT05", STRAT, _strat(block + entry))
    helper = (' protected override void OnStateChange() { if (State == State.Terminated) CancelWorking(); }\n'
              ' void CancelWorking() { foreach (var o in Orders) CancelOrder(o); }\n')
    _neg("NT05", STRAT, _strat(helper + entry))
    leaving = ' protected override void OnBarUpdate() { if (State != State.Realtime) { Account.Cancel(Orders); return; } EnterShort(); }\n'
    _neg("NT05", STRAT, _strat(leaving))
    _neg("NT05", STRAT, _strat(' protected override void OnBarUpdate() { ExitLong(); }\n'))  # no entries
    _neg("NT05", IND, _ind(entry))  # only strategies


def test_nt06_print_spam():
    _pos("NT06", IND, _ind(' protected override void OnBarUpdate() {\n  Print(Close[0]);\n }\n'), line=4)
    _pos("NT06", IND, _ind(' protected override void OnMarketData(MarketDataEventArgs e) { if (e.Price > 0) { Print(e.Price); } }\n'))
    good = (' protected override void OnBarUpdate() {\n  if (State == State.Realtime) Print(1);\n  if (!warned) { warned = true; Print(2); }\n'
            '  if (DebugMode) Print(3);\n  try { } catch (Exception ex) { Print(ex.Message); }\n }\n'
            ' protected override void OnStateChange() { Print("loaded"); }\n')
    _neg("NT06", IND, _ind(good))
    _neg("NT06", IND, _ind(' protected override void OnBarUpdate() {\n  if (State == State.Historical) return;\n  Print(Close[0]);\n }\n'))


def test_nt07_safety_scan():
    src = _ind(' void X() { Process.Start("cmd"); Registry.CurrentUser.OpenSubKey("k"); File.Delete(p); Environment.Exit(0); }\n')
    hits = _pos("NT07", ADDON, src)
    assert len(hits) == 4, hits
    _neg("NT07", ADDON, _ind(' void X() { var s = "Process.Start"; order.Delete(); } // File.Delete(p)\n'))


def test_nt08_sync_dispatcher_invoke():
    bad = (' protected override void OnMarketData(MarketDataEventArgs e) { Paint(); }\n'
           ' void Paint() { ChartControl.Dispatcher.Invoke(() => { }); }\n')
    _pos("NT08", IND, _ind(bad), line=4)
    _pos("NT08", ADDON, "class A : AddOnBase { void OnExec(object s, ExecutionEventArgs e) { Dispatcher.Invoke(() => { }); } }")
    good = (' protected override void OnBarUpdate() { ChartControl.Dispatcher.InvokeAsync(() => { }); }\n'
            ' void OnClick(object s, RoutedEventArgs e) { Dispatcher.Invoke(() => { }); }\n')
    _neg("NT08", IND, _ind(good))


def test_nt09_hidden_text():
    _pos("NT09", IND, _ind(' void X() { int a​ = 1; }\n'), line=3)
    blob = "QmFzZTY0" * 20 + "Zm9vYmFy1"
    _pos("NT09", IND, _ind(f' const string K = "{blob}";\n'))
    _neg("NT09", IND, "﻿" + _ind(' // ' + "/" * 200 + '\n void X() { string s = "' + "a" * 150 + '"; }\n'))


def test_lint_paths_and_mcp_tool():
    from nt8_mcp import server as nt8

    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "Strategies"))
        with open(os.path.join(d, "Strategies", "SampleStrat.cs"), "w", encoding="utf-8") as fh:
            fh.write(_strat(' protected override void OnBarUpdate() { EnterShort(); }\n'))
        with open(os.path.join(d, "notes.txt"), "w") as fh:
            fh.write("EnterLong();")
        res = nt8.nt_lint(paths=[d])
        assert res["files"] == 1 and res["counts"]["NT05"] == 1 and res["truncated"] is False, res
        res = nt8.nt_lint(paths=os.path.join(d, "**", "*.cs"), rules=["NT07"])
        assert res["files"] == 1 and res["findings"] == [], res
        assert "error" in nt8.nt_lint(paths=[d], rules=["NT99"])
        assert lint.main([d]) == 1 and lint.main([d, "--rule", "NT07"]) == 0
