"""nt_lint: the NinjaScript hazard linter (nt8_mcp/lint.py) as an MCP tool. Local, read-only, no AddOn."""

from nt8_mcp import lint
from nt8_mcp.app import mcp

MAX_FINDINGS = 500


@mcp.tool(name="nt_lint")
def nt_lint(paths: list[str] | str, rules: list[str] | None = None) -> dict:
    """Static hazard scan of NinjaScript .cs files (files, folders or globs; folders are read recursively).
    Rules NT01-NT09 cover known NT8 hazards (cross-instrument series, depth on a pool thread,
    Thread.Abort, unguarded file writes, network calls, no order cancel on disable, Print spam,
    Process/Registry/Delete/Exit, sync Dispatcher.Invoke, hidden text). rules=["NT05", ...] limits the scan.
    Returns {files, counts, findings: [{file, line, rule, severity, detail, hint, code}], errors, truncated}."""
    unknown = [r for r in rules or [] if r not in lint.RULES_BY_ID]
    if unknown:
        return {"error": f"unknown rule id(s): {unknown}; known: {sorted(lint.RULES_BY_ID)}"}
    res = lint.lint_paths(paths, rules)
    res["truncated"] = len(res["findings"]) > MAX_FINDINGS
    res["findings"] = res["findings"][:MAX_FINDINGS]
    return res
