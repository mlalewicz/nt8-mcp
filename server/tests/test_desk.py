"""The Sim desk tools (nt8_mcp.tools_desk) against a canned AddOn that enforces the documented
/desk/* contract (addon/NT8BridgeDesk.cs): Simulator accounts only, dry run then a one-shot confirm,
enabled rows and duplicates refused, and — on the Python side — the local NT8 lock and the
allowlist file. The C# gates are verified against a running NinjaTrader; what is pinned here is the
contract and the client-side lock/allowlist refusals, which must hold before anything is sent.

Placeholder names only: "Sim101", "LiveAcct", "StratA".
"""

import json
import os
import sys
import tempfile
import time
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [_HERE, os.path.join(_HERE, "..")]

from fake_addon import FakeAddon  # noqa: E402
from nt8_mcp import app, server as nt8, tools_desk  # noqa: E402

SIM, LIVE, ME = "Sim101", "LiveAcct", "agent-a"


class Desk:
    """The /desk/* contract as a scriptable fake. rows: {id: {enabled, alive, account, strategy,
    instrument, bridge}}; allow: the allowlist triples the AddOn would find in desk_legs.json."""

    def __init__(self, rows=None, allow=None, orphans=None):
        self.rows = rows or {}
        self.allow = allow if allow is not None else set()
        self.orphans = orphans if orphans is not None else ["o1", "o2"]
        self.used = set()
        self.acted = []

    def handle(self, req):
        body = json.loads(req.body or "{}")
        verb = req.path.rsplit("/", 1)[1]
        if body.get("account") != SIM:
            return 403, {"error": f"account '{body.get('account')}' is not a Provider.Simulator or Provider.Playback account"}
        refusal = self.check(verb, body)
        if refusal:
            return refusal
        plan = f"desk.{verb}|{json.dumps(body.get('strategyId') or body.get('instrument'))}"
        if "confirm" not in body:
            return {"dryRun": True, "plan": {"verb": verb}, "confirm": "sig:" + plan, "issuedAt": 1.0}
        if body["confirm"] != "sig:" + plan:
            return 409, {"error": "confirm does not match the plan as it is NOW"}
        if body["confirm"] in self.used:
            return 409, {"error": "this confirm was already used"}
        self.used.add(body["confirm"])
        self.acted.append((verb, body))
        row = self.rows.get(body.get("strategyId"))
        if verb == "gridEnable" and not row.get("stall"):
            row["enabled"] = row["alive"] = True
        return {"ok": True, "dryRun": False}

    def check(self, verb, body):
        if verb == "cancelOrphans":
            return None if self.orphans else (409, {"error": "no working order whose owning strategy instance is dead"})
        row = self.rows.get(body.get("strategyId"))
        if row is None:
            return 404, {"error": "no Strategies-grid row with strategy id"}
        if verb == "gridRemove":
            if row.get("bridge"):
                return 409, {"error": "started by this AddOn — use POST /strategy/stop"}
            if row["enabled"] or row["alive"]:
                return 409, {"error": "row is enabled or its instance is alive"}
            return None
        if row["enabled"] or row["alive"]:
            return 409, {"error": "row is already enabled or alive"}
        if (row["strategy"], row["account"], row["instrument"]) not in self.allow:
            return 403, {"error": "is not in the allowlist"}
        for rid, other in self.rows.items():
            if rid != body["strategyId"] and other["strategy"] == row["strategy"] \
                    and other["account"] == row["account"] and (other["enabled"] or other["alive"]):
                return 409, {"error": f"duplicate: already running as strategy id(s) {rid}"}
        return None


def _fake(desk):
    fake = FakeAddon()
    for verb in ("cancelOrphans", "gridRemove", "gridEnable"):
        fake.register("/desk/" + verb, "POST", desk.handle)
    fake.register("/strategies/running", "GET", lambda req: {"strategies": [
        _grid_row(rid, r["enabled"], "Realtime" if r["alive"] and not r.get("stall") else
                  ("Historical" if r["alive"] or r["enabled"] else "Terminated"),
                  name=r["strategy"], instrument=r["instrument"], account=r["account"])
        for rid, r in desk.rows.items()]})
    return fake


