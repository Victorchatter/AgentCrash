"""Temporary end-to-end smoke test of the AgentCrash Python vertical slice."""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "examples"))

from demo_agent import DEMO_REQUEST, buggy_agent, fixed_agent

from agentcrash.analyzer import Analyzer
from agentcrash.replay import ReplayConfig, Replayer
from agentcrash.sdk import CrashTracer
from agentcrash.storage import Storage
from agentcrash.tests_gen import generate_test, run_test


def main() -> None:
    tmp = tempfile.mkdtemp(prefix="agentcrash_smoke_")
    db = os.path.join(tmp, "ac.db")
    storage = Storage(db)
    tracer = CrashTracer(storage, integration="demo", framework="agentcrash-demo")

    # 1. RECORD: buggy agent fails (let the exception propagate so the run is
    #    marked failed by the SDK)
    run_id = None
    try:
        with tracer.run("support", model="stub-decide-v1", project="demo",
                        metadata={"request": DEMO_REQUEST}) as run:
            buggy_agent(DEMO_REQUEST, run)
    except Exception as e:
        print(f"buggy agent raised (expected): {type(e).__name__}: {e}")
    run_id = run.run_id
    print(f"\n[1] Recorded buggy run: {run_id} (status={storage.get_run(run_id)['status']})")
    ev = storage.get_events(run_id)
    print(f"    events: {len(ev)}; types: {[e.type for e in ev]}")

    # 2. REPLAY exact — should reproduce the failure
    replayer = Replayer(storage)
    exact = replayer.replay(run_id, buggy_agent, DEMO_REQUEST, ReplayConfig(mode="exact"))
    print(f"\n[2] Exact replay: status={exact.status} (expect failed); diff_same={not exact.diff.is_different}")

    # 3. ANALYZE — counterfactual disambiguation
    analyzer = Analyzer(storage, replayer)
    report = analyzer.analyze(run_id, buggy_agent, DEMO_REQUEST)
    print("\n[3] " + "\n".join(report.summary))
    print(f"    root_cause={report.root_cause}")
    print(f"    confidence={report.confidence}")
    print(f"    suggested_invariant={report.suggested_invariant}")

    # 4. GENERATE TEST
    test = generate_test(report, storage.get_run(run_id), DEMO_REQUEST)
    print(f"\n[4] Generated test: {test.name}")
    print(f"    invariants={test.invariants}")
    print(f"    required_actions={test.required_actions}")

    # 5. RUN TEST against buggy (should FAIL) and fixed (should PASS)
    buggy_result = run_test(test, buggy_agent, replayer)
    print(f"\n[5a] Test vs BUGGY agent: passed={buggy_result.passed} (expect False)")
    print(f"     violations={buggy_result.violations}")
    fixed_result = run_test(test, fixed_agent, replayer)
    print(f"[5b] Test vs FIXED agent: passed={fixed_result.passed} (expect True)")
    print(f"     violations={fixed_result.violations}")

    assert storage.get_run(run_id)["status"] == "failed", "buggy run should fail"
    assert exact.status == "failed", "exact replay should reproduce failure"
    assert report.confidence >= 0.5, "analyzer should be confident"
    assert not buggy_result.passed, "test should fail against buggy agent"
    assert fixed_result.passed, f"test should pass against fixed agent; violations={fixed_result.violations}"
    print("\n✅ SMOKE PASSED — full vertical slice works end-to-end.")


if __name__ == "__main__":
    main()