# AgentCrash Competitive Landscape & Gap Analysis

**Date:** 2026-07-15
**Scope:** Observability / eval / replay / reliability tools that overlap with AgentCrash
**AgentCrash positioning:** open-source, local-first, framework-agnostic agent debug + replay + reliability platform (record -> replay -> analyze -> intervene -> fix) with counterfactual interventions, behavioral diffing, causal failure analysis, chaos/fault injection, and regression-test generation from failures. Coding-agent aware.

---

## 1. Executive summary

The LLM-agent tooling ecosystem in 2026 splits into four crowded clusters:

1. **Trace + eval SaaS with self-host options** (Langfuse, LangSmith, Arize Phoenix, Helicone, Braintrust, Agentops, PostHog, MLflow, Datadog, Honeycomb, Sentry, OpenLLMetry). These own *capture and dashboards*. Most are OTel-based and increasingly agent-aware. Replay is shallow (span re-execution in a playground) or absent. **None do counterfactual forking, causal blame, chaos injection, or auto-regression-test generation.**
2. **Eval/regression frameworks** (DeepEval, Promptfoo, Ragas, Inspect AI). These own *offline test authoring and CI gates*. Local-first and open-source, but **none record from production, none replay, none do causal analysis, none generate tests from failures** — you write the tests by hand.
3. **App-builder platforms** (Dify). Adjacent, not competitive — they build agents, not debug them.
4. **Emerging 2026 replay+causal micro-tools** (tracefork, culpa, causal-agent-replay, counterfact, causal-agent-tracer). **This is the real competitive threat.** These projects (all created Mar-Jul 2026) directly target AgentCrash's proposed unique value: replay + counterfactual + causal blame + fault injection. `tracefork` in particular matches all five capabilities AgentCrash claims.

**The headline:** AgentCrash's "replay + counterfactual + causal + chaos + regression-test" bundle is **no longer uncontested**. The defensible whitespace has narrowed to: (a) one integrated local-first platform with a canonical event model + SQLite + FastAPI/CLI, (b) framework-agnostic breadth vs. these tools' SDK-monkey-patching/LangGraph-only approaches, (c) coding-agent awareness (Claude Code/Cursor), and (d) **regression-test generation from failure analysis** — which none of the emerging tools do. The causal/replay/chaos pieces alone are not a moat.

---

## 2. Comparison matrix

Legend: Yes / Partial / No / SaaS-only