class _Env:
    """Temp lock + legs files via the env overrides, restored on exit."""

    def __init__(self, lock_holder=ME, lock_ttl=600, legs=True):
        self.dir = tempfile.mkdtemp()
        self.lock = os.path.join(self.dir, "nt8.lock")
        self.legs = os.path.join(self.dir, "desk_legs.json")
        self.decisions = os.path.join(self.dir, "decisions")
        self.saved = {}
        if lock_holder:
            with open(self.lock, "w") as fh:
                json.dump({"holder": lock_holder, "expires": time.time() + lock_ttl}, fh)
        if legs:
            if legs is True:
                legs = [{"strategy": "StratA", "account": SIM, "instrument": "ES 12-26", "qty": 1}]
            with open(self.legs, "w") as fh:
                json.dump({"legs": legs}, fh)

    def __enter__(self):
        for k, v in (("NT8MCP_LOCK_FILE", self.lock), ("NT8MCP_DESK_LEGS", self.legs),
                     ("NT8MCP_DECISIONS_DIR", self.decisions)):
            self.saved[k] = os.environ.get(k)
            os.environ[k] = v
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


def _row(enabled=False, alive=False, strategy="StratA", account=SIM, instrument="ES 12-26", bridge=False):
    return {"enabled": enabled, "alive": alive, "strategy": strategy, "account": account,
            "instrument": instrument, "bridge": bridge}


def _two_step(tool, **kw):
    dry = tool(**kw)
    assert dry.get("dryRun") is True, dry
    return dry, tool(**kw, confirm=dry["confirm"], issued_at=dry["issuedAt"])


# ── the lock ────────────────────────────────────────────────────────────────

def test_writes_refused_without_the_lock_and_nothing_is_sent():
    desk = Desk(rows={"402010159": _row()}, allow={("StratA", SIM, "ES 12-26")})
    for holder, ttl in ((None, 0), ("someone-else", 600), (ME, -5)):
        with _Env(lock_holder=holder, lock_ttl=ttl), _fake(desk) as fake:
            for tool, kw in ((nt8.nt_cancel_orphans, {}), (nt8.nt_grid_remove, {"strategy_id": "402010159"}),
                             (nt8.nt_grid_enable, {"strategy_id": "402010159"})):
                out = tool(caller=ME, account=SIM, **kw)
                assert "lock" in out["error"], out
            assert sum(fake.calls.values()) == 0


def test_empty_caller_is_refused():
    with _Env(), _fake(Desk()) as fake:
        assert "caller is required" in nt8.nt_cancel_orphans(caller="", account=SIM)["error"]
        assert sum(fake.calls.values()) == 0


def test_nt_lock_take_renew_release_and_refuse_other():
    with _Env(lock_holder=None) as env:
        took = nt8.nt_lock(caller=ME, minutes=5)
        assert took["ok"] and took["holder"] == ME and took["path"] == env.lock
        assert nt8.nt_lock(caller=ME, minutes=10)["ok"]                  # renew
        assert "held by" in nt8.nt_lock(caller="other")["error"]
        assert "held by" in nt8.nt_lock(caller="other", release=True)["error"]
        assert nt8.nt_lock(caller=ME, release=True)["released"] is True
        assert not os.path.exists(env.lock)
        assert nt8.nt_lock(caller="other")["ok"]                         # free again
        assert "minutes" in nt8.nt_lock(caller="other", minutes=0)["error"]


# ── nt_cancel_orphans ───────────────────────────────────────────────────────

def test_cancel_orphans_dry_run_then_one_confirm():
    desk = Desk()
    with _Env(), _fake(desk):
        dry, done = _two_step(nt8.nt_cancel_orphans, caller=ME, account=SIM)
        assert desk.acted == [("cancelOrphans", {"account": SIM, "confirm": dry["confirm"], "issuedAt": 1.0})]
        assert done["ok"] is True
        replay = nt8.nt_cancel_orphans(caller=ME, account=SIM, confirm=dry["confirm"], issued_at=1.0)
        assert "already used" in replay["error"] and len(desk.acted) == 1


