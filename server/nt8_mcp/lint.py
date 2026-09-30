"""NinjaScript hazard linter: a static scan of .cs files for patterns that hurt a live NT8.

Every rule covers a known NinjaScript hazard or a call a third-party safety scan flags. The rules live in
one table (RULES, at the bottom): id, name, severity, one-line fix hint, check function.

The scan works on a copy of the source with comments, string contents and preprocessor lines
blanked out (same length, same newlines), so a pattern in a comment or a log message is never
a hit. Only rule NT09 (hidden text) reads the raw source.

No NT8, no AddOn, no network: pure text. Python stdlib only.
"""

import glob
import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Callable

# ---------------------------------------------------------------- source preparation


def strip_source(src: str) -> str:
    """Blank comments, string/char contents and preprocessor lines. Length and newlines are kept,
    string delimiters are kept (so `"` still marks a literal), so offsets map 1:1 to the source."""
    out = list(src)
    n = len(src)

    def blank(a: int, b: int) -> None:
        for k in range(a, min(b, n)):
            if out[k] != "\n":
                out[k] = " "

    def string_end(i: int, verbatim: bool, interp: bool) -> int:
        """i = index of the opening quote. Returns the index just past the closing quote."""
        j = i + 1
        depth = 0
        while j < n:
            c = src[j]
            if depth:  # inside an interpolation hole: code, may hold nested strings
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                elif c == '"':
                    v = src[j - 1] == "@"
                    j = string_end(j, v, src[j - 1] == "$" or src[j - 2:j] in ("$@", "@$"))
                    continue
                j += 1
                continue
            if interp and c == "{":
                if src.startswith("{{", j):
                    j += 2
                    continue
                depth = 1
            elif verbatim:
                if c == '"':
                    if src.startswith('""', j):
                        j += 2
                        continue
                    return j + 1
            else:
                if c == "\\":
                    j += 2
                    continue
                if c == '"' or c == "\n":
                    return j + 1
            j += 1
        return n

    i = 0
    while i < n:
        c = src[i]
        if c == "/" and src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            blank(i, j)
            i = j
        elif c == "/" and src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            blank(i, j)
            i = j
        elif c == '"':
            pre = src[max(0, i - 2):i]
            verbatim = pre.endswith("@") or pre in ("@$", "$@")
            interp = pre.endswith("$") or pre in ("@$", "$@")
            j = string_end(i, verbatim, interp)
            blank(i + 1, j - 1)
            i = j
        elif c == "'":
            j = i + 1
            if j < n and src[j] == "\\":
                j += 1
            j = src.find("'", j + 1)
            j = n if j < 0 else j + 1
            blank(i + 1, j - 1)
            i = j
        else:
            i += 1
    code = "".join(out)
    return re.sub(r"(?m)^[ \t]*#.*$", lambda m: " " * len(m.group(0)), code)


_KEYWORDS = {"if", "for", "foreach", "while", "switch", "catch", "using", "lock", "fixed",
             "return", "new", "else", "when", "get", "set", "add", "remove", "nameof", "typeof"}
_METHOD_RE = re.compile(r"(?:\[[^\]]*\]\s*)*[\w.<>\[\],?\s]+?\s(\w+)\s*\((.*)\)\s*(?::\s*(?:base|this)\s*\(.*\))?\s*$", re.S)


@dataclass
class Method:
    name: str
    params: str
    start: int  # the body's `{`
    end: int  # the body's `}`


@dataclass
class Source:
    path: str
    raw: str
    code: str
    kind: str  # indicator | strategy | addon | other
    methods: list
    blocks: list  # (start, end, header) of every {...}

    def line(self, pos: int) -> int:
        return self.code.count("\n", 0, pos) + 1

    def body(self, m: Method) -> str:
        return self.code[m.start:m.end + 1]

    def method_at(self, pos: int):
        best = None
        for m in self.methods:
            if m.start < pos < m.end and (best is None or m.start > best.start):
                best = m
        return best

    def callers(self, m: Method) -> list:
        call = re.compile(r"(?<![\w.])(?:this\s*\.\s*)?" + re.escape(m.name) + r"\s*\(")
        return [c for c in self.methods if c is not m and call.search(self.body(c))]


def _blocks(code: str):
    stack, blocks, last = [], [], -1
    for i, c in enumerate(code):
        if c == "{":
            stack.append((i, code[last + 1:i]))
            last = i
        elif c == "}":
            if stack:
                s, h = stack.pop()
                blocks.append((s, i, h))
            last = i
        elif c == ";":
            last = i
    return blocks