| Tool | License | Local-first? | Captures | Replay? | Counterfactual / causal? | Chaos / fault injection? | Regression test gen? | Coding-agent aware? |
|---|---|---|---|---|---|---|---|---|
| **Langfuse** | MIT (core) + commercial EE | Yes (Docker, same codebase as cloud) | LLM spans, prompts, scores, datasets, multimodal | Partial (playground span re-run; full replay via 3rd-party `Rewind`) | No | No | No (datasets are manual) | Partial (integrations, not first-class) |
| **LangSmith** | Proprietary; self-host = Enterprise add-on | Partial (Enterprise-only self-host; EKS) | Traces, feedback, datasets, annotations | Partial (promote prod traces to datasets, replay vs new agent versions) | No | No | No (manual datasets) | Partial |
| **Arize Phoenix** | Elastic License 2.0 (NOT Apache; source-available) | Yes (pip/Docker, all features, air-gap OK) | OTel spans, evals, datasets, experiments | Partial (Span Replay in playground) | No | No | No | No |
| **Helicone** | Apache 2.0 | Yes (Docker all-in-one) | LLM req/resp via proxy, cost, agent sessions | Partial (playground replay requests) | No | No | No | No |
| **Braintrust** | SDKs Apache 2.0; **platform server proprietary** | Partial (data plane via Terraform on AWS/GCP/Azure; control plane = Braintrust-hosted) | Experiments, traces, datasets, scores | Partial (playground promote to experiment) | No | No | Partial (regression *detection* vs baseline, not *generation*) | No |
| **Agentops** | SDK MIT; app ELv2 | Yes (Docker Compose; needs Supabase+ClickHouse) | Agent sessions, spans, cost, decorators | Partial (session replay = execution graph viewer, not re-execution) | No | No | No (scorecards on roadmap) | Partial (CrewAI/AutoGen/OpenAI Agents) |
| **PostHog LLM Obs** | MIT (SDK `@posthog/ai`); platform custom license | Yes (self-hostable) | gen_ai.* spans, traces, sessions, sentiment | No (session replay is for web UX, not LLM re-exec) | No | No | No | Partial (Claude Agent SDK integration) |
| **OpenLLMetry / Traceloop** | Apache 2.0 | Yes (SDK in-process; backend = any OTel sink) | OTel gen_ai.* spans, vector DB, frameworks | No (instrumentation only; no backend) | No | No | No | Partial (MCP protocol instrumentation) |
| **Sentry AI** | Proprietary (BSL-style); SDKs open | No (SaaS; some self-host tiers) | gen_ai spans, agent traces, tool calls, MCP | Partial (Conversations = chat replay view, beta; not re-exec) | No | No | No | Partial (MCP server monitoring) |
| **Datadog LLM/Agent Obs** | Proprietary | No (SaaS only) | Traces, evals, datasets, patterns, insights | Partial (datasets from prod traces; experiments side-by-side) | No | No | No | Partial (Strands, CrewAI, Pydantic) |
| **Honeycomb** | Proprietary | Partial (self-host tiers) | OTel spans, BubbleUp outlier analysis | No | No (BubbleUp = correlation, not causal) | No | No | Partial (Claude Code docs) |
| **Dify** | Dify OSS License (Apache 2.0 + extra conditions) | Yes (Docker Compose) | App logs, workflow runs, agent reasoning traces | Partial (run history, single-step node debug) | No | No | No | No |
| **Ragas** | Apache 2.0 | Yes (Python lib, local/CSV backend) | RAG eval metrics, synthetic test data | No | No | No | Partial (synthetic dataset gen, not from prod failures) | No |
| **DeepEval** | Apache 2.0 | Yes (pytest-native, local; Ollama for offline judge) | 50+ metrics, traces, MCP metrics | No | No | No | Partial (regression tracking via Confident AI; tests are hand-authored) | Partial (MCP metrics, Claude Code MCP) |
| **Promptfoo** | MIT (now OpenAI-owned, stays MIT) | Yes (100% local) | Eval results, red-team vulns | Partial (`--resume`, `retry`, `--filter-failing` re-runs) | No | Partial (red teaming = adversarial test gen, not runtime chaos) | Partial (red-team test gen, not from prod failures) | Partial (MCP server, code scans) |
| **Inspect AI (UK AISI)** | MIT | Yes (Python lib) | Eval tasks, solvers, scorers, agent traces, checkpoints | Partial (checkpointing + intervention for long-running agents) | No | No | No (200+ pre-built evals, not generated) | Partial (agent bridge, sandboxes) |
| **MLflow** | Apache 2.0 (Linux Foundation) | Yes (self-host tracking server + gateway) | OTel-compatible traces, evals, 50+ metrics, gateway usage | Partial (trace drill-down; eval on real traffic) | No | No | Partial (regression detection via evals) | Partial (route Claude Code via gateway) |
| **LlamaTrace** | Commercial hosted (built on Phoenix) | No (hosted only; self-host = use Phoenix directly) | LlamaIndex traces, evals | Partial (trace viewing; Phoenix Span Replay underneath) | No | No | No | No |

### The emerging 2026 replay+causal cluster (direct threat)

| Tool | License | Local-first? | Captures | Replay? | Counterfactual / causal? | Chaos / fault injection? | Regression test gen? | Coding-agent aware? |
|---|---|---|---|---|---|---|---|---|
| **tracefork** | MIT | Yes (in-process, 672 offline tests, no API key) | Content-addressed tape, bit-exact replay | **Yes** (hash-verified, $0 via stubs) | **Yes** (Wilson-CI flip-rate blame; temporal-Shapley) | **Yes** (`validate` injects 5 fault types; `chaos_release_order` reorders async) | **No** (validates blame, does not emit tests) | Partial (Claude Code/Cursor via proxy) |
| **culpa** | MIT | Yes (SDK monkey-patch or transparent proxy) | Flight-recorder tape | **Yes** (deterministic, stubbed responses) | **Yes** (counterfactual forking at any decision point) | No | No | **Yes** (Claude Code/Cursor proxy) |
| **causal-agent-replay** | Apache 2.0 | Yes (in-process) | Agent run as structural causal model | Partial (counterfactual replay) | **Yes** (Pearl do-calculus: do_resample/do_action/do_context/do_policy) | No | No | No (LangGraph/CrewAI/OpenAI Agents) |
| **counterfact** | Apache 2.0 | Yes (LangGraph replacement) | Multi-agent pipeline traces | Partial (ablation re-runs) | **Yes** (counterfactual ablation + Shapley) | No | Partial (ground-truth-free evals + failure classification) | Partial (Claude Agent Skill for debugging) |
| **causal-agent-tracer** | MIT | Yes (in-process) | Agent traces, Cladder-style causal benchmark | Partial | **Yes** (per-step counterfactual attribution) | No | Partial (A/B harness + promotion gate for CI) | No |