def test_cancel_orphans_dry_run_sends_no_confirm_key():
    seen = []
    with _Env(), FakeAddon() as fake:
        fake.register("/desk/cancelOrphans", "POST", lambda req: seen.append(json.loads(req.body)) or {"dryRun": True})
        nt8.nt_cancel_orphans(caller=ME, account=SIM, instrument="ES 12-26")
    assert seen == [{"account": SIM, "instrument": "ES 12-26"}]


def test_cancel_orphans_live_account_refused():
    with _Env(), _fake(Desk()):
        assert "not a Provider.Simulator" in nt8.nt_cancel_orphans(caller=ME, account=LIVE)["error"]


def test_cancel_orphans_nothing_to_do():
    with _Env(), _fake(Desk(orphans=[])):
        assert "no working order" in nt8.nt_cancel_orphans(caller=ME, account=SIM)["error"]


# ── nt_grid_remove ──────────────────────────────────────────────────────────

def test_grid_remove_dead_row():
    desk = Desk(rows={"402010159": _row()})
    with _Env(), _fake(desk):
        _dry, done = _two_step(nt8.nt_grid_remove, caller=ME, strategy_id=402010159, account=SIM)
        assert done["ok"] and desk.acted[0][0] == "gridRemove"
        assert desk.acted[0][1]["strategyId"] == "402010159"          # sent as a string, never a float


def test_grid_remove_refusals():
    desk = Desk(rows={"1": _row(enabled=True), "2": _row(alive=True), "3": _row(bridge=True)})
    with _Env(), _fake(desk):
        assert "enabled" in nt8.nt_grid_remove(caller=ME, strategy_id="1", account=SIM)["error"]
        assert "alive" in nt8.nt_grid_remove(caller=ME, strategy_id="2", account=SIM)["error"]
        assert "/strategy/stop" in nt8.nt_grid_remove(caller=ME, strategy_id="3", account=SIM)["error"]
        assert "not a Provider.Simulator" in nt8.nt_grid_remove(caller=ME, strategy_id="1", account=LIVE)["error"]
        assert "no Strategies-grid row" in nt8.nt_grid_remove(caller=ME, strategy_id="9", account=SIM)["error"]
    assert desk.acted == []


# ── nt_grid_enable ──────────────────────────────────────────────────────────

def test_grid_enable_needs_the_allowlist_file():
    with _Env(legs=False), _fake(Desk(rows={"1": _row()})) as fake:
        assert "no allowlist file" in nt8.nt_grid_enable(caller=ME, strategy_id="1", account=SIM)["error"]
        assert sum(fake.calls.values()) == 0


def test_grid_enable_allowlisted_row():
    desk = Desk(rows={"1": _row()}, allow={("StratA", SIM, "ES 12-26")})
    with _Env(), _fake(desk):
        _dry, done = _two_step(nt8.nt_grid_enable, caller=ME, strategy_id="1", account=SIM)
        assert done["ok"] and desk.acted[0][0] == "gridEnable"


def test_grid_enable_refusals():
    rows = {"1": _row(), "2": _row(enabled=True), "3": _row(instrument="NQ 12-26"),
            "4": _row(strategy="StratB"), "5": _row(strategy="StratB", alive=True)}
    allow = {("StratA", SIM, "ES 12-26"), ("StratA", SIM, "NQ 12-26"), ("StratB", SIM, "ES 12-26")}
    desk = Desk(rows=rows, allow=allow)
    with _Env(), _fake(desk):
        assert "already enabled" in nt8.nt_grid_enable(caller=ME, strategy_id="2", account=SIM)["error"]
        assert "duplicate" in nt8.nt_grid_enable(caller=ME, strategy_id="1", account=SIM)["error"]   # row 2 runs StratA
        assert "duplicate" in nt8.nt_grid_enable(caller=ME, strategy_id="4", account=SIM)["error"]   # row 5 alive
        assert "not a Provider.Simulator" in nt8.nt_grid_enable(caller=ME, strategy_id="1", account=LIVE)["error"]
        desk.allow = set()
        rows["2"]["enabled"] = False
        assert "allowlist" in nt8.nt_grid_enable(caller=ME, strategy_id="1", account=SIM)["error"]
    assert desk.acted == []


