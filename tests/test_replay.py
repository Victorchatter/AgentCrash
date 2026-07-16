"""Replay engine tests: exact reproduction, selective simulated live, safe-replay
boundaries, and the live-mode consent gate."""
from __future__ import annotations

import pytest

from agentcrash.interventions import Intervention
from agentcrash.replay import (
    ReplayConfig,
    Replayer,
)
from agentcrash.sdk import CrashTracer


def _record_buggy(tmp_storage, demo):
    tracer = CrashTracer(tmp_storage, integration="test", framework="t")
    try:
        with tracer.run("support", model="stub", project="demo",
                        metadata={"request": demo.DEMO_REQUEST}) as run:
            demo.buggy_agent(demo.DEMO_REQUEST, run)
    except Exception:
        pass
    return run.run_id


def test_exact_replay_reproduces_failure(tmp_storage, demo):
    rid = _record_buggy(tmp_storage, demo)
    r = Replayer(tmp_storage).replay(rid, demo.buggy_agent, demo.DEMO_REQUEST, ReplayConfig(mode="exact"))
    assert r.status == "failed"
    # exact replay is behaviorally identical to the original failure
    assert r.diff.is_different is False


def test_selective_replay_with_correct_customer_averts(tmp_storage, demo):
    rid = _record_buggy(tmp_storage, demo)
    # Find the search fixture key via the recorded events.
    events = tmp_storage.get_events(rid)
    search_ev = next(e for e in events if e.name == "search_customer" and e.replay and e.replay.frozen)
    correct = [c for c in search_ev.output if c["id"] == "CUST-002"]
    iv = Intervention(id="cf", type="replace_tool_response",
                      fixture_key=search_ev.replay.fixture_key, spec={"response": correct})
    r = Replayer(tmp_storage).replay(rid, demo.buggy_agent, demo.DEMO_REQUEST,
                                     ReplayConfig(mode="selective", interventions=[iv]))
    assert r.status == "completed"
    assert r.diff.result_changed is True


def test_exact_mode_diverges_when_intervention_changes_control_flow(tmp_storage, demo):
    """Exact mode must NOT run a tool live when its args changed (no frozen fixture).

    The intervention changes the selected customer -> refund_order is called with a
    different order_id -> different fixture key -> exact mode refuses to run it live.
    This is the safe-replay boundary working as designed: the replay surfaces as a
    *failed* run citing the divergence, never silently running the side effect live.
    """
    rid = _record_buggy(tmp_storage, demo)
    events = tmp_storage.get_events(rid)
    search_ev = next(e for e in events if e.name == "search_customer" and e.replay and e.replay.frozen)
    correct = [c for c in search_ev.output if c["id"] == "CUST-002"]
    iv = Intervention(id="cf", type="replace_tool_response",
                      fixture_key=search_ev.replay.fixture_key, spec={"response": correct})
    r = Replayer(tmp_storage).replay(rid, demo.buggy_agent, demo.DEMO_REQUEST,
                                     ReplayConfig(mode="exact", interventions=[iv]))
    assert r.status == "failed"
    assert "MissingFixtureError" in (r.error or "")


def test_live_mode_requires_explicit_consent(tmp_storage, demo):
    rid = _record_buggy(tmp_storage, demo)
    with pytest.raises(PermissionError):
        Replayer(tmp_storage).replay(rid, demo.buggy_agent, demo.DEMO_REQUEST,
                                     ReplayConfig(mode="live", consent_live=False))


def test_live_mode_with_consent_runs_against_real_environment(tmp_storage, demo):
    rid = _record_buggy(tmp_storage, demo)
    # The demo tools are pure/in-memory, so a live replay against them reproduces
    # the original failure faithfully (the "environment" is safe here).
    r = Replayer(tmp_storage).replay(rid, demo.buggy_agent, demo.DEMO_REQUEST,
                                     ReplayConfig(mode="live", consent_live=True))
    assert r.status == "failed"
    assert "WrongCustomerError" in (r.error or "")


def test_replay_does_not_run_real_fn_on_frozen_calls(tmp_storage, demo):
    """Frozen replay must return the captured response WITHOUT calling the real fn."""
    rid = _record_buggy(tmp_storage, demo)
    calls = {"n": 0}

    def spy_agent(request, ctx):
        # Provide a fn that would record a call; it must never run in exact replay.
        def boom():
            calls["n"] += 1
            raise RuntimeError("real fn ran during replay!")
        return ctx.tool("search_customer", {"name": request["name"]}, boom)

    r = Replayer(tmp_storage).replay(rid, spy_agent, demo.DEMO_REQUEST, ReplayConfig(mode="exact"))
    assert calls["n"] == 0, "frozen replay must not invoke the real function"
    # search result is returned from the fixture, so the agent does not raise
    assert r.status in ("completed", "failed")


def test_frozen_failure_surfaces_original_exception_type(tmp_storage, demo):
    """When replaying a frozen failed call, the surfaced error keeps the original
    exception type, not the internal ReplayFrozenError wrapper."""
    rid = _record_buggy(tmp_storage, demo)
    r = Replayer(tmp_storage).replay(rid, demo.buggy_agent, demo.DEMO_REQUEST, ReplayConfig(mode="exact"))
    assert "WrongCustomerError" in (r.error or "")
    assert "ReplayFrozenError" not in (r.error or "")


def test_selective_replay_runs_divergent_pure_kind_live(tmp_storage, demo):
    """In selective mode, a divergent call to a simulated-live kind (tool) runs
    against the provided fn instead of raising MissingFixtureError."""
    rid = _record_buggy(tmp_storage, demo)
    # fixed_agent diverges: it calls verify_customer (not in the buggy fixture).
    r = Replayer(tmp_storage).replay(rid, demo.fixed_agent, demo.DEMO_REQUEST,
                                     ReplayConfig(mode="selective"))
    # fixed agent verifies and refunds the correct order -> completed
    assert r.status == "completed"