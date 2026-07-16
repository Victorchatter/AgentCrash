"""MCP client-side instrumentation: record -> replay loop.

Drives MCPClientRecorder with synthetic `fn`s through a real CrashTracer run
and the Replayer, with no `mcp` dependency installed. Asserts that MCP traffic
sits on the same replayable / counterfactual / redaction rail as ordinary
tools — the whole point of funneling through ctx.call_external(kind="mcp").
"""
from __future__ import annotations

import os
import sys

import pytest

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from agentcrash.integrations.mcp_client import MCPClientRecorder, RecordingClientSession  # noqa: E402
from agentcrash.interventions import Intervention  # noqa: E402
from agentcrash.replay import ReplayConfig, Replayer  # noqa: E402
from agentcrash.sdk import CrashTracer, _fixture_key  # noqa: E402


@pytest.fixture
def tracer(tmp_storage):
    return CrashTracer(tmp_storage, integration="mcp-test", framework="agentcrash-tests")


def _record_run(tracer, agent_fn, request):
    """Run agent_fn(request, ctx) inside a tracer.run and return the run_id.
    The run context manager marks a raised exception as a failed run; we swallow
    it here so the caller always gets the run_id back to inspect/replay."""
    run_id = None
    try:
        with tracer.run("mcp-agent", model="stub", project="mcp", metadata={"request": request}) as run:
            run_id = run.run_id
            agent_fn(request, run)
    except Exception:
        pass  # run already recorded as failed by RunContext.__exit__
    return run_id


# ---------- 1. success: exact replay returns frozen output without calling fn ----------

def test_call_tool_success_replays_frozen_without_calling_fn(tracer, tmp_storage):
    calls = {"n": 0}

    def real_call():
        calls["n"] += 1
        return {"content": [{"type": "text", "text": "CUST-001"}], "isError": False}

    def agent(req, ctx):
        rec = MCPClientRecorder(ctx, "billing")
        return rec.call_tool("search", {"q": "john"}, real_call)

    rid = _record_run(tracer, agent, {"q": "john"})
    assert calls["n"] == 1  # record called the real fn exactly once

    # Exact replay: fn must NOT run; frozen output returned verbatim.
    # The replayed agent uses the SAME tool name + args so the fixture key matches.
    def replay_agent_same(req, ctx):
        rec = MCPClientRecorder(ctx, "billing")
        return rec.call_tool("search", {"q": "john"},
                              lambda: (_ for _ in ()).throw(AssertionError("fn must not run on replay")))

    result = Replayer(tmp_storage).replay(rid, replay_agent_same, {"q": "john"}, ReplayConfig(mode="exact"))
    assert result.status == "completed"
    assert result.agent_output == {"content": [{"type": "text", "text": "CUST-001"}], "isError": False}
    assert calls["n"] == 1  # unchanged — real fn never re-invoked
    assert result.diff.is_different is False


# ---------- 2. isError: true is a returned value, replay must not raise ----------

def test_iserror_result_recorded_completed_and_replayed_verbatim(tracer, tmp_storage):
    err_result = {"content": [{"type": "text", "text": "tool blew up"}], "isError": True}

    def agent(req, ctx):
        rec = MCPClientRecorder(ctx, "billing")
        return rec.call_tool("charge", {"amount": 100}, lambda: err_result)

    rid = _record_run(tracer, agent, {})

    def replay_agent(req, ctx):
        rec = MCPClientRecorder(ctx, "billing")
        return rec.call_tool("charge", {"amount": 100}, lambda: pytest.fail("should not run"))

    result = Replayer(tmp_storage).replay(rid, replay_agent, {}, ReplayConfig(mode="exact"))
    assert result.status == "completed"  # the RPC completed; isError is a value, not a raise
    assert result.agent_output == err_result
    # the recorded MCP_RESPONSE must carry isError in the output (not as a raise/error)
    resp = [e for e in tmp_storage.get_events(rid) if e.type == "mcp.response"][0]
    assert resp.output["isError"] is True
    assert resp.is_error is False  # completed status, not a failed event


