"""FastAPI server — local web API + UI for AgentCrash.

Serves the REST API the web UI talks to and, if the frontend has been built,
serves it as static files at ``/``. Replay/analyze/test endpoints require the
agent function that produced a run to be registered with the server (an agent
function cannot be deserialized from storage). The demo registers itself, so
the full RECORD→REPLAY→ANALYZE→TEST flow works end-to-end against demo runs.
For arbitrary foreign runs, the static trace views work; live replay requires
an in-process registration (roadmap: agent-registration MCP tool).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agentcrash.analyzer import Analyzer
from agentcrash.chaos import run_chaos
from agentcrash.interventions import Intervention
from agentcrash.replay import ReplayConfig, Replayer
from agentcrash.storage import Storage
from agentcrash.tests_gen import TestSpec, generate_test, run_test


class ReplayRequest(BaseModel):
    mode: str = "exact"
    live_kinds: list[str] = []
    interventions: list[dict[str, Any]] = []
    consent_live: bool = False
    agent: str = "buggy"  # which registered variant to replay as


class AnalyzeRequest(BaseModel):
    agent: str = "buggy"


class TestRunRequest(BaseModel):
    agent: str = "fixed"
    interventions: list[dict[str, Any]] = []


class ChaosRequest(BaseModel):
    agent: str = "buggy"
    spec: dict[str, Any]


class ServerContext:
    def __init__(self, storage: Storage, *, seed_demo: bool = True):
        self.storage = storage
        self.replayer = Replayer(storage)
        self.analyzer = Analyzer(storage, self.replayer)
        # run_id -> (agent_fn, original_input)
        self._agents: dict[str, tuple[Callable, Any]] = {}
        # named agent variants available for replay/test (e.g. demo buggy/fixed)
        self.variants: dict[str, Callable] = {}
        if seed_demo:
            self._seed_demo()

    def register_variant(self, name: str, fn: Callable) -> None:
        self.variants[name] = fn

    def register_run(self, run_id: str, agent_fn: Callable, original_input: Any) -> None:
        self._agents[run_id] = (agent_fn, original_input)

    def resolve(self, run_id: str, variant: str | None = None) -> tuple[Callable, Any]:
        if run_id not in self._agents:
            raise KeyError(
                f"Run {run_id} has no registered agent function. Replay/analyze require an in-process agent. "
                f"Use the demo endpoint or register the agent that produced this run."
            )
        fn, original_input = self._agents[run_id]
        if variant and variant in self.variants:
            fn = self.variants[variant]
        return fn, original_input

    def _seed_demo(self) -> None:
        from agentcrash.sdk import CrashTracer
        from examples.demo_agent import DEMO_REQUEST, buggy_agent, fixed_agent

        self.register_variant("buggy", buggy_agent)
        self.register_variant("fixed", fixed_agent)
        # only seed if no demo run exists yet
        existing = [r for r in self.storage.list_runs() if r.get("agent") == "support"]
        if existing:
            for r in existing:
                self.register_run(r["id"], buggy_agent, r["metadata"].get("request", DEMO_REQUEST))
            return
        tracer = CrashTracer(self.storage, integration="demo", framework="agentcrash-demo")
        run_id = None
        try:
            with tracer.run("support", model="stub-decide-v1", project="demo",
                            metadata={"request": DEMO_REQUEST}) as run:
                run_id = run.run_id
                buggy_agent(DEMO_REQUEST, run)
        except Exception:  # noqa: BLE001
            pass
        if run_id:
            self.register_run(run_id, buggy_agent, DEMO_REQUEST)


def _interventions_from(specs: list[dict[str, Any]]) -> list[Intervention]:
    return [Intervention(id=f"iv-{i}", type=s.get("type", "inject_failure"),
                         fixture_key=s.get("fixture_key"), kind=s.get("kind"),
                         name=s.get("name"), spec=s.get("spec", {}))
            for i, s in enumerate(specs)]


def create_app(ctx: ServerContext) -> FastAPI:
    app = FastAPI(title="AgentCrash", version="0.1.0",
                  description="The open-source crash debugger and reliability lab for AI agents.")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "version": "0.1.0"}

    @app.get("/api/projects")
    def projects() -> list[dict[str, Any]]:
        return ctx.storage.list_projects()

    @app.get("/api/runs")
    def runs(project_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return ctx.storage.list_runs(project_id, limit)

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        r = ctx.storage.get_run(run_id)
        if not r:
            raise HTTPException(404, "run not found")
        return r

    @app.get("/api/runs/{run_id}/events")
    def get_events(run_id: str) -> list[dict[str, Any]]:
        return [e.model_dump() for e in ctx.storage.get_events(run_id)]

    @app.get("/api/runs/{run_id}/events/{event_id}")
    def get_event(run_id: str, event_id: str) -> dict[str, Any]:
        e = ctx.storage.get_event(run_id, event_id)
        if not e:
            raise HTTPException(404, "event not found")
        return e.model_dump()

    @app.get("/api/runs/{run_id}/replays")
    def list_replays(run_id: str) -> list[dict[str, Any]]:
        return ctx.storage.list_replays(run_id)

    @app.post("/api/runs/{run_id}/replay")
    def replay(run_id: str, req: ReplayRequest) -> dict[str, Any]:
        try:
            fn, original_input = ctx.resolve(run_id, req.agent)
        except KeyError as e:
            raise HTTPException(400, str(e)) from None
        cfg = ReplayConfig(mode=req.mode, live_kinds=set(req.live_kinds),
                           interventions=_interventions_from(req.interventions),
                           consent_live=req.consent_live)
        try:
            result = ctx.replayer.replay(run_id, fn, original_input, cfg)
        except PermissionError as e:
            raise HTTPException(403, str(e)) from None
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"replay failed: {e}") from None
        return {
            "replay_id": result.replay_id, "new_run_id": result.new_run_id,
            "status": result.status, "error": result.error,
            "agent_output": result.agent_output, "diff_lines": result.diff_lines,
            "diff": _diff_to_dict(result.diff),
        }

    @app.post("/api/runs/{run_id}/analyze")
    def analyze(run_id: str, req: AnalyzeRequest) -> dict[str, Any]:
        try:
            fn, original_input = ctx.resolve(run_id, req.agent)
        except KeyError as e:
            raise HTTPException(400, str(e)) from None
        report = ctx.analyzer.analyze(run_id, fn, original_input)
        return {
            "run_id": report.run_id, "failed": report.failed,
            "root_cause": report.root_cause, "confidence": report.confidence,
            "recommended_fix": report.recommended_fix,
            "suggested_invariant": report.suggested_invariant,
            "summary": report.summary,
            "candidates": [{"event_id": c.event_id, "name": c.name, "score": c.score,
                            "averted": c.averted,
                            "evidence": [{"description": e.description, "event_id": e.event_id,
                                          "averted": e.averted, "replay_run_id": e.replay_run_id}
                                         for e in c.evidence]} for c in report.candidates],
        }

    @app.post("/api/runs/{run_id}/test/generate")
    def test_generate(run_id: str, req: AnalyzeRequest) -> dict[str, Any]:
        try:
            fn, original_input = ctx.resolve(run_id, req.agent)
        except KeyError as e:
            raise HTTPException(400, str(e)) from None
        report = ctx.analyzer.analyze(run_id, fn, original_input)
        spec = generate_test(report, ctx.storage.get_run(run_id), original_input)
        tid = ctx.storage.save_test(spec.name, spec.to_dict(), run_id)
        return {"test_id": tid, "spec": spec.to_dict()}

    @app.get("/api/tests")
    def list_tests() -> list[dict[str, Any]]:
        return ctx.storage.list_tests()

    @app.get("/api/tests/{test_id}")
    def get_test(test_id: str) -> dict[str, Any]:
        t = ctx.storage.get_test(test_id)
        if not t:
            raise HTTPException(404, "test not found")
        return t

    @app.post("/api/tests/{test_id}/run")
    def test_run(test_id: str, req: TestRunRequest) -> dict[str, Any]:
        t = ctx.storage.get_test(test_id)
        if not t:
            raise HTTPException(404, "test not found")
        spec = TestSpec.from_dict(t["spec"])
        spec.interventions = _interventions_from(req.interventions or [])
        try:
            fn, _ = ctx.resolve(spec.source_run_id, req.agent)
        except KeyError as e:
            raise HTTPException(400, str(e)) from None
        result = run_test(spec, fn, ctx.replayer)
        ctx.storage.record_test_result(test_id, {
            "passed": result.passed, "status": result.status,
            "violations": result.violations, "run_id": result.run_id, "agent": req.agent,
        })
        return {
            "test_name": result.test_name, "passed": result.passed, "status": result.status,
            "violations": result.violations, "run_id": result.run_id,
            "events": [e.model_dump() for e in result.trace],
        }

    @app.post("/api/runs/{run_id}/chaos")
    def chaos(run_id: str, req: ChaosRequest) -> dict[str, Any]:
        try:
            fn, original_input = ctx.resolve(run_id, req.agent)
        except KeyError as e:
            raise HTTPException(400, str(e)) from None
        result = run_chaos(req.spec, fn, original_input, run_id, ctx.replayer)
        return {"name": result.name, "fault": result.fault, "passed": result.passed,
                "status": result.status, "run_id": result.run_id,
                "observations": result.observations, "violations": result.violations}

    @app.post("/api/demo/record")
    def demo_record() -> dict[str, Any]:
        ctx._seed_demo()
        runs = [r for r in ctx.storage.list_runs() if r.get("agent") == "support"]
        return {"seeded": True, "runs": runs}

    @app.get("/api/export/{run_id}")
    def export(run_id: str) -> dict[str, Any]:
        try:
            return ctx.storage.export_run(run_id)
        except KeyError:
            raise HTTPException(404, "run not found") from None

    # ----- serve built UI if present -----
    dist = Path(__file__).resolve().parent.parent / "apps" / "web" / "dist"
    if dist.exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(dist / "index.html")

        @app.get("/{path:path}")
        def spa(path: str) -> Any:
            full = dist / path
            if full.is_file():
                return FileResponse(full)
            return FileResponse(dist / "index.html")
    else:
        @app.get("/")
        def index_placeholder() -> JSONResponse:
            return JSONResponse({
                "name": "AgentCrash API",
                "message": "Web UI not built. Run `npm run build` in apps/web, or use the API at /api/*.",
                "quickstart": "agentcrash demo  |  agentcrash start  |  /api/runs",
            })

    return app


def _diff_to_dict(d: Any) -> dict[str, Any]:
    if d is None:
        return {}
    return {
        "tool_calls_a": d.tool_calls_a, "tool_calls_b": d.tool_calls_b,
        "added": d.added, "removed": d.removed, "reordered": d.reordered,
        "arg_changes": d.arg_changes, "result_changed": d.result_changed,
        "regressions": d.regressions, "lines": d.lines,
    }


def build_server(db_path: str | None = None, *, seed_demo: bool = True) -> tuple[FastAPI, ServerContext]:
    import sys

    db = db_path or os.path.join(os.getcwd(), ".agentcrash", "agentcrash.db")
    # examples/ must be importable before seeding the demo
    repo_root = str(Path(db).resolve().parent.parent)
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    storage = Storage(db)
    ctx = ServerContext(storage, seed_demo=seed_demo)
    app = create_app(ctx)
    return app, ctx


def build_app() -> FastAPI:
    """Uvicorn factory entry point: `uvicorn agentcrash.server:build_app --factory`."""
    app, _ctx = build_server()
    return app