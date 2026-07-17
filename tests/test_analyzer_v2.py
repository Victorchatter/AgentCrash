"""Causal analysis v2 — multi-intervention combination probing.

A genuine multi-root failure: the agent makes two ambiguous searches and
commits a side effect that fails if EITHER selection is wrong. Fixing either
search alone does not avert (the other wrong selection still trips the
commit); only fixing BOTH averts. v1 (single-candidate) reports "did not
cleanly avert" at ~40%; v2 detects the multi-root combination.
"""

from __future__ import annotations

from agentcrash.analyzer import Analyzer
from agentcrash.replay import Replayer
from agentcrash.sdk import CrashTracer


def _commit(a: str, b: str) -> dict:
    # Fails if EITHER selection is the wrong record (A1 or B1).
    if a == "A1" or b == "B1":
        raise RuntimeError(f"bad combo a={a} b={b}")
    return {"ok": True, "a": a, "b": b}


def multi_root_agent(request, ctx):
    r1 = ctx.tool("search_a", {"q": "x"}, lambda: [{"id": "A1"}, {"id": "A2"}])
    r2 = ctx.tool("search_b", {"q": "y"}, lambda: [{"id": "B1"}, {"id": "B2"}])
    a, b = r1[0]["id"], r2[0]["id"]
    return ctx.tool("commit", {"a": a, "b": b}, lambda: _commit(a, b))


def _record(tmp_storage):
    tracer = CrashTracer(tmp_storage, integration="test", framework="t")
    try:
        with tracer.run("multi", model="stub", project="p") as run:
            multi_root_agent({}, run)
    except Exception:
        pass
    return run.run_id


def test_single_candidate_probes_do_not_avert(tmp_storage):
    """Sanity: fixing either search alone does not avert the multi-root failure."""
    rid = _record(tmp_storage)
    rep = Analyzer(tmp_storage, Replayer(tmp_storage)).analyze(rid, multi_root_agent, {})
    assert rep.failed
    # no single candidate averted on its own
    assert not any(c.averted for c in rep.candidates)


def test_v2_detects_multi_root_combination(tmp_storage):
    rid = _record(tmp_storage)
    rep = Analyzer(tmp_storage, Replayer(tmp_storage)).analyze(rid, multi_root_agent, {})
    assert rep.multi_root is True
    assert len(rep.root_events) == 2
    assert "multi-root" in rep.root_cause.lower() or "more than one" in rep.root_cause.lower()
    assert 0.7 <= rep.confidence <= 0.9
    # evidence: the combination averts; single fixes reproduce
    averts = [e for e in rep.evidence if e.averted]
    reproduces = [e for e in rep.evidence if not e.averted]
    assert averts, "combination should avert"
    assert reproduces, "single fixes should still reproduce"
    assert rep.recommended_fix is not None
    assert rep.suggested_invariant is not None
    assert rep.suggested_invariant["type"] == "action_requires_preceding_multi"


def test_v2_does_not_trigger_for_single_root_demo(tmp_storage, demo):
    """The single-root demo must still report single-root (not multi-root)."""
    tracer = CrashTracer(tmp_storage, integration="test", framework="t")
    try:
        with tracer.run("support", model="stub", project="demo",
                        metadata={"request": demo.DEMO_REQUEST}) as run:
            demo.buggy_agent(demo.DEMO_REQUEST, run)
    except Exception:
        pass
    rep = Analyzer(tmp_storage, Replayer(tmp_storage)).analyze(run.run_id, demo.buggy_agent, demo.DEMO_REQUEST)
    assert rep.multi_root is False
    assert 0.85 <= rep.confidence <= 0.97  # unchanged single-root confidence