def _methods(blocks) -> list:
    out = []
    for s, e, h in blocks:
        h = h.strip()
        p = h.find("(")
        if p < 0 or "=" in h[:p] or h.startswith(("return", "else", "case", "=>")):
            continue
        m = _METHOD_RE.match(h)
        if m and m.group(1) not in _KEYWORDS:
            out.append(Method(m.group(1), m.group(2), s, e))
    return out


def _kind(path: str, code: str) -> str:
    base = re.search(r"\bclass\s+\w+\s*:\s*(?:[\w.]*\.)?(Strategy|Indicator|AddOnBase)\b", code)
    if base:
        return {"Strategy": "strategy", "Indicator": "indicator", "AddOnBase": "addon"}[base.group(1)]
    parts = {p.lower() for p in re.split(r"[\\/]", path)}
    for folder, kind in (("strategies", "strategy"), ("indicators", "indicator"), ("addons", "addon")):
        if folder in parts:
            return kind
    return "other"


def parse(path: str, raw: str) -> Source:
    code = strip_source(raw)
    blocks = _blocks(code)
    return Source(path, raw, code, _kind(path, code), _methods(blocks), blocks)


def _paren_span(code: str, open_idx: int) -> int:
    """open_idx = index of `(`; returns the index of its matching `)` (or len)."""
    depth = 0
    for j in range(open_idx, len(code)):
        if code[j] == "(":
            depth += 1
        elif code[j] == ")":
            depth -= 1
            if depth == 0:
                return j
    return len(code)


def _split_args(text: str) -> list[str]:
    args, depth, cur = [], 0, []
    for c in text:
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        if c == "," and depth == 0:
            args.append("".join(cur).strip())
            cur = []
        else:
            cur.append(c)
    args.append("".join(cur).strip())
    return [a for a in args if a]


# ---------------------------------------------------------------- data-event reach

_DATA_EVENTS = {"OnBarUpdate", "OnMarketData", "OnMarketDepth", "OnExecutionUpdate", "OnOrderUpdate",
                "OnPositionUpdate", "OnAccountItemUpdate", "OnFundamentalData"}
_DATA_ARGS = re.compile(r"\b(?:MarketData|MarketDepth|Execution|Order|Position|AccountItem|BarsUpdate|Fundamental)\w*EventArgs\b")


def _is_data_event(m: Method) -> bool:
    return m.name in _DATA_EVENTS or bool(_DATA_ARGS.search(m.params))


def _unguarded_reach(src: Source, m, guard, depth: int = 3, seen=None, is_event=None):
    """The data event that reaches method m through callers with no `guard` match on the way, or None.
    guard=None: any reach counts. ponytail: name-based call graph, 3 levels, overloads merge."""
    if m is None or depth < 0:
        return None
    seen = seen or set()
    if id(m) in seen:
        return None
    seen.add(id(m))
    if guard is not None and guard.search(src.body(m)):
        return None
    if (is_event or _is_data_event)(m):
        return m
    for c in src.callers(m):
        hit = _unguarded_reach(src, c, guard, depth - 1, seen, is_event)
        if hit:
            return hit
    return None


# ---------------------------------------------------------------- the checks
# Each check(src) yields (pos, detail). pos is an offset into src.code.

_SAME_INSTRUMENT = re.compile(r"^(?:(?:NinjaTrader\.)?Data\.)?(?:BarsPeriodType\b|BarsPeriod\b)|^new\s+(?:[\w.]*\.)?BarsPeriod\b"
                              r"|^(?:this\.)?(?:Instrument|BarsArray\s*\[\s*0\s*\]\.Instrument|Bars\.Instrument)\.FullName$|^null$")


def check_cross_instrument(src: Source):
    if src.kind != "indicator":
        return
    strings = set(re.findall(r"\bstring\s+(\w+)", src.code))
    for m in re.finditer(r"(?<![\w.])Add(DataSeries|Volumetric)\s*\(", src.code):
        close = _paren_span(src.code, m.end() - 1)
        args = _split_args(src.code[m.end():close])
        if not args or _SAME_INSTRUMENT.search(args[0]):
            continue
        a = args[0]
        cross = (m.group(1) == "Volumetric" or a.startswith(('"', '$"', '@"')) or a in strings
                 or (len(args) > 1 and _SAME_INSTRUMENT.search(args[1])))
        if cross:
            yield m.start(), f"Add{m.group(1)}({a}, ...)"


_DEPTH_SUB = re.compile(r"MarketDepth\s*\.\s*Update\s*\+=|\bnew\s+MarketDepth\s*<")
_POOL = re.compile(r"\bTask\s*\.\s*Run\s*\(|\bTask\s*\.\s*Factory\s*\.\s*StartNew\s*\(|\bThreadPool\s*\.\s*(?:Unsafe)?QueueUserWorkItem\s*\(")