# ---------- 3. JSON-RPC / transport error: fn raises; replay re-raises ----------

def test_protocol_error_recorded_failed_and_reraised_on_replay(tracer, tmp_storage):
    class RPCError(Exception):
        pass

    def agent(req, ctx):
        rec = MCPClientRecorder(ctx, "billing")
        return rec.call_tool("refund", {"id": "ORD-1"}, lambda: (_ for _ in ()).throw(RPCError("invalid params")))

    rid = _record_run(tracer, agent, {})  # agent raises -> run fails

    def replay_agent(req, ctx):
        rec = MCPClientRecorder(ctx, "billing")
        return rec.call_tool("refund", {"id": "ORD-1"}, lambda: pytest.fail("should not run"))

    result = Replayer(tmp_storage).replay(rid, replay_agent, {}, ReplayConfig(mode="exact"))
    assert result.status == "failed"
    assert "RPCError" in (result.error or "")
    # recorded event is a failed MCP_RESPONSE with the error info, frozen for replay
    resp = [e for e in tmp_storage.get_events(rid) if e.type == "mcp.response" and e.status == "failed"][0]
    assert resp.error is not None and "invalid params" in resp.error.message


# ---------- 4. redaction: bearer token in tool args is scrubbed before storage ----------

def test_secret_in_mcp_tool_args_is_redacted(tracer, tmp_storage):
    secret = "Bearer abcdefghijklmnop1234567890"

    def agent(req, ctx):
        rec = MCPClientRecorder(ctx, "billing")
        return rec.call_tool("charge", {"Authorization": secret, "amount": 5}, lambda: {"ok": True})

    rid = _record_run(tracer, agent, {})
    events = tmp_storage.get_events(rid)
    req = [e for e in events if e.type == "mcp.request"][0]
    assert req.privacy.redacted is True
    # token must not appear anywhere in the stored input
    assert secret not in os.environ  # sanity: not leaking via env
    import json
    blob = json.dumps([e.model_dump() for e in events], default=str)
    assert secret not in blob
    assert "REDACTED" in blob


# ---------- 5. counterfactual: replace_tool_response on the MCP fixture changes the outcome ----------

def test_counterfactual_replace_tool_response_on_mcp_fixture(tracer, tmp_storage):
    def agent(req, ctx):
        rec = MCPClientRecorder(ctx, "billing")
        out = rec.call_tool("search", {"q": "john"},
                            lambda: [{"id": "CUST-001"}, {"id": "CUST-002"}])
        ctx.decision("pick", {"first": out[0]}) if out else None
        return out

    rid = _record_run(tracer, agent, {"q": "john"})

    # Compute the exact fixture key the recorder used, then override the response.
    fkey = _fixture_key("mcp", "billing.search", {"server": "billing", "tool": "search", "arguments": {"q": "john"}})
    iv = Intervention(id="cf", type="replace_tool_response", fixture_key=fkey,
                      spec={"response": [{"id": "CUST-999"}]})

    def replay_agent(req, ctx):
        rec = MCPClientRecorder(ctx, "billing")
        return rec.call_tool("search", {"q": "john"},
                             lambda: pytest.fail("exact+intervention must use the overridden fixture, not fn"))

    result = Replayer(tmp_storage).replay(rid, replay_agent, {"q": "john"},
                                           ReplayConfig(mode="exact", interventions=[iv]))
    assert result.status == "completed"
    assert result.agent_output == [{"id": "CUST-999"}]  # the counterfactual response


# ---------- 6. RecordingClientSession requires the mcp extra (skip if installed) ----------

def test_recording_client_session_requires_mcp_extra():
    try:
        import mcp  # noqa: F401
    except ImportError:
        # mcp not installed: constructing the wrapper must raise a helpful ImportError.
        with pytest.raises(ImportError, match="agentcrash\\[mcp\\]"):
            RecordingClientSession(underlying=None, ctx=None, server="x")
    else:  # pragma: no cover
        pytest.skip("mcp installed; the not-installed branch is covered in CI without the extra")