---

## 3. Per-tool detail

### Trace + eval platforms

**Langfuse** (MIT core + commercial EE; ~31k stars). Self-host runs the exact cloud codebase (Postgres + ClickHouse + Redis + S3), air-gap capable. Captures LLM spans, prompts, evals, datasets, LLM-as-judge, multimodal. No native replay; the third-party `Rewind` tool (`rewind import from-langfuse`) adds fork-from-step replay, diff timelines, and LLM-judge scoring, then exports back via OTLP. No counterfactual, causal, chaos, or test-gen. **Implication:** Langfuse is the likely *export target* and *upstream trace source* for AgentCrash, not a competitor on the reliability axis.

**LangSmith** (proprietary; self-host is an Enterprise add-on on EKS; ClickHouse + Postgres + Redis). Trace replay = promote prod traces to datasets and replay vs new agent versions. LangSmith Engine clusters failures and recommends fixes ($1.50/LCU). No counterfactual/chaos/test-gen. **Implication:** closed, expensive, enterprise-only self-host — leaves the local-first indie/dev segment entirely open.

**Arize Phoenix** (Elastic License 2.0 — NOT Apache, NOT OSI-approved; source-available, restricts managed-service resale). `pip install arize-phoenix`, all features unlocked self-hosted, OTel-native (OTLP gRPC on 4317, UI on 6006). Span Replay in the playground re-runs a traced LLM call with different prompts/models. PXI = built-in AI agent for debugging. **Implication:** strongest free-local observability baseline; AgentCrash should interop via OTLP and differentiate on replay-fork + causal + chaos, not on tracing. Note the ELv2 license is a subtle adoption blocker for some enterprises vs. AgentCrash's MIT/Apache opportunity.

**Helicone** (Apache 2.0; ~5.9k stars). All-in-one Docker container, but self-host supports only OpenAI + Anthropic providers (Bedrock/Vertex/Azure are cloud-only). Proxy-based capture (<50ms overhead), AI gateway, caching, rate limiting, prompt A/B. Replay = playground request replay. No causal/chaos/test-gen. **Implication:** proxy-based capture is a complementary pattern; AgentCrash's in-process SDK + canonical event model is broader.

**Braintrust** (SDKs Apache 2.0; **server is proprietary**). Data plane via Terraform (AWS/GCP/Azure), control plane hosted by Braintrust. Strong experiment comparison with explicit improvement/regression counts and diffs, CI/CD integration, online scoring of prod traces. Regression *detection* yes; regression *test generation* no. **Implication:** Braintrust's experiment-diff UX is the bar to beat for AgentCrash's behavioral diffing feature.

**Agentops** (SDK MIT; app ELv2; ~5.6k stars). Docker Compose self-host (needs Supabase + ClickHouse). Auto-instrumentation + decorators (`@session`, `@agent`, `@tool`, `@workflow`). Session replay = execution-graph viewer (visualization, not re-execution). Roadmap includes infinite-loop detection, token-overflow flags, honeypot detection. **Implication:** closest to "agent-aware observability" in the SaaS cluster, but no real replay/causal/chaos.

**PostHog LLM Obs** (MIT SDK; platform custom license). gen_ai.* OTel-native, trace waterfall, AI search over traces, sentiment. "Session replay" is PostHog's web-UX replay product, not LLM re-execution. **Implication:** broad product analytics integration; not a reliability tool.

**OpenLLMetry / Traceloop** (Apache 2.0; ~7.3k stars). Pure instrumentation — OTel gen_ai.* spans exported to 25+ backends. Traceloop leads the OTel GenAI semantic-convention working group. No backend, no replay, no causal. **Implication:** this is the *instrumentation layer AgentCrash should build on top of*, not compete with. Consuming OpenLLMetry spans and mapping them into AgentCrash's canonical event model is the right integration.