# ── nt_desk_status ──────────────────────────────────────────────────────────

def _grid_row(sid, enabled, live, name="StratA", instrument="ES 12-26", account=SIM, parent=None):
    return {"name": name, "type": name, "parent": parent, "enabled": enabled, "strategyId": sid,
            "liveState": live, "instanceAlive": live in ("Realtime", "Historical"),
            "account": account, "instrument": instrument}


def test_desk_status_legs_duplicates_orphans():
    tmp = tempfile.mkdtemp()
    trace_dir = os.path.join(tmp, "trace")
    log_dir = os.path.join(tmp, "log")
    os.makedirs(trace_dir); os.makedirs(log_dir)
    with open(os.path.join(trace_dir, "trace.txt"), "w") as fh:
        fh.write("x\n")
    with open(os.path.join(log_dir, "log.txt"), "w") as fh:
        fh.write("2026-09-29 15:48:56:329|2|4|Session Break (Version 8.1.8.3)\n")
    saved = (app.NT_TRACE_DIR, app.NT_LOG_DIR, tools_desk._process_start)
    app.NT_TRACE_DIR, app.NT_LOG_DIR = trace_dir, log_dir
    tools_desk._process_start = lambda: "2026-09-29T15:48:50"
    try:
        with _Env(), FakeAddon() as fake:
            fake.json("/health", {"connections": [{"name": "Feed", "status": "Connected"}]})
            fake.json("/strategies/running", {"gridResolved": True, "strategies": [
                _grid_row("1", True, "Realtime"),
                _grid_row("1", True, "Realtime", parent="StratA"),       # per-instrument child: ignored
                _grid_row("2", True, "Terminated"),                      # enabled, not Realtime, duplicate
                _grid_row("3", False, "Terminated", name="StratB")]})
            fake.json("/desk/orphans", {"accounts": [{"account": SIM, "orphans": [{"orderId": "o9", "ownerStrategyId": "2"}]}]})
            out = nt8.nt_desk_status()
    finally:
        app.NT_TRACE_DIR, app.NT_LOG_DIR, tools_desk._process_start = saved
    assert out["processStart"] == "2026-09-29T15:48:50"
    assert out["lastSessionBreak"] == "2026-09-29T15:48:56"
    assert out["connections"][0]["status"] == "Connected"
    leg = out["legs"][0]
    assert (leg["rows"], leg["enabled"], leg["realtime"], leg["ok"]) == (2, 2, 1, False)
    assert out["duplicates"] == [{"strategy": "StratA", "account": SIM, "instrument": "ES 12-26", "strategyIds": ["1", "2"]}]
    assert [r["strategyId"] for r in out["enabledNotRealtime"]] == ["2"]
    assert out["orphans"] == [{"orderId": "o9", "ownerStrategyId": "2", "account": SIM}]
    assert out["traceAgeSec"] is not None and out["gridResolved"] is True


def test_desk_status_unreadable_says_null_not_empty():
    tmp = tempfile.mkdtemp()
    saved = (app.NT_TRACE_DIR, app.NT_LOG_DIR, tools_desk._process_start)
    app.NT_TRACE_DIR = app.NT_LOG_DIR = tmp
    tools_desk._process_start = lambda: None
    try:
        with _Env(legs=False), FakeAddon():          # every route answers 404
            out = nt8.nt_desk_status()
    finally:
        app.NT_TRACE_DIR, app.NT_LOG_DIR, tools_desk._process_start = saved
    assert out["gridResolved"] is False and out["orphans"] is None and out["legsError"] == "absent"


# ── nt_crash_report ─────────────────────────────────────────────────────────

