"""End-to-end test for the AgentCrash stdio MCP server.

Drives the JSON-RPC 2.0 stdio loop directly (no subprocess) through the
canonical agent workflow: initialize -> tools/list -> trace_search ->
analyze_failure -> test_generate. Asserts both the protocol surface and the
MCP error-channel distinction (tool-execution failure via isError, not a
JSON-RPC error).
"""
from __future__ import annotations

import io
import json
import os
import sys

# repo root on sys.path so examples.demo_agent is importable
_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import pytest  # noqa: E402

from agentcrash.mcp_server import PROTOCOL_VERSION, _handle, serve  # noqa: E402
from agentcrash.server import build_server  # noqa: E402


def _rpc(method: str, *, _id: int, params: dict | None = None) -> str:
    msg = {"jsonrpc": "2.0", "id": _id, "method": method}
    if params is not None:
        msg["params"] = params
    return json.dumps(msg)


def _run_lines(lines: list[str], ctx) -> list[dict]:
    out = io.StringIO()
    serve(ctx, stdin=io.StringIO("\n".join(lines) + "\n"), stdout=out)
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


@pytest.fixture
def mcp_ctx(tmp_path):
    db = str(tmp_path / "ac.db")
    _app, ctx = build_server(db, seed_demo=True)
    yield ctx
    ctx.storage.close()


def test_mcp_initialize_negotiates_protocol(mcp_ctx):
    resp = json.loads(_handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, mcp_ctx))
    assert resp["id"] == 1
    assert resp["result"]["protocolVersion"] == PROTOCOL_VERSION
    assert resp["result"]["serverInfo"]["name"] == "agentcrash"
    assert "tools" in resp["result"]["capabilities"]


def test_mcp_tools_list_exposes_surface(mcp_ctx):
    resp = json.loads(_handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, mcp_ctx))
    names = {t["name"] for t in resp["result"]["tools"]}
    assert names == {"trace_search", "trace_get", "replay_run", "analyze_failure", "test_generate"}
    # every tool has a JSON Schema input
    for t in resp["result"]["tools"]:
        assert t["inputSchema"]["type"] == "object"


def test_mcp_full_workflow_over_stdio(mcp_ctx):
    # 1) find the seeded failed demo run
    resps = _run_lines([_rpc("tools/call", _id=10, params={"name": "trace_search",
                          "arguments": {"status": "failed"}})], mcp_ctx)
    search = json.loads(resps[0]["result"]["content"][0]["text"])
    assert search["count"] >= 1
    failed_run = search["runs"][0]["run_id"]
    assert search["runs"][0]["status"] == "failed"

    # 2) analyze it -> cited root cause with high confidence (demo ~89%)
    resps = _run_lines([_rpc("tools/call", _id=11, params={"name": "analyze_failure",
                          "arguments": {"run_id": failed_run}})], mcp_ctx)
    payload = json.loads(resps[0]["result"]["content"][0]["text"])
    assert payload["failed"] is True
    assert "search_customer" in payload["root_cause"]
    assert payload["confidence"] >= 0.8
    assert payload["recommended_fix"]

    # 3) generate a regression test that discriminates buggy vs fixed
    resps = _run_lines([_rpc("tools/call", _id=12, params={"name": "test_generate",
                          "arguments": {"run_id": failed_run}})], mcp_ctx)
    payload = json.loads(resps[0]["result"]["content"][0]["text"])
    assert payload["vs_buggy"]["passed"] is False
    assert payload["vs_fixed"]["passed"] is True
    assert payload["test_id"]


def test_mcp_unknown_run_is_invalid_params_not_tool_error(mcp_ctx):
    """Spec §9.5: unknown run_id -> JSON-RPC -32602, not an isError result."""
    resp = json.loads(_handle({"jsonrpc": "2.0", "id": 20, "method": "tools/call",
                               "params": {"name": "trace_get", "arguments": {"run_id": "nope"}}}, mcp_ctx))
    assert resp["error"]["code"] == -32602
    assert "not found" in resp["error"]["message"]


def test_mcp_unknown_tool_is_invalid_params(mcp_ctx):
    resp = json.loads(_handle({"jsonrpc": "2.0", "id": 21, "method": "tools/call",
                               "params": {"name": "bogus_tool", "arguments": {}}}, mcp_ctx))
    assert resp["error"]["code"] == -32602


def test_mcp_unknown_method_is_method_not_found(mcp_ctx):
    resp = json.loads(_handle({"jsonrpc": "2.0", "id": 22, "method": "resources/read"}, mcp_ctx))
    assert resp["error"]["code"] == -32601


def test_mcp_notifications_get_no_response():
    # notifications carry no id -> serve() must not emit a line for them
    out = io.StringIO()
    from agentcrash.mcp_server import serve as _serve
    # a bare ctx is unused for a notification-only stream; pass a minimal stub
    class _Stub:
        pass
    _serve(_Stub(), stdin=io.StringIO(json.dumps({"jsonrpc": "2.0",
                "method": "notifications/initialized"}) + "\n"), stdout=out)
    assert out.getvalue() == ""