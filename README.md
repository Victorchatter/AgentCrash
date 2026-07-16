# AgentCrash

**The open-source crash debugger and reliability lab for AI agents.**

> Your AI agent failed. AgentCrash tells you why — and proves what would have prevented it.

AI agents fail in ways traditional software does not. They make ambiguous
selections, misread tool output, retry into duplicate side effects, and behave
flakily across models and prompts. Developers still debug them as if they were
chatbots — reading logs, guessing, re-running by hand.

AgentCrash records what actually happened, replays the execution, tests
counterfactuals, identifies the likely root cause with cited evidence, and
turns the failure into a regression test.

```
RECORD → REPLAY → VISUALIZE → ANALYZE → INTERVENE → COMPARE COUNTERFACTUALS → IDENTIFY ROOT CAUSE → GENERATE REGRESSION TEST
```

It is **not** another agent framework. It is an observability, replay,
debugging, reliability, and testing layer that sits *under* any agent.

---

## Why it exists

| Question AgentCrash answers | How |
|---|---|
| What exactly happened during this run? | Structured, replayable trace timeline |
| Which decision caused the failure? | Causal analysis with counterfactual replays |
| Was it the model, prompt, tool, or environment? | Intervention-based root-cause isolation |
| Can the failure be reproduced? | Deterministic exact replay from a frozen fixture |
| Is the agent flaky? / did this change regress? | Behavioral diff + regression tests |
| What would have prevented it? | Counterfactual replay that averts the failure |

## Principles

- **Local-first.** Traces, replay, analysis, and tests run on your machine.
  No SaaS required. Sensitive agent data stays local; secrets are redacted at
  ingestion.
- **Framework-agnostic.** A normalized, versioned event model
  (`agentcrash.schema.v1`). Integrations map foreign agent events into it.
- **Replayability is real.** External calls (LLM, tools, MCP, HTTP, retrieval,
  shell, fs) are captured frozen and replayed — exact, selective, or live.
  Counterfactuals modify the frozen world and re-run the agent.
- **Explainability, not omniscience.** Root-cause reports cite trace event IDs
  and a confidence derived from evidence. An optional LLM summarizes evidence;
  it is never the sole source of truth.
- **Safe by default.** Replayed side-effecting calls never fire automatically.
  `SAFE` / `SIMULATED` / `LIVE` replay modes; `LIVE` requires explicit consent.

## Quick start

```bash
git clone <repo> agentcrash && cd agentcrash
pip install -e ".[dev]"

# Run the full demo (no API keys, fully offline):
agentcrash demo

# Launch the local web UI + API:
agentcrash start
# → http://127.0.0.1:8000

# Or expose AgentCrash to any MCP-aware host as a stdio MCP server:
agentcrash mcp
```