def test_crash_report_trace_before_the_break_and_events_window():
    tmp = tempfile.mkdtemp()
    trace_dir, log_dir = os.path.join(tmp, "trace"), os.path.join(tmp, "log")
    os.makedirs(trace_dir); os.makedirs(log_dir)
    with open(os.path.join(trace_dir, "trace.a.txt"), "w") as fh:
        fh.write("2026-09-29 15:40:00:000 before 1\n"
                 "   continuation of before 1\n"
                 "2026-09-29 15:41:00:000 UpdateMarketDepth exception\n"
                 "2026-09-29 15:50:00:000 after the restart\n")
    with open(os.path.join(log_dir, "log.a.txt"), "w") as fh:
        fh.write("2026-09-28 09:00:00:000|2|4|Session Break (Version 8.1.8.3)\n"
                 "2026-09-29 15:48:56:329|2|4|Session Break (Version 8.1.8.3)\n")
    seen = []
    saved = (app.NT_TRACE_DIR, app.NT_LOG_DIR, tools_desk._win_events)
    app.NT_TRACE_DIR, app.NT_LOG_DIR = trace_dir, log_dir
    tools_desk._win_events = lambda center: seen.append(center) or [{"log": "System", "id": 41}]
    try:
        out = nt8.nt_crash_report(n=2)
    finally:
        app.NT_TRACE_DIR, app.NT_LOG_DIR, tools_desk._win_events = saved
    assert out["sessionBreak"] == "2026-09-29T15:48:56"
    assert out["traceLines"] == ["   continuation of before 1", "2026-09-29 15:41:00:000 UpdateMarketDepth exception"]
    assert seen == [datetime(2026, 9, 29, 15, 48, 56)]
    assert out["windowsEvents"] == [{"log": "System", "id": 41}]


def test_crash_report_without_a_break():
    tmp = tempfile.mkdtemp()
    saved = app.NT_LOG_DIR
    app.NT_LOG_DIR = tmp
    try:
        assert "no 'Session Break'" in nt8.nt_crash_report()["error"]
    finally:
        app.NT_LOG_DIR = saved


# ── nt_desk_recover ─────────────────────────────────────────────────────────

LEG_A = {"strategy": "StratA", "account": SIM, "instrument": "ES 12-26", "qty": 1}
LEG_B = {"strategy": "StratB", "account": SIM, "instrument": "NQ 12-26", "qty": 1}
ALLOW_AB = {("StratA", SIM, "ES 12-26"), ("StratB", SIM, "NQ 12-26")}


def _enables(desk):
    return [b["strategyId"] for v, b in desk.acted if v == "gridEnable"]


def test_recover_nothing_off():
    desk = Desk(rows={"1": _row(enabled=True, alive=True)}, allow={("StratA", SIM, "ES 12-26")})
    with _Env(), _fake(desk):
        out = nt8.nt_desk_recover(caller=ME)
    assert out["dryRun"] is True and out["planned"] == 0 and "confirm" not in out
    assert [l["status"] for l in out["legs"]] == ["on"] and desk.acted == []


def test_recover_two_legs_off_one_confirm_used_once():
    desk = Desk(rows={"1": _row(), "2": _row(strategy="StratB", instrument="NQ 12-26")}, allow=ALLOW_AB)
    with _Env(legs=[LEG_A, LEG_B]), _fake(desk):
        dry = nt8.nt_desk_recover(caller=ME)
        assert dry["planned"] == 2 and desk.acted == []
        assert [(l["status"], l["strategyId"], l["duplicateGuard"]) for l in dry["legs"]] == \
            [("planned", "1", "passed (AddOn dry run)"), ("planned", "2", "passed (AddOn dry run)")]
        bogus = nt8.nt_desk_recover(caller=ME, confirm="recover:00", issued_at=dry["issuedAt"])
        assert "does not match" in bogus["error"] and desk.acted == []
        done = nt8.nt_desk_recover(caller=ME, confirm=dry["confirm"], issued_at=dry["issuedAt"])
        assert done["ok"] is True and _enables(desk) == ["1", "2"]
        assert [(l["strategyId"], l["result"], l["state"]) for l in done["legs"]] == \
            [("1", "enabled", "Realtime"), ("2", "enabled", "Realtime")]
        again = nt8.nt_desk_recover(caller=ME, confirm=dry["confirm"], issued_at=dry["issuedAt"])
        assert "already used" in again["error"] and len(desk.acted) == 2


