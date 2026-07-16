"""End-to-end vertical slice: RECORD -> REPLAY -> ANALYZE -> INTERVENE -> COMPARE
-> IDENTIFY ROOT CAUSE -> GENERATE REGRESSION TEST. Plus the HTTP API surface."""
from __future__ import annotations

from agentcrash.analyzer import Analyzer
from agentcrash.interventions import Intervention
from agentcrash.replay import ReplayConfig, Replayer
from agentcrash.sdk import CrashTracer
from agentcrash.tests_gen import generate_test, run_test


def test_full_vertical_slice(tmp_storage, demo):
    tracer = CrashTracer(tmp_storage, integration="demo", framework="agentcrash-demo")

    # 1. RECORD the buggy failure (let the exception propagate so the SDK marks it failed)
    try:
        with tracer.run("support", model="stub-decide-v1", project="demo",
                        metadata={"request": demo.DEMO_REQUEST}) as run:
            demo.buggy_agent(demo.DEMO_REQUEST, run)
    except Exception:
        pass
    rid = run.run_id
    assert tmp_storage.get_run(rid)["status"] == "failed"
    assert len(tmp_storage.get_events(rid)) >= 7

    replayer = Replayer(tmp_storage)

    # 2. REPLAY (exact) reproduces the failure with no behavioral diff
    exact = replayer.replay(rid, demo.buggy_agent, demo.DEMO_REQUEST, ReplayConfig(mode="exact"))
    assert exact.status == "failed"
    assert exact.diff.is_different is False

    # 3. ANALYZE -> root cause via disambiguation counterfactuals
    report = Analyzer(tmp_storage, replayer).analyze(rid, demo.buggy_agent, demo.DEMO_REQUEST)
    assert report.root_cause and report.confidence >= 0.5

    # 4. INTERVENE + COMPARE: replace search with only the correct customer averts
    events = tmp_storage.get_events(rid)
    search_ev = next(e for e in events if e.name == "search_customer" and e.replay and e.replay.frozen)
    correct = [c for c in search_ev.output if c["id"] == "CUST-002"]
    iv = Intervention(id="cf", type="replace_tool_response",
                      fixture_key=search_ev.replay.fixture_key, spec={"response": correct})
    cf = replayer.replay(rid, demo.buggy_agent, demo.DEMO_REQUEST,
                         ReplayConfig(mode="selective", interventions=[iv]))
    assert cf.status == "completed"
    assert cf.diff.result_changed is True  # failed -> completed

    # 5. GENERATE REGRESSION TEST and run against both agents
    spec = generate_test(report, tmp_storage.get_run(rid), demo.DEMO_REQUEST)
    assert run_test(spec, demo.buggy_agent, replayer).passed is False
    assert run_test(spec, demo.fixed_agent, replayer).passed is True


# ---- HTTP API ----
def test_http_api_runs_events_analyze_test(tmp_storage, demo):
    from agentcrash.server import ServerContext, create_app

    ctx = ServerContext(tmp_storage, seed_demo=False)
    # seed one failed run manually via the demo agent
    tracer = CrashTracer(tmp_storage, integration="demo", framework="agentcrash-demo")
    try:
        with tracer.run("support", model="stub", project="demo",
                        metadata={"request": demo.DEMO_REQUEST}) as run:
            demo.buggy_agent(demo.DEMO_REQUEST, run)
    except Exception:
        pass
    ctx.register_run(run.run_id, demo.buggy_agent, demo.DEMO_REQUEST)
    ctx.register_variant("buggy", demo.buggy_agent)
    ctx.register_variant("fixed", demo.fixed_agent)

    from starlette.testclient import TestClient

    app = create_app(ctx)
    client = TestClient(app)

    r = client.get("/api/runs")
    assert r.status_code == 200
    runs = r.json()
    assert any(rr["id"] == run.run_id for rr in runs)

    r = client.get(f"/api/runs/{run.run_id}/events")
    assert r.status_code == 200
    assert len(r.json()) >= 7

    r = client.post(f"/api/runs/{run.run_id}/analyze", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["confidence"] >= 0.5
    assert body["suggested_invariant"]["action"] == "refund_order"

    # generate + run a regression test via the API
    r = client.post(f"/api/runs/{run.run_id}/test/generate", json={})
    assert r.status_code == 200
    test_id = r.json()["test_id"]

    r = client.post(f"/api/tests/{test_id}/run", json={"agent": "buggy"})
    assert r.status_code == 200
    assert r.json()["passed"] is False

    r = client.post(f"/api/tests/{test_id}/run", json={"agent": "fixed"})
    assert r.status_code == 200
    assert r.json()["passed"] is True


def test_http_api_replay_counterfactual(tmp_storage, demo):
    from starlette.testclient import TestClient

    from agentcrash.server import ServerContext, create_app

    tracer = CrashTracer(tmp_storage, integration="demo", framework="agentcrash-demo")
    try:
        with tracer.run("support", model="stub", project="demo",
                        metadata={"request": demo.DEMO_REQUEST}) as run:
            demo.buggy_agent(demo.DEMO_REQUEST, run)
    except Exception:
        pass
    ctx = ServerContext(tmp_storage, seed_demo=False)
    ctx.register_run(run.run_id, demo.buggy_agent, demo.DEMO_REQUEST)
    ctx.register_variant("buggy", demo.buggy_agent)
    app = create_app(ctx)
    client = TestClient(app)

    # Mirror the UI's "disambiguate to correct customer" counterfactual: replace
    # the search_customer tool response with only the correct customer.
    r = client.post(f"/api/runs/{run.run_id}/replay",
                    json={"mode": "selective", "agent": "buggy",
                          "interventions": [{"type": "replace_tool_response",
                                             "kind": "tool", "name": "search_customer",
                                             "spec": {"response": [
                                                 {"id": "CUST-002", "name": "John Smith",
                                                  "email": "b@x.com", "order_id": "ORD-456",
                                                  "order_total": 89.5}]}}]})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "completed"