**Sentry AI** (proprietary; free tier includes AI monitoring). End-to-end agent traces stitched across LLM/tool/handoff spans; Conversations (beta) = chat replay view; MCP server monitoring. No re-exec, no causal, no chaos. **Implication:** the full-stack error-tracking integration (agent failure -> slow DB query root cause) is a UX pattern worth borrowing.

**Datadog Agent Observability** (proprietary; SaaS only). Trace-level + span-level evals, datasets from prod traces (3-year retention), Insights anomaly surfacing, Patterns topic clustering. No counterfactual/chaos/test-gen. **Implication:** the enterprise SaaS incumbent — AgentCrash cannot compete on scale/breadth, only on the local-first + reliability-lab axis Datadog will not touch.

**Honeycomb** (proprietary; self-host tiers). BubbleUp = ML outlier correlation across hundreds of dimensions (correlation, not causation). OTel GenAI v1.40.0 conventions; docs cover Claude Code instrumentation via transform processor. **Implication:** BubbleUp is the high bar for "why is this trace different?" analysis; AgentCrash's causal blame should be positioned as *causal* where BubbleUp is *correlational*.

**MLflow** (Apache 2.0, Linux Foundation; 20k+ stars, 30M+ monthly downloads). AI Gateway (unified OpenAI-compatible proxy, credential mgmt, traffic splitting, fallbacks, guardrails, MCP server access mgmt) + OTel-compatible tracing + 50+ eval metrics. Traces feed evals on real traffic. 2026 blogs show Claude Code routed through the gateway. **Implication:** MLflow is the most credible open-source full-stack competitor on the *gateway+tracing+eval* axis, but has **zero replay-fork/counterfactual/chaos/test-gen**. AgentCrash should interop with MLflow traces, not duplicate the gateway.

### Eval / regression frameworks

**DeepEval** (Apache 2.0; ~16.8k stars). Pytest-native, local-first, Ollama for offline LLM-judge. 50+ metrics including agentic (Task Completion, Tool Correctness, Goal Accuracy, Step Efficiency, Plan Adherence) and MCP-specific (MCP Task Completion, MCP Use). Regression tracking via Confident AI (cloud). Tests are **hand-authored** — no generation from prod failures. **Implication:** AgentCrash's regression-test-from-failure output should be DeepEval-compatible (emit pytest cases) so users can drop them into existing DeepEval suites. Strong integration opportunity.

**Promptfoo** (MIT; ~22.9k stars; now OpenAI-owned, stays MIT). Declarative YAML configs, `promptfoo eval`, red teaming, `--filter-failing`/`retry`/`--resume` for re-running failures, CI/CD integration across all major CI systems, MCP server with 14 tools, code scanning. Runs 100% locally. **Implication:** Promptfoo owns the CI-gate UX. AgentCrash should generate Promptfoo-format test cases from failure analysis (output `promptfooconfig.yaml` fragments) rather than build a competing CI runner. Red teaming is adversarial-test generation, not runtime chaos — distinct from AgentCrash's chaos injection.

**Ragas** (Apache 2.0; ~14.8k stars; now VibrantLabs). RAG-specific metrics, synthetic test-data generation, local/CSV backend. Narrow RAG focus. **Implication:** adjacent; AgentCrash's RAG evals could delegate to Ragas metrics.

**Inspect AI** (MIT; UK AISI; ~2.2k stars). Evaluation framework with 200+ pre-built evals, ReAct/Deep/Multi-agent, agent bridge to OpenAI Agents/LangChain/Pydantic, checkpointing + intervention for long-running agents, sandboxes, MCP tools, scanners. **Implication:** the checkpointing+intervention capability is conceptually adjacent to AgentCrash's counterfactual forking, but Inspect is an *eval authoring* framework, not a prod-replay/chaos tool. AgentCrash could export Inspect-style eval tasks. Government-backed credibility is a signal that "agent reliability" is a real category.

### App-builder (adjacent)

**Dify** (Dify OSS License = Apache 2.0 + extra conditions; ~148k stars). Visual workflow builder, agent strategies, 50+ tools, RAG pipeline, Prompt IDE, MCP support. Observability via Langfuse/Phoenix/Opik integrations. Run history + single-step node debug. **Implication:** not a competitor; a potential integration target — Dify agents could emit AgentCrash canonical events.

### The emerging 2026 replay+causal cluster (CRITICAL)

