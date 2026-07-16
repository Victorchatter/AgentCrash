"""AgentCrash as an MCP server (stdio transport).

Exposes AgentCrash over the Model Context Protocol so any MCP-aware host
(Claude Desktop, IDEs, coding agents) can search traces, inspect runs,
replay failures, analyze root causes, and mint regression tests from its
tool loop — the "agent debugs its own crashes" surface (see
``docs/research/mcp.md`` §9).

Implementation is deliberately dependency-free: newline-delimited
JSON-RPC 2.0 over stdin/stdout. ``mcp`` is not required. Stdio is the
dominant local transport per the research, and it fits AgentCrash (local
SQLite store, self-describing per-call handles). The same ``ServerContext``
engine backs the web API and CLI, so behavior is identical across surfaces.

Protocol version negotiated: ``2025-06-18`` (stable, broadly supported).
Tool execution failures surface as results with ``isError: true`` (NOT
JSON-RPC errors), per spec §7.2 — this distinction is what lets an agent
reason about retry vs. a real protocol fault.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from agentcrash.server import ServerContext, _interventions_from

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "agentcrash"
SERVER_VERSION = "0.1.0"


class InvalidParams(Exception):
    """Raised for bad/unknown arguments -> JSON-RPC -32602 (invalid params)."""


class ToolError(Exception):
    """Raised when a tool *ran* but failed -> result with isError: true."""


def _get_run(ctx: ServerContext, run_id: str) -> dict[str, Any]:
    run = ctx.storage.get_run(run_id)
    if not run:
        raise InvalidParams(f"run {run_id} not found")
    return run


# ---------- tools ----------

def _tool_trace_search(args: dict[str, Any], ctx: ServerContext) -> str:
    limit = int(args.get("limit", 20))
    status = args.get("status", "all")
    agent = args.get("agent")
    rows = ctx.storage.list_runs(limit=limit)
    out = []
    for r in rows:
        if status == "failed" and r.get("status") != "failed":
            continue
        if agent and r.get("agent") != agent:
            continue
        out.append({
            "run_id": r["id"], "status": r.get("status"), "agent": r.get("agent"),
            "model": r.get("model"), "tool_calls": r.get("tool_calls", 0),
            "retries": r.get("retries", 0), "error": r.get("error"),
        })
    return json.dumps({"count": len(out), "runs": out}, default=str)


def _tool_trace_get(args: dict[str, Any], ctx: ServerContext) -> str:
    run_id = args["run_id"]
    include_content = bool(args.get("include_content", False))
    run = _get_run(ctx, run_id)
    events = ctx.storage.get_events(run_id)
    timeline = []
    for e in events:
        item = {
            "seq": e.seq, "type": e.type, "name": e.name,
            "status": e.status, "duration_ms": e.duration_ms,
        }
        if e.is_error and e.error:
            item["error"] = e.error.message
        if include_content:
            item["input"] = e.input
            item["output"] = e.output
        timeline.append(item)
    return json.dumps({
        "run_id": run["id"], "status": run.get("status"), "agent": run.get("agent"),
        "model": run.get("model"), "error": run.get("error"),
        "root_cause": run.get("root_cause"), "tool_calls": run.get("tool_calls", 0),
        "event_count": len(events), "events": timeline,
    }, default=str)


def _tool_replay_run(args: dict[str, Any], ctx: ServerContext) -> str:
    from agentcrash.replay import ReplayConfig

    run_id = args["run_id"]
    _get_run(ctx, run_id)
    mode = args.get("mode", "exact")
    if mode not in ("exact", "selective", "live"):
        raise InvalidParams(f"mode must be exact|selective|live, got {mode!r}")
    try:
        fn, original_input = ctx.resolve(run_id, args.get("agent"))
    except KeyError as e:
        # Run exists but has no in-process agent -> tool execution error, not a
        # protocol error. The agent can reason about this (register the agent).
        raise ToolError(str(e)) from None
    cfg = ReplayConfig(
        mode=mode,
        interventions=_interventions_from(args.get("interventions") or []),
        consent_live=bool(args.get("consent_live", False)),
    )
    try:
        result = ctx.replayer.replay(run_id, fn, original_input, cfg)
    except PermissionError as e:
        raise ToolError(f"live replay refused: {e}") from None
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"replay failed: {e}") from None
    return json.dumps({
        "replay_id": result.replay_id, "new_run_id": result.new_run_id,
        "status": result.status, "error": result.error,
        "behaviorally_identical": not result.diff.is_different if result.diff else None,
        "diff_lines": result.diff_lines,
    }, default=str)


def _tool_analyze_failure(args: dict[str, Any], ctx: ServerContext) -> str:
    run_id = args["run_id"]
    _get_run(ctx, run_id)
    try:
        fn, original_input = ctx.resolve(run_id, args.get("agent"))
    except KeyError as e:
        raise ToolError(str(e)) from None
    report = ctx.analyzer.analyze(run_id, fn, original_input)
    return json.dumps({
        "run_id": report.run_id, "failed": report.failed,
        "root_cause": report.root_cause, "confidence": report.confidence,
        "recommended_fix": report.recommended_fix,
        "suggested_invariant": report.suggested_invariant,
        "summary": report.summary,
    }, default=str)


def _tool_test_generate(args: dict[str, Any], ctx: ServerContext) -> str:
    from agentcrash.tests_gen import generate_test, run_test

    run_id = args["run_id"]
    _get_run(ctx, run_id)
    try:
        fn, original_input = ctx.resolve(run_id, args.get("agent"))
    except KeyError as e:
        raise ToolError(str(e)) from None
    report = ctx.analyzer.analyze(run_id, fn, original_input)
    spec = generate_test(report, ctx.storage.get_run(run_id), original_input)
    test_id = ctx.storage.save_test(spec.name, spec.to_dict(), run_id)

    # Prove the test discriminates: it must fail the buggy agent and pass the
    # fixed one. Skip the fixed verdict if that variant isn't registered.
    buggy = run_test(spec, fn, ctx.replayer)
    out: dict[str, Any] = {
        "test_id": test_id, "test_name": spec.name,
        "invariant": spec.to_dict().get("invariant"),
        "vs_buggy": {"passed": buggy.passed, "status": buggy.status,
                     "violations": buggy.violations},
    }
    if "fixed" in ctx.variants and ctx.variants["fixed"] is not fn:
        try:
            fixed = run_test(spec, ctx.variants["fixed"], ctx.replayer)
            out["vs_fixed"] = {"passed": fixed.passed, "status": fixed.status,
                               "violations": fixed.violations}
        except Exception as e:  # noqa: BLE001
            out["vs_fixed"] = {"error": str(e)}
    return json.dumps(out, default=str)


# JSON Schema 2020-12 common subset — works against 2025-06-18 hosts too.
TOOLS: list[dict[str, Any]] = [
    {
        "name": "trace_search",
        "description": "List recent AgentCrash runs. Use to discover run_ids. "
                       "Filter by status='failed' to find crashes to debug.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["failed", "all"], "default": "all"},
                "agent": {"type": "string"},
                "limit": {"type": "integer", "default": 20},
            },
            "additionalProperties": False,
        },
        "handler": _tool_trace_search,
    },
    {
        "name": "trace_get",
        "description": "Get a run's summary and its event timeline. Set "
                       "include_content=true to include tool inputs/outputs (may be large/redacted).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "include_content": {"type": "boolean", "default": False},
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
        "handler": _tool_trace_get,
    },
    {
        "name": "replay_run",
        "description": "Replay a recorded run: exact (deterministic reproduction), "
                       "selective (counterfactual — apply interventions), or live (real calls, "
                       "requires consent_live=true).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "mode": {"type": "string", "enum": ["exact", "selective", "live"], "default": "exact"},
                "agent": {"type": "string", "default": "buggy"},
                "consent_live": {"type": "boolean", "default": False},
                "interventions": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
        "handler": _tool_replay_run,
    },
    {
        "name": "analyze_failure",
        "description": "Identify the root cause of a failed run via counterfactual replays. "
                       "Returns a cited root cause, confidence, recommended fix, and suggested invariant.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "agent": {"type": "string", "default": "buggy"},
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
        "handler": _tool_analyze_failure,
    },
    {
        "name": "test_generate",
        "description": "Generate and save a regression test from a failed run, then prove it "
                       "discriminates (fails the buggy agent, passes the fixed one).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "agent": {"type": "string", "default": "buggy"},
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
        "handler": _tool_test_generate,
    },
]

_TOOLS_BY_NAME = {t["name"]: t for t in TOOLS}


# ---------- JSON-RPC dispatch ----------

def _result(req_id: Any, result: dict[str, Any]) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error(req_id: Any, code: int, message: str, data: Any = None) -> str:
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return json.dumps({"jsonrpc": "2.0", "id": req_id, "error": err})


def _tool_list_result() -> dict[str, Any]:
    return {"tools": [{"name": t["name"], "description": t["description"],
                       "inputSchema": t["inputSchema"]} for t in TOOLS]}


def _handle(msg: dict[str, Any], ctx: ServerContext) -> str | None:
    """Return the JSON response line for a request, or None for notifications."""
    req_id = msg.get("id")
    method = msg.get("method")

    # Notifications (no id) get no response.
    if req_id is None and method is not None:
        return None

    if method == "initialize":
        return _result(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "AgentCrash observability & replay. Use trace_search to find runs, "
                "trace_get to inspect one, analyze_failure for root cause, "
                "replay_run to reproduce, test_generate to mint regression tests."
            ),
        })
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return _result(req_id, {})
    if method == "tools/list":
        return _result(req_id, _tool_list_result())
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        tool = _TOOLS_BY_NAME.get(name)
        if tool is None:
            return _error(req_id, -32602, f"unknown tool: {name!r}")
        try:
            text = tool["handler"](params.get("arguments") or {}, ctx)
        except InvalidParams as e:
            return _error(req_id, -32602, str(e))
        except ToolError as e:
            # Tool ran but failed -> result with isError: true (spec §7.2).
            return _result(req_id, {"content": [{"type": "text", "text": str(e)}], "isError": True})
        except Exception as e:  # noqa: BLE001
            return _result(req_id, {"content": [{"type": "text",
                    "text": f"{type(e).__name__}: {e}"}], "isError": True})
        return _result(req_id, {"content": [{"type": "text", "text": text}], "isError": False})

    return _error(req_id, -32601, f"method not found: {method!r}")


def serve(ctx: ServerContext, *, stdin=None, stdout=None) -> None:
    """Run the stdio MCP loop. Reads JSON-RPC lines from stdin, writes responses
    to stdout (one JSON object per line), logs to stderr."""
    sin = stdin or sys.stdin
    sout = stdout or sys.stdout
    for line in sin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            sout.write(_error(None, -32700, f"parse error: {e}") + "\n")
            sout.flush()
            continue
        resp = _handle(msg, ctx)
        if resp is not None:
            sout.write(resp + "\n")
            sout.flush()