def check_depth_on_pool(src: Source):
    if not _DEPTH_SUB.search(src.code) or re.search(r"\bDispatcher\s*\.\s*Run\s*\(", src.code):
        return
    by_name = {}
    for m in src.methods:
        by_name.setdefault(m.name, []).append(m)
    for p in _POOL.finditer(src.code):
        span = src.code[p.end() - 1:_paren_span(src.code, p.end() - 1) + 1]
        texts = [span]
        for name in set(re.findall(r"(\w+)\s*\(", span)):
            texts += [src.body(m) for m in by_name.get(name, [])]
        if any(_DEPTH_SUB.search(t) for t in texts):
            yield p.start(), p.group(0).rstrip("( ")


def check_thread_suspend(src: Source):
    names = set(re.findall(r"\bThread\s+(\w+)\s*[;=,)]", src.code))
    names |= set(re.findall(r"(\w+)\s*=\s*new\s+(?:System\.Threading\.)?Thread\s*\(", src.code))
    names |= {"Thread", "CurrentThread"}
    for m in re.finditer(r"\b(\w+)\s*\.\s*(Suspend|Resume|Abort|ResetAbort)\s*\(", src.code):
        if m.group(1) in names:
            yield m.start(), m.group(0).rstrip("( ")


_FILE_WRITE = re.compile(r"\bFile\s*\.\s*(?:Write\w*|Append\w*|Create\w*|OpenWrite|Copy|Move|Replace)\s*\("
                         r"|\bnew\s+(?:System\.IO\.)?(?:StreamWriter|FileStream)\s*\(")
_RT_GUARD = re.compile(r"State\s*\.\s*(?:Realtime|Historical)\b|\bIsHistorical\b|\b\w*(?:Realtime|RealTime|realtime|Historical|historical)\w*\b")


def _is_backfill_event(m: Method) -> bool:
    """Data events that also run on historical data. Market depth never does (realtime/Playback only)."""
    return _is_data_event(m) and m.name != "OnMarketDepth" and "MarketDepthEventArgs" not in m.params


def check_unguarded_write(src: Source):
    if src.kind not in ("indicator", "strategy"):
        return
    for m in _FILE_WRITE.finditer(src.code):
        if m.group(0).startswith("new") and "FileStream" in m.group(0):
            args = src.code[m.end():_paren_span(src.code, m.end() - 1)]
            if re.search(r"FileAccess\s*\.\s*Read\b(?!Write)", args):
                continue
        ev = _unguarded_reach(src, src.method_at(m.start()), _RT_GUARD, is_event=_is_backfill_event)
        if ev:
            yield m.start(), f"{m.group(0).rstrip('( ')} reached from {ev.name}"


_NETWORK = re.compile(r"\bnew\s+(?:System\.Net\.(?:Http\.|Sockets\.)?)?(?:WebClient|HttpClient|TcpClient|UdpClient|TcpListener|Socket|HttpListener|SmtpClient)\s*\("
                      r"|\b(?:Http)?WebRequest\s*\.\s*Create(?:Http)?\s*\(|\bDns\s*\.\s*GetHost\w*\s*\(")


def check_network(src: Source):
    for m in _NETWORK.finditer(src.code):
        yield m.start(), m.group(0).rstrip("( ")


_ENTRY = re.compile(r"(?<![\w.])(?:Enter(?:Long|Short)\w*|SubmitOrderUnmanaged)\s*\(")


_CLEANUP = re.compile(r"(?<![\w.])CancelOrder\s*\(|\bAccount\s*\.\s*Cancel(?:AllOrders)?\s*\(")
# the state the strategy leaves on a disable: Terminated, or a check that it is no longer Realtime
_LEAVING = re.compile(r"State\s*\.\s*Terminated\b|State\s*!=\s*State\s*\.\s*Realtime\b")


def _cancels_on_disable(src: Source) -> bool:
    """True if a block (or a braceless if) keyed on leaving Realtime cancels orders, directly or one call deep."""
    by_name = {}
    for m in src.methods:
        by_name.setdefault(m.name, []).append(m)

    def cleans(text: str) -> bool:
        if _CLEANUP.search(text):
            return True
        return any(_CLEANUP.search(src.body(m)) for n in set(re.findall(r"(\w+)\s*\(", text)) for m in by_name.get(n, []))

    for s, e, h in src.blocks:
        if _LEAVING.search(h) and cleans(src.code[s:e + 1]):
            return True
    for m in re.finditer(r"\bif\s*\([^;{}]*;", src.code):  # braceless: if (State == State.Terminated) CancelAll();
        if _LEAVING.search(m.group(0)) and cleans(m.group(0)):
            return True
    return False


