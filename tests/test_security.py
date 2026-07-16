"""Security invariants required by the AgentCrash spec.

These are not nice-to-haves — they are the hard safety boundaries the platform
is built around, enforced in code and asserted here so a regression is loud:

1. Secrets are redacted at ingestion and never reach storage in plaintext.
2. Private chain-of-thought is never recorded — only observable behavior.
3. Replay never auto-executes replayed side-effecting calls (safe by default);
   LIVE mode requires explicit consent.
4. Untrusted external inputs (tool output, shell output, model output, MCP
   responses, file contents, HTTP responses) pass through redaction.
"""
from __future__ import annotations

import json

import pytest

from agentcrash.replay import ReplayConfig, Replayer
from agentcrash.schema import EventType
from agentcrash.sdk import CrashTracer

SECRET = "sk-ant-" + "q" * 24


# ---- 1. redaction at ingestion ----
def test_secret_in_tool_output_never_reaches_storage_plaintext(tmp_storage):
    tracer = CrashTracer(tmp_storage, integration="test", framework="t")
    with tracer.run("a", project="p") as run:
        run.tool("fetch_profile", {"id": "u1"}, lambda: {"bio": f"key={SECRET}"})
    rows = tmp_storage.get_events(run.run_id)
    blob = json.dumps([e.model_dump() for e in rows])
    assert SECRET not in blob, "secret must not be stored in plaintext"
    assert any(e.privacy.redacted for e in rows)


def test_secret_in_llm_output_redacted(tmp_storage):
    tracer = CrashTracer(tmp_storage, integration="test", framework="t")
    with tracer.run("a", project="p") as run:
        run.llm({"prompt": "summarize"}, lambda: {"text": f"here is the token {SECRET}"})
    blob = json.dumps([e.model_dump() for e in tmp_storage.get_events(run.run_id)])
    assert SECRET not in blob


def test_secret_in_shell_stdout_redacted(tmp_storage):
    tracer = CrashTracer(tmp_storage, integration="test", framework="t")
    with tracer.run("a", project="p") as run:
        run.call_external(kind="shell", name="env", signature={"cmd": "printenv"},
                          fn=lambda: SECRET, request_type=EventType.SHELL_COMMAND.value,
                          response_type=EventType.SHELL_STDOUT.value)
    blob = json.dumps([e.model_dump() for e in tmp_storage.get_events(run.run_id)])
    assert SECRET not in blob


def test_secret_in_http_response_redacted(tmp_storage):
    tracer = CrashTracer(tmp_storage, integration="test", framework="t")
    with tracer.run("a", project="p") as run:
        run.call_external(kind="http", name="api", signature={"url": "https://x"},
                          fn=lambda: {"authorization": f"Bearer {SECRET}"},
                          request_type=EventType.HTTP_REQUEST.value,
                          response_type=EventType.HTTP_RESPONSE.value)
    blob = json.dumps([e.model_dump() for e in tmp_storage.get_events(run.run_id)])
    assert SECRET not in blob


# ---- 2. no private chain-of-thought ----
def test_sdk_records_observable_decision_not_reasoning(tmp_storage):
    """The SDK exposes `decision()` for observable decisions. There is no API to
    record hidden reasoning, so it can never leak into a trace."""
    tracer = CrashTracer(tmp_storage, integration="test", framework="t")
    with tracer.run("a", project="p") as run:
        run.decision("refund_without_verification", {"order_id": "ORD-1", "verified": False})
    events = tmp_storage.get_events(run.run_id)
    decisions = [e for e in events if e.type == EventType.AGENT_DECISION.value]
    assert decisions
    # The decision records its label + observable detail, nothing more.
    assert decisions[0].name == "refund_without_verification"
    assert decisions[0].output == {"order_id": "ORD-1", "verified": False}


# ---- 3. safe replay: no auto-execution of side-effecting calls ----
def test_exact_replay_never_calls_real_side_effecting_fn(tmp_storage, demo):
    """A replayed side-effecting tool must return the frozen response, never run
    the real function. We assert by substituting a fn that would mutate state."""
    tracer = CrashTracer(tmp_storage, integration="test", framework="t")
    try:
        with tracer.run("support", model="stub", project="demo") as run:
            demo.buggy_agent(demo.DEMO_REQUEST, run)
    except Exception:
        pass
    rid = run.run_id
    fired = {"n": 0}

    def dangerous_refund(order_id):
        fired["n"] += 1
        return {"order_id": order_id, "refunded": True}

    def replay_agent(request, ctx):
        results = ctx.tool("search_customer", {"name": request["name"]},
                           lambda: demo.search_customer(request["name"]))
        ctx.llm({"role": "plan", "policy": "refund_first"}, lambda: {"policy": "refund_first"})
        return ctx.tool("refund_order", {"order_id": results[0]["order_id"]},
                        lambda: dangerous_refund(results[0]["order_id"]))

    Replayer(tmp_storage).replay(rid, replay_agent, demo.DEMO_REQUEST, ReplayConfig(mode="exact"))
    assert fired["n"] == 0, "replay must not execute the real side-effecting function"


def test_live_consent_gate_blocks_unconsented_live_replay(tmp_storage, demo):
    tracer = CrashTracer(tmp_storage, integration="test", framework="t")
    try:
        with tracer.run("support", model="stub", project="demo") as run:
            demo.buggy_agent(demo.DEMO_REQUEST, run)
    except Exception:
        pass
    with pytest.raises(PermissionError):
        Replayer(tmp_storage).replay(run.run_id, demo.buggy_agent, demo.DEMO_REQUEST,
                                     ReplayConfig(mode="live", consent_live=False))


# ---- 4. untrusted external inputs are redacted ----
@pytest.mark.parametrize("req,resp", [
    (EventType.TOOL_CALLED.value, EventType.TOOL_COMPLETED.value),
    (EventType.MCP_REQUEST.value, EventType.MCP_RESPONSE.value),
    (EventType.FILESYSTEM_READ.value, EventType.FILESYSTEM_READ.value),
])
def test_redaction_applies_across_external_input_kinds(tmp_storage, req, resp):
    tracer = CrashTracer(tmp_storage, integration="test", framework="t")
    with tracer.run("a", project="p") as run:
        run.call_external(kind="ext", name="source", signature={"q": "x"},
                          fn=lambda: {"data": SECRET},
                          request_type=req, response_type=resp)
    blob = json.dumps([e.model_dump() for e in tmp_storage.get_events(run.run_id)])
    assert SECRET not in blob