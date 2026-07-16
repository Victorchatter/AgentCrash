"""Analyzer, regression-test generation, and chaos engine tests."""
from __future__ import annotations

from agentcrash.analyzer import Analyzer
from agentcrash.chaos import run_chaos
from agentcrash.replay import Replayer
from agentcrash.sdk import CrashTracer
from agentcrash.tests_gen import TestSpec, generate_test, run_test


def _record_buggy(tmp_storage, demo):
    tracer = CrashTracer(tmp_storage, integration="test", framework="t")
    try:
        with tracer.run("support", model="stub", project="demo",
                        metadata={"request": demo.DEMO_REQUEST}) as run:
            demo.buggy_agent(demo.DEMO_REQUEST, run)
    except Exception:
        pass
    return run.run_id


def test_analyzer_identifies_ambiguous_selection_root_cause(tmp_storage, demo):
    rid = _record_buggy(tmp_storage, demo)
    rep = Analyzer(tmp_storage, Replayer(tmp_storage)).analyze(rid, demo.buggy_agent, demo.DEMO_REQUEST)
    assert rep.failed
    assert rep.root_cause is not None
    assert "ambiguous" in rep.root_cause.lower() or "wrong record" in rep.root_cause.lower()
    # decisive disambiguation with exactly one averter -> ~90% confidence
    assert 0.85 <= rep.confidence <= 0.97
    # evidence: at least one avert and at least one reproduce
    averts = [e for e in rep.evidence if e.averted]
    reproduces = [e for e in rep.evidence if not e.averted]
    assert averts, "analyzer should find an averting counterfactual"
    assert reproduces, "analyzer should find a reproducing counterfactual"


def test_analyzer_suggests_verify_before_refund_invariant(tmp_storage, demo):
    rid = _record_buggy(tmp_storage, demo)
    rep = Analyzer(tmp_storage, Replayer(tmp_storage)).analyze(rid, demo.buggy_agent, demo.DEMO_REQUEST)
    inv = rep.suggested_invariant
    assert inv is not None
    assert inv["type"] == "action_requires_preceding"
    assert inv["action"] == "refund_order"
    assert inv["preceding"] == "verify_customer"
    assert "refund_order" in (rep.recommended_fix or "")


def test_analyzer_on_non_failure_returns_no_cause(tmp_storage, demo):
    tracer = CrashTracer(tmp_storage, integration="test", framework="t")
    with tracer.run("support", model="stub", project="demo") as run:
        demo.fixed_agent(demo.DEMO_REQUEST, run)
    rep = Analyzer(tmp_storage, Replayer(tmp_storage)).analyze(run.run_id, demo.fixed_agent, demo.DEMO_REQUEST)
    assert rep.failed is False
    assert rep.root_cause is None or "did not fail" in " ".join(rep.summary).lower()


def test_generate_test_fails_on_buggy_passes_on_fixed(tmp_storage, demo):
    rid = _record_buggy(tmp_storage, demo)
    rep = Analyzer(tmp_storage, Replayer(tmp_storage)).analyze(rid, demo.buggy_agent, demo.DEMO_REQUEST)
    spec = generate_test(rep, tmp_storage.get_run(rid), demo.DEMO_REQUEST)
    assert spec.name == "require_verify_customer_before_refund_order"
    assert any(i["type"] == "action_requires_preceding" for i in spec.invariants)
    assert "verify_customer" in spec.required_actions
    replayer = Replayer(tmp_storage)
    buggy = run_test(spec, demo.buggy_agent, replayer)
    fixed = run_test(spec, demo.fixed_agent, replayer)
    assert buggy.passed is False, f"expected fail on buggy; violations={buggy.violations}"
    assert fixed.passed is True, f"expected pass on fixed; violations={fixed.violations}"


def test_testspec_roundtrips_through_dict():
    spec = TestSpec(name="t", input={"q": 1}, source_run_id="r", invariants=[{"type": "forbidden_action", "action": "x"}],
                    forbidden_actions=["x"], required_actions=["y"], must_succeed=True,
                    description="d")
    d = spec.to_dict()
    spec2 = TestSpec.from_dict(d)
    assert spec2.name == "t"
    assert spec2.required_actions == ["y"]
    assert spec2.invariants[0]["action"] == "x"


def test_chaos_detects_non_recovery_on_injected_fault(tmp_storage, demo):
    rid = _record_buggy(tmp_storage, demo)
    spec = {
        "name": "search_timeout_recovery",
        "target": {"kind": "tool", "name": "search_customer"},
        "fault": {"type": "inject_failure", "message": "AgentCrash dropped this call"},
        "expected": {"must_recover": True, "avoid_duplicate_side_effects": True, "max_retries": 2},
    }
    res = run_chaos(spec, demo.buggy_agent, demo.DEMO_REQUEST, rid, Replayer(tmp_storage))
    # Buggy agent has no recovery path -> it should not recover.
    assert res.status == "failed"
    assert res.passed is False
    assert any("recover" in v for v in res.violations)


def test_chaos_fixed_agent_recovers_or_fails_safely(tmp_storage, demo):
    # Record a fixed run as the chaos source.
    tracer = CrashTracer(tmp_storage, integration="test", framework="t")
    with tracer.run("support", model="stub", project="demo") as run:
        demo.fixed_agent(demo.DEMO_REQUEST, run)
    rid = run.run_id
    spec = {
        "name": "verify_timeout",
        "target": {"kind": "tool", "name": "verify_customer"},
        "fault": {"type": "inject_failure", "message": "verify unavailable"},
        "expected": {"must_recover": True, "avoid_duplicate_side_effects": True, "max_retries": 4},
    }
    res = run_chaos(spec, demo.fixed_agent, demo.DEMO_REQUEST, rid, Replayer(tmp_storage))
    # Fixed agent gives up cleanly (no wrong refund, no duplicate side effects).
    assert "refund_order" not in " ".join(res.violations)
    assert not any("duplicate" in v.lower() for v in res.violations)