This cluster did not exist a few months ago and is the most important finding for AgentCrash's strategy. All five projects appeared Mar-Jul 2026.

**tracefork** (MIT; PyPI v0.2.1, released 2026-07-02). The most direct match to AgentCrash's full pitch: content-addressed tape with bit-exact hash-verified replay ($0 via stubbed responses); counterfactual forking (swap any step's response, re-run forward); causal blame with Wilson-score confidence intervals on flip-rates; **fault-injection validation** (`tracefork validate` plants 5 fault types — corrupted tool output, misleading retrieval, wrong system prompt, dropped message, poisoned argument — and confirms top-1 blame precision of 1.00); **chaos mode** (`chaos_release_order` reorders async completion to surface race bugs); competing-cause discrimination via temporal-Shapley. Supports Anthropic/OpenAI/Gemini/Bedrock/LangChain/LangGraph/CrewAI/AutoGen/Google ADK/OpenAI Agents SDK. 672 offline tests, no API key for validation. **This is the benchmark AgentCrash must beat or join.** tracefork's gaps vs. AgentCrash's stated scope: no regression-test *generation*, no FastAPI server + CLI platform wrapping, no canonical cross-framework event model (it adapts per-SDK), no SQLite storage story pitched as local-first.

**culpa** (MIT; created 2026-03-28). Flight recorder + deterministic replay (stubbed responses, $0) + counterfactual forking at any decision point. Two capture modes: SDK monkey-patching or **transparent proxy for Claude Code/Cursor** — explicitly coding-agent aware. No causal blame, no chaos, no test-gen. **Implication:** culpa owns the "coding-agent flight recorder" framing AgentCrash also wants. AgentCrash must differentiate by adding the causal + chaos + test-gen layers culpa lacks.

**causal-agent-replay** (Apache 2.0; created 2026-06-03). Models an agent run as a Pearl structural causal model; do-calculus interventions (`do_resample`, `do_action`, `do_observation`, `do_context`, `do_policy`). LangGraph/LangChain/OpenAI Agents/CrewAI adapters. **Implication:** the formal-causal methodology here is the academic bar AgentCrash's causal engine should be measured against.

**counterfact** (Apache 2.0; created 2026-05-05). Drop-in LangGraph replacement with counterfactual ablation (re-run with each agent removed/degraded) + Shapley attribution + ground-truth-free evals + failure classification + Claude Agent Skill for debugging. **Implication:** the Shapley-attribution-over-ablations approach is a competing causal method to tracefork's flip-rate/Wilson approach; AgentCrash should pick a side and document why.

**causal-agent-tracer** (MIT; created 2026-06-10). Per-step counterfactual failure attribution + A/B testing harness (power analysis, CUPED, propensity matching, DiD) + CI promotion gate. **Implication:** the CI promotion gate is the closest thing to "regression test from failure" in this cluster, but it gates on attribution, not generated test cases.

---

## 4. Gap analysis — what AgentCrash can actually own

### Genuinely uncontested whitespace (AgentCrash's defensible core)

1. **Regression-test GENERATION from failure analysis.** No tool in any cluster converts a causal-blame verdict on a prod failure into a runnable, committed regression test. DeepEval/Promptfoo/Inspect author tests by hand; tracefork/culpa/counterfact do blame but emit no tests. AgentCrash can own "failure -> blamed step -> generated pytest/Promptfoo case -> CI gate" as a closed loop. **This is the single strongest unique value prop.**

2. **One integrated local-first platform spanning the full record->replay->counterfactual->causal->chaos->test-gen loop.** The 2026 micro-tools each cover 2-4 of these; none cover all six, and none wrap them in a FastAPI server + CLI + SQLite storage + canonical event model. The integration is the product.

3. **Framework-agnostic canonical event model with foreign-event mapping.** OpenLLMetry defines OTel gen_ai.* attributes but not a replay/counterfactual event model. tracefork adapts per-SDK. AgentCrash's `agentcrash.schema` (Actor, ActorType, AgentCrashEvent, Artifact, ErrorInfo, EventStatus, EventType, ReplayMeta, Source) is a deliberately cross-framework replay-ready event model — this is a real differentiator if the adapters land for OpenLLMetry/Langfuse/Phoenix/Claude Code.

4. **Coding-agent awareness as a first-class citizen.** Only culpa (proxy for Claude Code/Cursor) and marginally PostHog/Sentry/MLflow touch this. AgentCrash can make Claude Code, Cursor, Codex, and MCP-tool calls first-class traced actors with replay semantics — not just "supported via integration."