def test_recover_only_realtime_counts():
    rows = {"1": _row(), "2": _row(strategy="StratB", instrument="NQ 12-26")}
    rows["2"]["stall"] = True
    desk = Desk(rows=rows, allow=ALLOW_AB)
    with _Env(legs=[LEG_A, LEG_B]), _fake(desk):
        dry = nt8.nt_desk_recover(caller=ME)
        done = nt8.nt_desk_recover(caller=ME, confirm=dry["confirm"], issued_at=dry["issuedAt"])
    assert done["ok"] is False
    assert [l["result"] for l in done["legs"]] == ["enabled", "failed"]
    assert "not Realtime" in done["legs"][1]["reason"]


def test_recover_refuses_zero_and_two_candidates():
    desk = Desk(rows={"1": _row(strategy="StratB", instrument="NQ 12-26"),
                      "2": _row(strategy="StratB", instrument="NQ 12-26")}, allow=ALLOW_AB)
    with _Env(legs=[LEG_A, LEG_B]), _fake(desk) as fake:
        out = nt8.nt_desk_recover(caller=ME)
        assert fake.calls[("POST", "/desk/gridEnable")] == 0        # never sent a guess
    a, b = out["legs"]
    assert a["status"] == "refused" and a["reason"].startswith("0 candidate rows")
    assert b["status"] == "refused" and b["reason"].startswith("2 candidate rows") and "guess" in b["reason"]
    assert out["planned"] == 0 and "confirm" not in out


def test_recover_duplicate_guard():
    leg_a2 = {"strategy": "StratA", "account": SIM, "instrument": "NQ 12-26", "qty": 1}
    desk = Desk(rows={"1": _row(enabled=True, alive=True), "2": _row(instrument="NQ 12-26")},
                allow={("StratA", SIM, "ES 12-26"), ("StratA", SIM, "NQ 12-26")})
    with _Env(legs=[LEG_A, leg_a2]), _fake(desk):
        out = nt8.nt_desk_recover(caller=ME)
    on, dup = out["legs"]
    assert on["status"] == "on"
    assert dup["status"] == "refused" and "duplicate" in dup["duplicateGuard"]
    assert out["planned"] == 0 and desk.acted == []


def test_recover_without_the_lock_sends_nothing():
    desk = Desk(rows={"1": _row()}, allow={("StratA", SIM, "ES 12-26")})
    for holder, ttl in ((None, 0), ("someone-else", 600), (ME, -5)):
        with _Env(lock_holder=holder, lock_ttl=ttl), _fake(desk) as fake:
            assert "lock" in nt8.nt_desk_recover(caller=ME)["error"]
            assert "lock" in nt8.nt_desk_recover(caller=ME, confirm="recover:x", issued_at=time.time())["error"]
            assert sum(fake.calls.values()) == 0


def test_recover_live_account_refused():
    leg = {"strategy": "StratA", "account": LIVE, "instrument": "ES 12-26", "qty": 1}
    desk = Desk(rows={"1": _row(account=LIVE)}, allow={("StratA", LIVE, "ES 12-26")})
    with _Env(legs=[leg]), _fake(desk):
        out = nt8.nt_desk_recover(caller=ME)
        assert out["legs"][0]["status"] == "refused" and "not a Provider.Simulator" in out["legs"][0]["reason"]
        assert out["planned"] == 0 and "confirm" not in out
        assert nt8.nt_desk_recover(caller=ME, account=SIM)["legs"] == []      # account narrows the legs
    assert desk.acted == []


# ── nt_leg_decisions ────────────────────────────────────────────────────────

WED_10 = datetime(2026, 9, 30, 10, 0)      # a Wednesday, 10:00 ET


def _decisions(env, name, text):
    os.makedirs(env.decisions, exist_ok=True)
    with open(os.path.join(env.decisions, name), "w", newline="") as fh:
        fh.write(text)


