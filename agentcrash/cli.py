"""AgentCrash CLI.

Implemented commands work end-to-end against the local SQLite store. Commands
that require the agent function that produced a run (replay/analyze/test) work
for demo runs (the demo agents are imported); arbitrary foreign runs require an
in-process agent registration and print a clear note otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentcrash.storage import Storage

DEFAULT_DB = os.path.join(os.getcwd(), ".agentcrash", "agentcrash.db")


def _storage(args) -> Storage:
    from agentcrash.storage import Storage

    return Storage(args.db or DEFAULT_DB)


def _resolve_demo_agent(storage, run_id: str):
    """Return (agent_fn, original_input) for a demo run, else raise."""
    from examples.demo_agent import DEMO_REQUEST, buggy_agent

    run = storage.get_run(run_id)
    if not run:
        raise SystemExit(f"run {run_id} not found")
    if run.get("agent") != "support":
        raise SystemExit(
            f"run {run_id} was produced by '{run.get('agent')}', not the demo agent. "
            f"CLI replay/analyze/test currently support demo runs only; use the web API "
            f"with a registered agent for other runs."
        )
    req = run.get("metadata", {}).get("request", DEMO_REQUEST)
    return buggy_agent, req


def cmd_init(args) -> None:
    os.makedirs(os.path.dirname(DEFAULT_DB), exist_ok=True)
    from agentcrash.storage import Storage

    Storage(DEFAULT_DB)
    print(f"Initialized AgentCrash at {DEFAULT_DB}")
    print("Next: `agentcrash demo` to record a failing demo run, or `agentcrash start` for the web UI.")


def cmd_start(args) -> None:
    import uvicorn

    host, port = args.host, args.port
    print(f"Starting AgentCrash at http://{host}:{port}  (db={args.db or DEFAULT_DB})")
    uvicorn.run("agentcrash.server:build_app", host=host, port=port, factory=True,
                reload=False, log_level=args.log_level)


def cmd_mcp(args) -> None:
    """Run AgentCrash as a stdio MCP server (tools: trace_search, trace_get,
    replay_run, analyze_failure, test_generate). Stdin/stdout speak JSON-RPC;
    keep server logs on stderr so they don't corrupt the protocol stream."""
    from agentcrash.mcp_server import serve
    from agentcrash.server import build_server

    app, ctx = build_server(args.db or DEFAULT_DB)
    _ = app  # web app unneeded for the stdio server; ctx is what we use
    serve(ctx)


def cmd_demo(args) -> None:
    from agentcrash.analyzer import Analyzer
    from agentcrash.replay import ReplayConfig, Replayer
    from agentcrash.sdk import CrashTracer
    from agentcrash.storage import Storage
    from agentcrash.tests_gen import generate_test, run_test
    from examples.demo_agent import DEMO_REQUEST, buggy_agent, fixed_agent

    storage = Storage(args.db or DEFAULT_DB)
    tracer = CrashTracer(storage, integration="demo", framework="agentcrash-demo")
    run_id = None
    try:
        with tracer.run("support", model="stub-decide-v1", project="demo",
                        metadata={"request": DEMO_REQUEST}) as run:
            run_id = run.run_id
            buggy_agent(DEMO_REQUEST, run)
    except Exception as e:  # noqa: BLE001
        print(f"\n[1/6] RECORDED failing run {run_id}  ({type(e).__name__}: {e})")
    else:
        print(f"\n[1/6] RECORDED run {run_id} (did not fail — unexpected)")

    replayer = Replayer(storage)
    exact = replayer.replay(run_id, buggy_agent, DEMO_REQUEST, ReplayConfig(mode="exact"))
    print(f"[2/6] EXACT REPLAY: status={exact.status}, behaviorally identical={not exact.diff.is_different}")

    analyzer = Analyzer(storage, replayer)
    report = analyzer.analyze(run_id, buggy_agent, DEMO_REQUEST)
    print("\n[3/6] ROOT CAUSE ANALYSIS")
    print("\n".join(report.summary))

    test = generate_test(report, storage.get_run(run_id), DEMO_REQUEST)
    storage.save_test(test.name, test.to_dict(), run_id)
    print(f"\n[4/6] GENERATED REGRESSION TEST: {test.name}")

    br = run_test(test, buggy_agent, replayer)
    print(f"[5/6] TEST vs buggy agent:  PASSED={br.passed}  violations={br.violations}")
    fr = run_test(test, fixed_agent, replayer)
    print(f"[5/6] TEST vs fixed agent:  PASSED={fr.passed}  violations={fr.violations}")

    print("\n[6/6] Open the web UI: `agentcrash start` then visit http://127.0.0.1:8000")
    print(f"      Inspect this run: `agentcrash inspect {run_id}`")