def check_cancel_on_disable(src: Source):
    if src.kind != "strategy":
        return
    entry = _ENTRY.search(src.code)
    if entry and not _cancels_on_disable(src):
        yield entry.start(), f"{entry.group(0).rstrip('( ')} with no order cancel when the strategy leaves Realtime"


_PRINT_GUARD = re.compile(r"State|[Rr]ealtime|[Hh]istorical|[Oo]nce|[Pp]rinted|[Ww]arned|[Ll]ogged|[Dd]ebug|[Vv]erbose|[Tt]race"
                          r"|\b[Ll]og\w*|\b[Pp]rint\w*|IsLastBarOfChart|\bCount\s*-|\bcatch\b")


def check_print_spam(src: Source):
    for m in re.finditer(r"(?<![\w.])Print\s*\(", src.code):
        meth = src.method_at(m.start())
        if meth is None or meth.name not in ("OnBarUpdate", "OnMarketData", "OnMarketDepth"):
            continue
        body_before = src.code[meth.start:m.start()]
        # an early `if (State ...) return;` at the top of the method guards everything after it
        if re.search(r"\bif\s*\([^;{}]*(?:State|[Oo]nce|[Pp]rinted|[Dd]ebug)[^;{}]*\)\s*return\b", body_before):
            continue
        headers = [h for s, e, h in src.blocks if meth.start < s < m.start() < e]
        last = max(src.code.rfind(";", 0, m.start()), src.code.rfind("{", 0, m.start()), src.code.rfind("}", 0, m.start()))
        headers.append(src.code[last + 1:m.start()])  # a braceless `if (x) Print(...)`
        if not any(_PRINT_GUARD.search(h) for h in headers):
            yield m.start(), f"Print in {meth.name}"


_SAFETY = re.compile(r"\bProcess\s*\.\s*Start\s*\(|\bnew\s+(?:System\.Diagnostics\.)?ProcessStartInfo\b"
                     r"|\b(?:Microsoft\.Win32\.)?Registry(?:Key)?\s*\.\s*\w+|\b(?:File|Directory)\s*\.\s*Delete\s*\("
                     r"|\bEnvironment\s*\.\s*(?:Exit|FailFast)\s*\(")


def check_safety(src: Source):
    for m in _SAFETY.finditer(src.code):
        yield m.start(), m.group(0).rstrip("( ")


def check_sync_invoke(src: Source):
    for m in re.finditer(r"\bDispatcher\s*\.\s*Invoke\s*\(", src.code):
        ev = _unguarded_reach(src, src.method_at(m.start()), None)
        if ev:
            yield m.start(), f"Dispatcher.Invoke reached from {ev.name}"


_BLOB = re.compile(r"[A-Za-z0-9+/]{120,}={0,2}|(?:0x[0-9A-Fa-f]{2}\s*,\s*){48,}")


def check_hidden_text(src: Source):
    raw = src.raw
    seen_lines = set()
    for i, ch in enumerate(raw):
        if ord(ch) > 127 and unicodedata.category(ch) == "Cf" and not (i == 0 and ch == "﻿"):
            ln = raw.count("\n", 0, i)
            if ln not in seen_lines:
                seen_lines.add(ln)
                yield i, f"invisible U+{ord(ch):04X} {unicodedata.name(ch, '?')}"
    for m in _BLOB.finditer(raw):
        t = m.group(0)
        if t.startswith("0x") or (re.search(r"[A-Z]", t) and re.search(r"[a-z]", t) and re.search(r"\d", t)):
            yield m.start(), f"encoded blob, {len(t)} chars"


# ---------------------------------------------------------------- the rule table


@dataclass(frozen=True)
class Rule:
    id: str
    name: str
    severity: str  # high | medium | low
    hint: str
    check: Callable