5. **Chaos/fault injection as a reliability discipline, not just blame validation.** tracefork uses fault injection to *validate its blame engine*; AgentCrash can position chaos as a proactive reliability practice (inject faults into a recorded tape, observe agent resilience, generate tests for the failures that surface) — closer to chaos engineering than to blame auditing.

### Contested but winnable

6. **Counterfactual replay + causal blame.** Contested by tracefork/culpa/causal-agent-replay/counterfact. AgentCrash wins only if it (a) integrates blame with test generation, (b) supports more frameworks via the canonical model, and (c) is more usable than tracefork's CLI-first approach via the FastAPI server + web UI. Losing here is acceptable if #1 and #2 are strong.

7. **Local-first observability with replay.** Contested by Phoenix/Langfuse/Agentops. AgentCrash wins only on the replay+causal+chaos layers, not on raw tracing/dashboarding — those are commodity. Do not compete on dashboards.

### Not a moat (do not invest)

8. **Raw OTel span capture.** Commodity — OpenLLMetry, Phoenix, Langfuse, Sentry, Datadog, Honeycomb all do this. AgentCrash should consume OTel gen_ai.* via OpenLLMetry, not reinvent instrumentation.

9. **LLM-as-judge evals.** Commodity — every eval framework and most observability platforms ship these. Delegate to DeepEval/Ragas/Promptfoo.

10. **Cost/token analytics, prompt management, A/B routing.** Owned by Helicone/MLflow/Braintrust/Langfuse. Not AgentCrash's job.

---

## 5. Recommendations for AgentCrash

1. **Make regression-test generation the headlining feature**, not replay or causal (those are now contested). The output formats that matter: pytest cases (DeepEval-compatible), `promptfooconfig.yaml` fragments, Inspect AI tasks. The input: a blamed step on a recorded tape. The loop: failure -> blame -> generated test -> CI gate -> never regress.

2. **Interop, don't compete, on tracing.** Ship an OpenLLMetry/OTLP ingest adapter that maps gen_ai.* spans into `agentcrash.schema`. Ship a Langfuse trace importer (Langfuse already has the `Rewind` precedent). Ship a Phoenix Span-Replay bridge. Be the reliability layer that sits on top of existing observability, not a replacement for it.

3. **Study tracefork and publish a comparison.** tracefork is the direct competitor on replay+causal+chaos. AgentCrash must be explicit about what it does that tracefork does not (test generation, platform/UI, canonical event model, coding-agent first-class). Consider whether to absorb tracefork's methodology (Wilson-CI flip-rate blame, temporal-Shapley, the 5 fault types) rather than invent a rival causal method from scratch.