def cmd_status(args) -> None:
    storage = _storage(args)
    runs = storage.list_runs()
    projects = storage.list_projects()
    print(f"db: {storage.path}")
    print(f"projects: {len(projects)}  runs: {len(runs)}")
    for r in runs[:5]:
        print(f"  {r['id'][:12]}  {r['status']:<10}  agent={r.get('agent')}  "
              f"tool_calls={r.get('tool_calls')}  error={r.get('error') or '-'}")


def cmd_runs(args) -> None:
    storage = _storage(args)
    for r in storage.list_runs(limit=args.limit):
        print(f"{r['id'][:12]}  {r['status']:<10}  {r.get('agent') or '-':<16}  "
              f"calls={r.get('tool_calls', 0)}  retries={r.get('retries', 0)}  "
              f"{(r.get('error') or '-')[:60]}")


def cmd_inspect(args) -> None:
    storage = _storage(args)
    run = storage.get_run(args.run_id)
    if not run:
        raise SystemExit("run not found")
    print(f"RUN {run['id']}  status={run['status']}  agent={run.get('agent')}  model={run.get('model')}")
    print(f"duration={run.get('duration_ms')}ms  tool_calls={run.get('tool_calls')}  "
          f"retries={run.get('retries')}  error={run.get('error')}")
    if run.get("root_cause"):
        print(f"root_cause: {run['root_cause']}")
    print("\nevents:")
    for e in storage.get_events(args.run_id):
        extra = ""
        if e.is_error and e.error:
            extra = f"  ERR: {e.error.message}"
        elif e.output is not None:
            extra = f"  out={_short(e.output)}"
        print(f"  #{e.seq:>2} {e.type:<22} {e.name or '':<18} {e.status:<10}{extra}")


def cmd_replay(args) -> None:
    from agentcrash.replay import ReplayConfig, Replayer

    storage = _storage(args)
    fn, req = _resolve_demo_agent(storage, args.run_id)
    cfg = ReplayConfig(mode=args.mode, consent_live=args.consent_live)
    result = Replayer(storage).replay(args.run_id, fn, req, cfg)
    print(f"replay status={result.status}  new_run={result.new_run_id}")
    print("\n".join(result.diff_lines))


def cmd_analyze(args) -> None:
    from agentcrash.analyzer import Analyzer
    from agentcrash.replay import Replayer

    storage = _storage(args)
    fn, req = _resolve_demo_agent(storage, args.run_id)
    report = Analyzer(storage, Replayer(storage)).analyze(args.run_id, fn, req)
    print("\n".join(report.summary))


def cmd_test_generate(args) -> None:
    from agentcrash.analyzer import Analyzer
    from agentcrash.replay import Replayer
    from agentcrash.tests_gen import generate_test

    storage = _storage(args)
    fn, req = _resolve_demo_agent(storage, args.run_id)
    report = Analyzer(storage, Replayer(storage)).analyze(args.run_id, fn, req)
    spec = generate_test(report, storage.get_run(args.run_id), req)
    tid = storage.save_test(spec.name, spec.to_dict(), args.run_id)
    print(f"saved test {tid}  name={spec.name}")
    print(json.dumps(spec.to_dict(), indent=2, default=str))


def cmd_test_run(args) -> None:
    from agentcrash.replay import Replayer
    from agentcrash.tests_gen import TestSpec, run_test

    storage = _storage(args)
    t = storage.get_test(args.test_id)
    if not t:
        raise SystemExit("test not found")
    fn, _ = _resolve_demo_agent(storage, t["spec"].get("source_run_id") or "")
    spec = TestSpec.from_dict(t["spec"])
    result = run_test(spec, fn, Replayer(storage))
    storage.record_test_result(args.test_id, {
        "passed": result.passed, "status": result.status,
        "violations": result.violations, "run_id": result.run_id, "agent": args.agent})
    print(f"test={result.test_name}  agent={args.agent}  PASSED={result.passed}  status={result.status}")
    for v in result.violations:
        print(f"  - {v}")