RULES = [
    Rule("NT01", "cross-instrument-series", "high",
         "Indicator adds a series for another instrument (froze every chart); load it with BarsRequest instead.",
         check_cross_instrument),
    Rule("NT02", "depth-on-pool-thread", "high",
         "Market depth subscribed from a thread-pool thread with no Dispatcher (Replay hang); use a dedicated thread that runs Dispatcher.Run().",
         check_depth_on_pool),
    Rule("NT03", "thread-suspend-abort", "high",
         "Thread.Suspend/Resume/Abort can stop a thread inside an NT8 lock; signal the thread to exit (CancellationToken/flag) instead.",
         check_thread_suspend),
    Rule("NT04", "unguarded-file-write", "medium",
         "File write reachable from a data event with no State.Realtime guard (writes on every historical bar); guard it with State == State.Realtime.",
         check_unguarded_write),
    Rule("NT04N", "network-call", "medium",
         "Network call: review where it sends data; never call it from a data event without a timeout.",
         check_network),
    Rule("NT05", "no-cancel-on-disable", "high",
         "Strategy places entries but never cancels its own working orders on disable (NT8's default leaves them working); CancelOrder them in State.Terminated, or have the bridge set the global option Globals.StrategiesOptions.CancelEntriesOnStrategyDisable on start.",
         check_cancel_on_disable),
    Rule("NT06", "print-spam", "low",
         "Print in OnBarUpdate/OnMarketData with no State or once guard floods the Output window; guard it (State == State.Realtime, a once flag, or a Debug input).",
         check_print_spam),
    Rule("NT07", "safety-scan", "high",
         "Process/Registry/Delete/Exit call: third-party code needs a review before it is installed; own code must justify it.",
         check_safety),
    Rule("NT08", "sync-dispatcher-invoke", "high",
         "Synchronous Dispatcher.Invoke from a data event can deadlock against the UI thread; use Dispatcher.InvokeAsync.",
         check_sync_invoke),
    Rule("NT09", "hidden-text", "high",
         "Invisible Unicode or a long encoded blob can hide code or instructions; decode it and read it before install.",
         check_hidden_text),
]
RULES_BY_ID = {r.id: r for r in RULES}


def lint_source(path: str, raw: str, rules=None) -> list[dict]:
    src = parse(path, raw)
    out = []
    for rule in rules or RULES:
        for pos, detail in rule.check(src):
            line = src.raw.count("\n", 0, pos) + 1
            text = src.raw.splitlines()[line - 1].strip() if src.raw else ""
            out.append({"file": path, "line": line, "rule": rule.id, "name": rule.name, "severity": rule.severity,
                        "detail": detail, "hint": rule.hint, "code": text[:160]})
    out.sort(key=lambda f: (f["line"], f["rule"]))
    return out


def expand(paths) -> list[str]:
    """Files, directories (every *.cs below) and globs -> a sorted, unique list of .cs files."""
    if isinstance(paths, str):
        paths = [paths]
    found = set()
    for p in paths:
        if os.path.isdir(p):
            found.update(glob.glob(os.path.join(glob.escape(p), "**", "*.cs"), recursive=True))
        elif os.path.isfile(p):
            found.add(p)
        else:
            found.update(f for f in glob.glob(p, recursive=True) if os.path.isfile(f))
    return sorted(os.path.normpath(f) for f in found)


def lint_paths(paths, rules: list[str] | None = None) -> dict:
    selected = [RULES_BY_ID[r] for r in rules] if rules else RULES
    files = expand(paths)
    findings, errors = [], []
    for f in files:
        try:
            with open(f, encoding="utf-8-sig", errors="replace") as fh:
                raw = fh.read()
        except OSError as e:
            errors.append({"file": f, "error": str(e)})
            continue
        findings += lint_source(f, raw, selected)
    counts = {r.id: 0 for r in selected}
    for x in findings:
        counts[x["rule"]] += 1
    return {"files": len(files), "findings": findings, "counts": counts, "errors": errors}


def main(argv=None) -> int:
    import argparse
    import json
    import sys

    ap = argparse.ArgumentParser(prog="nt_lint", description="NinjaScript hazard linter (static, read-only).")
    ap.add_argument("paths", nargs="*", help=".cs files, folders or globs")
    ap.add_argument("--rule", action="append", help="only this rule id (repeatable)")
    ap.add_argument("--json", action="store_true", help="print the full result as JSON")
    ap.add_argument("--list", action="store_true", help="list the rules and exit")
    a = ap.parse_args(argv)
    if a.list:
        for r in RULES:
            print(f"{r.id:6} {r.severity:6} {r.name:26} {r.hint}")
        return 0
    if not a.paths:
        ap.error("give at least one path")
    res = lint_paths(a.paths, a.rule)
    if a.json:
        print(json.dumps(res, indent=1))
    else:
        for x in sorted(res["findings"], key=lambda x: (x["file"], x["line"])):
            print(f"{x['file']}:{x['line']}: {x['rule']} {x['severity']}: {x['detail']} -- {x['hint']}")
        for e in res["errors"]:
            print(f"{e['file']}: ERROR {e['error']}")
        print(f"{res['files']} files, {len(res['findings'])} findings: "
              + ", ".join(f"{k}={v}" for k, v in res["counts"].items() if v))
    return 1 if res["findings"] or res["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