4. **Pick a causal methodology and document the choice.** The 2026 cluster offers three: Pearl do-calculus (causal-agent-replay), ablation+Shapley (counterfact), and flip-rate+Wilson+temporal-Shapley (tracefork). AgentCrash should adopt one (recommend tracefork's flip-rate approach for its empirical validation story) and cite the others as alternatives.

5. **Ship Claude Code / Cursor / Codex capture early.** culpa proves there is demand for coding-agent flight recording. AgentCrash's coding-agent awareness should be a vertical slice in Phase 2, not a roadmap afterthought. Proxy-based capture (culpa's approach) and SDK-based capture (tracefork's approach) should both be supported.

6. **License MIT or Apache 2.0 — not ELv2.** Phoenix (ELv2) and Agentops app (ELv2) have a subtle enterprise-adoption friction. Langfuse (MIT) and MLflow (Apache 2.0) show that permissive licensing wins self-host adoption. AgentCrash's MIT/Apache choice is a competitive advantage over ELv2 tools; keep it.

7. **Do not build a dashboarding product.** Phoenix, Langfuse, Datadog, and Honeycomb own visualization. AgentCrash's UI should be a replay/counterfactual/test-gen workbench, not a generic trace explorer. Budget the UI accordingly.

8. **Position against the cluster, not against Langfuse.** The marketing battle is "AgentCrash vs. tracefork/culpa/counterfact" (who is the best open-source local agent reliability lab), not "AgentCrash vs. Langfuse" (different layer). Co-exist with Langfuse/Phoenix; differentiate from tracefork.

---

## 6. Sources

- Langfuse: [langfuse.com/self-hosting](https://langfuse.com/self-hosting), [github.com/langfuse/langfuse](https://github.com/langfuse/langfuse), [Rewind blog](https://agentoptics.dev/blog/langfuse-debugging/)
- LangSmith: [langchain.com/pricing](https://www.langchain.com/pricing), [docs.langchain.com/langsmith/self-hosted](https://docs.langchain.com/langsmith/self-hosted)
- Arize Phoenix: [github.com/Arize-AI/phoenix](https://github.com/Arize-AI/phoenix), [arize.com/docs/phoenix](https://arize.com/docs/phoenix.md)
- Helicone: [github.com/helicone/helicone](https://github.com/helicone/helicone), [docs.helicone.ai](https://docs.helicone.ai/getting-started/self-host/docker)
- Braintrust: [braintrust.dev/docs/admin/self-hosting](https://www.braintrust.dev/docs/admin/self-hosting), [braintrust.dev/docs/evaluate](https://www.braintrust.dev/docs/evaluate)
- Agentops: [github.com/AgentOps-AI/agentops](https://github.com/AgentOps-AI/agentops), [docs.agentops.ai](https://docs.agentops.ai/v2/quickstart)
- PostHog: [posthog.com/docs/ai-observability/traces](https://posthog.com/docs/ai-observability/traces)
- OpenLLMetry: [github.com/Traceloop/openllmetry](https://github.com/Traceloop/openllmetry)
- Sentry AI: [docs.sentry.io/ai/monitoring](https://docs.sentry.io/ai/monitoring/), [blog.sentry.io/ai-agent-observability](https://blog.sentry.io/ai-agent-observability-developers-guide-to-agent-monitoring/)
- Datadog: [datadoghq.com/product/ai/llm-observability](https://www.datadoghq.com/product/ai/llm-observability/2/), [docs.datadoghq.com/llm_observability](https://docs.datadoghq.com/llm_observability.md)
- Honeycomb: [honeycomb.io/use-cases/ai-llm-observability](https://www.honeycomb.io/use-cases/ai-llm-observability), [honeycomb.io/platform/bubbleup](https://www.honeycomb.io/platform/bubbleup)
- Dify: [github.com/langgenius/dify](https://github.com/langgenius/dify)
- DeepEval: [github.com/confident-ai/deepeval](https://github.com/confident-ai/deepeval), [deepeval.com/docs/introduction](https://deepeval.com/docs/introduction)
- Promptfoo: [github.com/promptfoo/promptfoo](https://github.com/promptfoo/promptfoo), [promptfoo.dev/docs/integrations/ci-cd](https://www.promptfoo.dev/docs/integrations/ci-cd.md)
- Ragas: [github.com/explodinggradients/ragas](https://github.com/explodinggradients/ragas), [docs.ragas.io](https://docs.ragas.io/en/latest/)
- Inspect AI: [github.com/ukgovernmentbeis/inspect_ai](https://github.com/ukgovernmentbeis/inspect_ai), [inspect.aisi.org.uk](https://inspect.aisi.org.uk/)
- MLflow: [mlflow.org](https://mlflow.org/), [mlflow.org/ai-gateway](https://mlflow.org/ai-gateway), [mlflow.org/docs/latest/genai/tracing](https://mlflow.org/docs/latest/genai/tracing/)
- LlamaTrace: [arize.com/resource/introducing-hosted-phoenix-llamatrace](https://arize.com/resource/introducing-hosted-phoenix-llamatrace/), [developers.llamaindex.ai/observability](https://developers.llamaindex.ai/python/framework/module_guides/observability/)
- OpenTelemetry GenAI: [github.com/open-telemetry/semantic-conventions-genai](https://github.com/open-telemetry/semantic-conventions-genai), [Issue #35](https://github.com/open-telemetry/semantic-conventions-genai/issues/35)
- tracefork: [pypi.org/project/tracefork](https://pypi.org/project/tracefork/)
- culpa: [github.com/AnshKanyadi/culpa](https://github.com/AnshKanyadi/culpa)
- causal-agent-replay: [github.com/jaineet17/causal-agent-replay](https://github.com/jaineet17/causal-agent-replay)
- counterfact: [github.com/counterfact-labs/counterfact](https://github.com/counterfact-labs/counterfact)
- causal-agent-tracer: [github.com/rahul-alhan/causal-agent-tracer](https://github.com/rahul-alhan/causal-agent-tracer)