def cmd_export(args) -> None:
    storage = _storage(args)
    bundle = storage.export_run(args.run_id)
    print(json.dumps(bundle, indent=2, default=str))


def cmd_import(args) -> None:
    storage = _storage(args)
    bundle = json.loads(Path(args.file).read_text(encoding="utf-8"))
    rid = storage.import_run(bundle)
    print(f"imported run {rid}")


def cmd_chaos(args) -> None:
    from agentcrash.chaos import run_chaos
    from agentcrash.replay import Replayer

    storage = _storage(args)
    fn, req = _resolve_demo_agent(storage, args.run_id)
    spec = {"name": "refund_timeout_recovery", "target": {"kind": "tool", "name": "refund_order"},
            "fault": {"type": "inject_timeout", "ms": 30000},
            "expected": {"must_recover": True, "avoid_duplicate_side_effects": True, "max_retries": 2}}
    result = run_chaos(spec, fn, req, args.run_id, Replayer(storage))
    print(f"chaos={result.name}  fault={result.fault}  PASSED={result.passed}  status={result.status}")
    for o in result.observations:
        print(f"  obs: {o}")
    for v in result.violations:
        print(f"  VIOLATION: {v}")


def _short(v) -> str:
    s = str(v)
    return s if len(s) <= 80 else s[:77] + "..."


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agentcrash",
                                description="The open-source crash debugger and reliability lab for AI agents.")
    p.add_argument("--db", default=None, help="path to the AgentCrash SQLite database")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="initialize a local AgentCrash store").set_defaults(func=cmd_init)

    s = sub.add_parser("start", help="start the local web UI + API server")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--log-level", default="info")
    s.set_defaults(func=cmd_start)

    m = sub.add_parser("mcp", help="run AgentCrash as a stdio MCP server")
    m.set_defaults(func=cmd_mcp)

    sub.add_parser("demo", help="run the full RECORD→REPLAY→ANALYZE→TEST demo").set_defaults(func=cmd_demo)
    sub.add_parser("status", help="show store summary").set_defaults(func=cmd_status)

    r = sub.add_parser("runs", help="list runs")
    r.add_argument("--limit", type=int, default=50)
    r.set_defaults(func=cmd_runs)

    i = sub.add_parser("inspect", help="inspect a run and its events")
    i.add_argument("run_id")
    i.set_defaults(func=cmd_inspect)

    rp = sub.add_parser("replay", help="replay a run (exact|selective|live)")
    rp.add_argument("run_id")
    rp.add_argument("--mode", default="exact", choices=["exact", "selective", "live"])
    rp.add_argument("--consent-live", action="store_true")
    rp.set_defaults(func=cmd_replay)

    a = sub.add_parser("analyze", help="analyze a failed run")
    a.add_argument("run_id")
    a.set_defaults(func=cmd_analyze)

    tg = sub.add_parser("test-generate", help="generate a regression test from a failed run")
    tg.add_argument("run_id")
    tg.set_defaults(func=cmd_test_generate)

    tr = sub.add_parser("test-run", help="run a saved regression test")
    tr.add_argument("test_id")
    tr.add_argument("--agent", default="buggy", choices=["buggy", "fixed"])
    tr.set_defaults(func=cmd_test_run)

    ch = sub.add_parser("chaos", help="run a chaos fault-injection test on a run")
    ch.add_argument("run_id")
    ch.set_defaults(func=cmd_chaos)

    e = sub.add_parser("export", help="export a run as a portable JSON bundle")
    e.add_argument("run_id")
    e.set_defaults(func=cmd_export)

    imp = sub.add_parser("import", help="import a run bundle")
    imp.add_argument("file")
    imp.set_defaults(func=cmd_import)

    return p


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to a legacy code page (cp1252); the demo and
    # analyzer reports print emoji (✅/❌). Reconfigure stdout to UTF-8 so
    # encoding never raises. Renders as well as the host terminal allows.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    # ensure repo root (for examples/) is importable when using the demo
    cwd = os.getcwd()
    if cwd not in sys.path:
        sys.path.insert(0, cwd)
    args = build_parser().parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())