The demo runs a customer-support agent that refunds the **wrong customer**
after an ambiguous name search. AgentCrash records the failure, replays it,
runs a counterfactual ("what if the search had returned only the right
customer?") that **averts** the failure, identifies the root cause at 89%
confidence, and generates a regression test that the buggy agent fails and the
fixed agent passes.

```text
[1/6] RECORDED failing run …  (WrongCustomerError: refunded wrong customer …)
[2/6] EXACT REPLAY: status=failed, behaviorally identical=True
[3/6] ROOT CAUSE ANALYSIS
  Most likely cause: The agent selected the wrong record after an ambiguous
  search_customer result (event …, 2 candidates returned).
  Confidence: 89%
  Evidence:
    - [❌ reproduces] Replay with only search_customer#0 (CUST-001) -> failed
    - [✅ averts]      Replay with only search_customer#1 (CUST-002) -> completed
  Recommended fix: Require verify_customer before refund_order.
[4/6] GENERATED REGRESSION TEST: require_verify_customer_before_refund_order
[5/6] TEST vs buggy agent: PASSED=False
[5/6] TEST vs fixed agent: PASSED=True
```

## Use the SDK in your own agent

```python
from agentcrash.sdk import CrashTracer

tracer = CrashTracer()  # local SQLite at ./.agentcrash/agentcrash.db

with tracer.run("my-agent", model="gpt-4o") as run:
    results = run.tool("search", {"q": "john"}, lambda: search("john"))
    answer  = run.llm({"prompt": "..."},  lambda: model.generate(...))
    run.decision("refund", {"order_id": results[0]["order_id"]})
    run.tool("refund_order", {"order_id": results[0]["order_id"]},
             lambda: refund(results[0]["order_id"]))
```

Every `run.tool` / `run.llm` call is captured frozen and is automatically
replayable. An agent authored against the SDK is replayable as-is — no extra
wiring.

## Use as an MCP server (let agents debug themselves)

`agentcrash mcp` runs AgentCrash as a **stdio MCP server** — the same engine
as the web UI, spoken as JSON-RPC 2.0 over stdin/stdout. Any MCP-aware host
(Claude Desktop, IDEs, coding agents) can search traces, replay failures,
analyze root causes, and mint regression tests from its own tool loop. No
extra dependency; reuse in an `mcp` client config:

```json
{
  "mcpServers": {
    "agentcrash": { "command": "agentcrash", "args": ["mcp"] }
  }
}
```

Tools exposed: `trace_search`, `trace_get`, `replay_run`, `analyze_failure`,
`test_generate`. Tool-execution failures return `isError: true` (not a
JSON-RPC error), so the host can reason about retry. See
[`docs/research/mcp.md`](docs/research/mcp.md) §9 for the design.

## Replay & counterfactuals

```python
from agentcrash.replay import Replayer, ReplayConfig
from agentcrash.interventions import Intervention

replayer = Replayer(storage)

# Deterministic reproduction — every external response frozen.
replayer.replay(run_id, my_agent, original_input, ReplayConfig(mode="exact"))

# Counterfactual: "what if search had returned only CUST-002?"
replayer.replay(run_id, my_agent, original_input, ReplayConfig(
    mode="selective",
    interventions=[Intervention(id="cf", type="replace_tool_response",
                                fixture_key=..., spec={"response": [cust2]})],
))
```

Modes:
- **exact** — all external responses frozen. Deterministic reproduction.
- **selective** — frozen responses for calls in the fixture; new (divergent)
  calls run live against the *simulated* environment (pure kinds only). This is
  how counterfactuals work offline: change a frozen tool result, re-run the LLM
  decision, observe the new outcome.
- **live** — everything runs for real. Dangerous; requires `consent_live=True`.

Intervention types: `replace_tool_response`, `replace_llm_output`,
`modify_tool_response`, `inject_failure`, `inject_timeout`, `remove_tool`,
`replace_model`, `modify_prompt` (last two are live-only).

## Architecture

```
Agent (any framework)
   │  via SDK or integration adapter
   ▼
Normalized AgentCrash Event Model  (agentcrash.schema.v1)
   │
   ▼
Collector → Redaction → SQLite storage (+ on-disk artifacts)
   │
   ▼
FastAPI server ── REST ──► React/TS web UI (trace timeline, event inspector,
   │                         replay workspace, failure report, test suite)
   ▼
Replay engine ── Counterfactual interventions ──► Behavioral diff
   │
   ▼
Causal analyzer (evidence + cited event IDs + confidence)
   │
   ▼
Regression test generation ──► Reproducible invariant tests
   │
   ▼
Chaos engine (fault injection against frozen fixtures)
```

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and
[`docs/research/SYNTHESIS.md`](docs/research/SYNTHESIS.md).

## Repository layout

```
agentcrash/            # Python package: schema, storage, SDK, replay, diff,
                       #   analyzer, interventions, chaos, tests_gen, server,
                       #   mcp_server (stdio MCP), CLI
apps/web/              # React + TypeScript + Vite web UI
examples/demo_agent.py # the intentionally-failing demo agent (offline)
docs/research/         # ecosystem, competitors, integrations, OTel, MCP, …
tests/                 # unit + integration + e2e tests
```

## Integrations (framework adapters)

AgentCrash is framework-agnostic. Integrations map foreign agent events into
the canonical schema. The first integrations planned from the ecosystem
research (see `docs/research/integrations.md`):

- **generic Python / TypeScript SDKs** — instrument any agent in-process
- **OpenTelemetry** — ingest OTel GenAI spans via a compatibility adapter
- **MCP** ✅ — AgentCrash runs *as* a stdio MCP server (`agentcrash mcp`):
  `trace_search`, `trace_get`, `replay_run`, `analyze_failure`,
  `test_generate`. Client-side instrumentation of MCP traffic is on the roadmap.
- **OpenAI Agents SDK, Anthropic Claude Agent SDK, LangGraph, PydanticAI,
  smolagents** — via each framework's callback/hook surface
- **Claude Code, Codex CLI, Aider, OpenHands** — coding-agent actions via
  subprocess wrapping / hooks / log+filesystem watching

Each integration lives in `agentcrash/integrations/<name>` with an adapter,
tests, docs, and a compatibility matrix. See
[`CONTRIBUTING.md`](CONTRIBUTING.md) to add one.

## Status & roadmap

AgentCrash is **Alpha**. The core vertical slice (record → view → replay →
analyze → test) is real and tested. In progress:

- **Phase 0** ✅ Ecosystem + architecture research (`docs/research/`)
- **Phase 1** ✅ Schema, storage, SDK, collector, redaction, server, CLI
- **Phase 2** ✅ First vertical slice + demo + web UI
- **Phase 3** 🚧 Universal integration layer — MCP server ✅ shipped; generic
  SDKs, OTel ingestion, and MCP client-side instrumentation next
- **Phase 4** 🚧 Major framework + coding-agent integrations
- **Phase 5** 🚧 Causal analysis v2 (multi-intervention, ranking)
- **Phase 6** ⏳ Regression test suite + CI runner
- **Phase 7** ⏳ Chaos engine v2 (reliability scoring across fault classes)

## Security

Traces may contain source code, API keys, customer data, and proprietary
prompts. AgentCrash:

- Stores everything locally by default.
- Redacts secrets (API keys, bearer tokens, env assignments, high-entropy
  blobs) at ingestion.
- Treats all tool/shell/model/MCP/file/HTTP outputs as **untrusted**.
- Never auto-executes replayed side-effecting calls. `LIVE` replay requires
  explicit consent.
- Supports safe export/import and configurable retention.

See [`SECURITY.md`](SECURITY.md) and [`docs/research/security.md`](docs/research/security.md).

## Contributing

PRs welcome. Read [`CONTRIBUTING.md`](CONTRIBUTING.md). The cleanest
contribution is a new integration adapter under `agentcrash/integrations/`.

## License

Apache-2.0. See [`LICENSE`](LICENSE).