def _frozen(now, fn):
    saved = tools_desk._now_et
    tools_desk._now_et = lambda: now
    try:
        return fn()
    finally:
        tools_desk._now_et = saved


def test_decisions_header_commas_and_today():
    legs = [dict(LEG_A, decision_time="09:35")]
    with _Env(legs=legs) as env:
        _decisions(env, f"StratA_{SIM}.csv",
                   "date_et,time_et,decision,reason,inputs\r\n"
                   "2026-09-28,09:35:00,SKIP,no label,a=1\r\n"
                   "2026-09-29,09:35:00,NO_LABEL,flat open,a=1,b=2\r\n"
                   "2026-09-30,09:35:01,ENTER_LONG,signal,x=1, y=2,z=3\r\n")
        out = _frozen(WED_10, lambda: nt8.nt_leg_decisions(days=2))
    leg = out["legs"][0]
    assert leg["fileError"] is None and len(leg["lines"]) == 2
    assert leg["lines"][-1] == {"date_et": "2026-09-30", "time_et": "09:35:01", "decision": "ENTER_LONG",
                                "reason": "signal", "inputs": "x=1, y=2,z=3"}
    assert leg["noLineToday"] is False and out["silentLegs"] == []


def test_decisions_missing_file_and_no_line_today_flag():
    legs = [dict(LEG_A, decision_time="09:35"), dict(LEG_B, decision_time="09:35"),
            {"strategy": "StratC", "account": SIM, "instrument": "ES 12-26", "qty": 1}]
    with _Env(legs=legs) as env:
        _decisions(env, f"StratA_{SIM}.csv", "2026-09-29,09:35:00,SKIP,no label,\n")
        out = _frozen(WED_10, lambda: nt8.nt_leg_decisions())
        early = _frozen(datetime(2026, 9, 30, 9, 0), lambda: nt8.nt_leg_decisions())
        saturday = _frozen(datetime(2026, 10, 3, 12, 0), lambda: nt8.nt_leg_decisions())
        only_b = _frozen(WED_10, lambda: nt8.nt_leg_decisions(strategy="stratb"))
    a, b, c = out["legs"]
    assert a["noLineToday"] is True and a["fileError"] is None and a["lines"][0]["inputs"] == ""
    assert b["noLineToday"] is True and b["fileError"] == "missing"
    assert c["noLineToday"] is None and c["fileError"] == "missing"          # no decision_time: no flag
    assert out["silentLegs"] == [f"StratA/{SIM}", f"StratB/{SIM}"]
    assert [l["noLineToday"] for l in early["legs"]] == [False, False, None]
    assert [l["noLineToday"] for l in saturday["legs"]] == [False, False, None]
    assert [l["strategy"] for l in only_b["legs"]] == ["StratB"]


def test_desk_status_silent_legs():
    tmp = tempfile.mkdtemp()
    saved = (app.NT_TRACE_DIR, app.NT_LOG_DIR, tools_desk._process_start, tools_desk._now_et)
    app.NT_TRACE_DIR = app.NT_LOG_DIR = tmp
    tools_desk._process_start = lambda: None
    tools_desk._now_et = lambda: WED_10
    try:
        with _Env(legs=[dict(LEG_A, decision_time="09:35"), LEG_B]), FakeAddon():
            out = nt8.nt_desk_status()
    finally:
        app.NT_TRACE_DIR, app.NT_LOG_DIR, tools_desk._process_start, tools_desk._now_et = saved
    assert out["silentLegs"] == [{"strategy": "StratA", "account": SIM, "decisionTime": "09:35",
                                  "fileError": "missing"}]


def test_tools_registered():
    names = {t.name for t in nt8.mcp._tool_manager.list_tools()} if hasattr(nt8.mcp, "_tool_manager") else None
    for name in ("nt_lock", "nt_cancel_orphans", "nt_grid_remove", "nt_grid_enable", "nt_desk_status",
                 "nt_crash_report", "nt_desk_recover", "nt_leg_decisions"):
        assert hasattr(nt8, name)
        if names is not None:
            assert name in names
