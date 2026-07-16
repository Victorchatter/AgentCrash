# Chaos Engineering and Fault Injection for AI Agents

> Research note for AgentCrash. Covers classical chaos engineering principles, HTTP/network fault injection tooling, the agent-specific fault surface, reproducibility techniques, and a proposed design for AgentCrash's chaos engine (fault taxonomy, YAML test format, injection mechanism, reliability scoring).
>
> Status: research, 2026-07-15. Citations inline as links.

---

## 1. Why chaos engineering for agents is a different problem

Classical chaos engineering asks: *"does my distributed system keep serving users when a component fails?"* Agent chaos engineering asks a harder, stranger question: *"does my system keep producing **correct** behavior when the world, the tools, the context, and the model itself degrade?"*

The difference is threefold:

1. **The failure surface is semantic, not just operational.** A tool that returns HTTP 200 with a subtly wrong number is a worse failure than a tool that times out — the timeout is visible, the wrong number propagates into the agent's reasoning and may never be caught. The 2026 tool **faultline** was built precisely around this "200-OK-but-wrong" class and found 17 real silent bugs in 16 popular open-source AI projects (Aider, GPT Researcher, LlamaIndex, LangChain, pandas-ai) using deterministic injection with no LLM judge, so it can gate CI without flaking. ([faultline on PyPI](https://pypi.org/project/faultline/))

2. **The blast radius crosses a cognitive boundary.** A failed tool call doesn't just raise an exception — it changes what the agent *believes*, which changes subsequent tool calls, which changes the final answer. The downstream propagation is through the model's reasoning, not through a call graph you can statically trace. **HEXFIRE** models this explicitly with a DAG Cascade Engine that maps blast radius across pipeline nodes. ([HEXFIRE](https://github.com/Shreekumar-Shah-AICTE/hexfire))

3. **Non-determinism is the default, not an anomaly.** Same inputs, different run. This means "reproducibility" can't mean "same bits out" — it has to mean *deterministic fault schedule + pinned model + replayed tool responses + invariant-checked outcomes*.

---

## 2. Classical chaos engineering — the Netflix principles

Chaos engineering as a discipline was crystallized by Netflix's Simian Army, of which **Chaos Monkey** is the famous member. The principles AgentCrash should inherit:

### 2.1 Steady-state hypothesis
Every chaos experiment starts by defining the measurable **steady state** of the system in normal operation — the metrics that define "healthy." The experiment then injects failure and checks whether steady state is preserved or breaks. The output of an experiment is not "it crashed" or "it didn't" — it is *a falsifiable claim about an invariant*.

> Translated to agents: steady state is not "no exceptions." It is "the final answer satisfies invariant X," "no hallucinated tool was invoked," "the agent abstained when context was insufficient," "cost stayed under budget C."

### 2.2 Blast radius
Chaos Monkey originally ran only in one AWS region, during business hours, on a subset of instances — to limit **blast radius** while engineers learned. The principle: start small, contain the damage, expand the surface as confidence grows. AgentCrash should let users scope faults to a single tool, a single replay, a single turn, a single agent in a multi-agent system.

### 2.3 Controlled experiments, not random breakage
A chaos experiment is a *controlled* scientific experiment: hypothesis, independent variable (the fault), dependent variable (the steady-state metric), control run (no fault), treatment run (fault injected), comparison. Random "let's break stuff" is not chaos engineering; it is vandalism.

### 2.4 Automation and cadence
Chaos Monkey ran continuously. Chaos engineering is most valuable when it is a **regular, automated cadence** in CI or staging — not a one-off manual exercise. AgentCrash chaos tests should run on every PR that touches tools, prompts, or model selection, and on a weekly schedule against recorded replays.

Reference background: [Netflix TechBlog on Chaos Monkey](https://netflixtechblog.com/tagged/chaos-engineering); the general principles are summarized well in the [principlesofchaos.org](https://principlesofchaos.org/) definition.

---

## 3. HTTP and network fault injection — the tooling layer

Before agents existed, the fault-injection toolbelt was built for HTTP/TCP services. AgentCrash reuses this layer for the network-shaped subset of agent faults.

### 3.1 Toxiproxy
[Toxiproxy](https://github.com/Shopify/toxiproxy) (Shopify, MIT, ~12k stars, v2.12.0 March 2025, last push May 2026) is a TCP proxy that simulates network conditions in test/CI/dev. It sits between your app and its dependencies; you manipulate connection health over an HTTP API on port 8474. No app code changes beyond connection strings.

**Built-in toxics** (each supports `stream: upstream|downstream` and `toxicity: 0.0–1.0` probability):

| Toxic | What it does | Agent-relevant use |
|---|---|---|
| `latency` | Adds delay ± jitter | Model API or tool latency spikes |
| `timeout` | Stops data, closes after N ms (or hangs if 0) | Tool / MCP server hangs |
| `reset_peer` | TCP RESET | Dropped connection mid-stream |
| `bandwidth` | Cap KB/s | Slow tool responses (large file reads) |
| `slow_close` | Delays socket close | Half-open connections |
| `slicer` | Chunks data with delays | Fragmented streaming responses |
| `limit_data` | Closes after N bytes | Truncated responses |
| `down` | Disables proxy entirely | Full dependency outage |

Toxics can be **deterministic** (toxicity 1.0) or **probabilistic** (e.g. 30% of connections). Custom toxics can be implemented via the `Toxic` interface (`Pipe(*toxics.ToxicStub)`). HTTP API: `POST /proxies/{proxy}/toxics` to add, `POST /reset` to clear all. Prometheus metrics at `/metrics`. Client libraries exist for Python, Go, Node, Rust, Java, and more. ([README](https://github.com/Shopify/toxiproxy/blob/main/README.md), [CREATING_TOXICS.md](https://github.com/shopify/toxiproxy/blob/main/CREATING_TOXICS.md))

A [Feb 2026 OneUptime guide](https://oneuptime.com/blog/post/2026-02-08-how-to-use-docker-for-chaos-engineering-with-toxiproxy/view) gives a complete Docker Compose pattern: Toxiproxy between an API and Redis/Postgres, Python integration tests driving the HTTP API to inject latency/reset_peer/bandwidth mid-test.

**Where AgentCrash uses Toxiproxy:** the network-only faults — tool HTTP 500, latency, timeout, reset, bandwidth throttling, partial response. AgentCrash should NOT use Toxiproxy for semantic faults (malformed JSON, wrong numbers) — those need to intercept at the tool-response layer, not the byte layer.

### 3.2 httpx-retry and resiliency testing
At the application resiliency layer, Python's `httpx` ecosystem provides:
- **`httpx-retry`** / transport-layer retry: configurable retries with backoff for 429/5xx, idempotency-aware.
- **Custom retry transports** (`httpx.AsyncHTTPTransport` subclassing) — the natural seam for AgentCrash to inject synthetic 429/500/timeout responses in tests by swapping the transport.
- **`tenacity`** — higher-level retry decorators with exponential backoff, jitter, conditional retry on exception types. Useful for modeling the *expected* resiliency the agent's tool wrappers should have.

The test pattern: write an `httpx.MockTransport` that returns canned fault responses deterministically, then assert the agent's retry/circuit-breaker logic preserves the steady-state invariant (e.g. "after 3 retries the agent falls back to a cached answer, it does not loop forever").

### 3.3 Other network-layer tools worth knowing
- **Chaos Mesh** (Kubernetes-native, CRD-based) — for agents deployed on k8s.
- **Gremlin** (commercial) — polished UI, fine-grained targeting.
- **Pumba** (Docker-native) — network delay/loss/partition for containerized agents.
- **mitmproxy** — HTTP MITM proxy; this is the seam **Rewind** uses to record/replay agent runs (see §6.1).

---

## 4. The agent-specific fault surface — full taxonomy

This is the core contribution AgentCrash makes over classical chaos tooling. Below is the fault taxonomy, grouped by the **layer** the fault belongs to. Each entry lists: the fault, how it manifests in an agent, what invariant it threatens, and the natural injection point.

### 4.1 Tool-layer faults
| Fault | Manifestation | Threatened invariant | Injection point |
|---|---|---|---|
| **Tool timeout** | Tool call exceeds budget; agent must give up or retry | No infinite loops; bounded wall-clock | Tool wrapper / Toxiproxy |
| **Tool failure (exception)** | Tool raises; agent must handle | Agent never crashes on tool error; falls back gracefully | Tool wrapper |
| **Malformed JSON output** | Tool returns syntactically invalid JSON | Agent does not propagate parse error to user answer; retries or asks | Tool response interceptor |
| **Schema drift** | Tool returns valid JSON but wrong schema (missing field, renamed key) | Agent validates tool output before use | Tool response interceptor |
| **HTTP 429 / rate limit** | Provider throttles | Agent backs off, does not hammer, does not deadlock | httpx transport / Toxiproxy |
| **HTTP 5xx / server error** | Tool upstream broken | Agent distinguishes transient vs permanent; retries appropriately | httpx transport |
| **Network latency / jitter** | Slow responses | Agent stays under wall-clock budget; no timeouts cascade | Toxiproxy `latency` toxic |
| **Partial / truncated response** | Tool response cut mid-payload | Agent detects incompleteness, does not treat as final | Toxiproxy `limit_data` / response interceptor |
| **Missing tool** | Tool referenced by prompt is not registered / unavailable | Agent declines gracefully, does not hallucinate tool behavior | Agent tool registry mock |

### 4.2 MCP-server faults (a distinct sub-case)
MCP (Model Context Protocol) servers are long-lived tool providers. Their failure modes deserve their own bucket because they sit one layer above raw tools:
| Fault | Manifestation | Injection point |
|---|---|---|
| **MCP server failure / crash** | Server process dies mid-session | Kill subprocess; assert agent reconnects or degrades |
| **MCP server delay** | Server slow to respond to `tools/list` or `tools/call` | Toxiproxy on stdio/HTTP transport, or sleep wrapper |
| **MCP server returns malformed handshake** | Schema mismatch at initialize | Mock transport returning bad `InitializeResult` |
| **MCP tool list changes mid-session** | Tools appear/disappear | Dynamic registry mutation between turns |
| **MCP server resource exhaustion** | Server OOMs under load | Resource-limited subprocess |

### 4.3 Model-layer faults
| Fault | Manifestation | Injection point |
|---|---|---|
| **Model fallback / downgrade** | Primary model unavailable; fallback to smaller/cheaper model | Model client wrapper: swap completion endpoint |
| **Model 429 / overload** | Provider returns overload | httpx transport; assert graceful degradation |
| **Token budget reduction** | Effective context window shrinks mid-run | Truncate context passed to model; assert behavior under pressure |
| **Context truncation** | Earlier turns or tool outputs dropped from context | Truncation function in context assembler; assert key info preserved |
| **Streaming interruption** | SSE stream drops mid-chunk | Mock SSE transport that closes early |

### 4.4 Memory / context faults
| Fault | Manifestation | Injection point |
|---|---|---|
| **Memory corruption** | Stored memory value altered | Mutate the memory store entry before read |
| **Memory contradiction** | Two stored facts conflict | Inject conflicting entries; assert agent detects or reasons over conflict |
| **Stale memory** | Memory points to outdated state | Timestamp-skew memory entries |
| **Context poisoning** | Untrusted content injected into context | Append adversarial text to context buffer |

### 4.5 Adversarial / security faults
| Fault | Manifestation | Injection point |
|---|---|---|
| **Prompt injection** | Untrusted input rewrites agent instructions | Inject adversarial user/tool content; assert agent does not comply with injected instructions (OWASP LLM01) |
| **Malicious tool output** | Tool returns content that tries to issue further instructions | Tool response interceptor returning prompt-injection payloads |
| **Jailbreak via tool output** | Tool output contains encoded escape / second-stage prompt | As above |

### 4.6 Environment / resource faults
| Fault | Manifestation | Injection point |
|---|---|---|
| **Filesystem permission failure** | `PermissionError` on read/write | Wrap file ops; raise `PermissionError` |
| **Shell failure / nonzero exit** | Subprocess returns nonzero / stderr | Mock subprocess result |
| **Browser failure** | Headless browser crashes, page navigation fails, element not found | Playwright MCP mock / browser driver |
| **Disk full / write failure** | `OSError: No space left` | Mock `open()` to raise |
| **Env var missing** | Required config absent | Unset env var before run |

### 4.7 Silent semantic faults (the hardest class)
These produce 200-OK-but-wrong behavior. faultline's library is the canonical reference:
| Fault | What it does |
|---|---|
| **WrongNumber** | Numeric value subtly off in tool response |
| **StaleData** | Tool returns last-week's data as if current |
| **NullResponse** | Tool returns null where data expected |
| **Truncate** | Tool output cut, agent reasons on incomplete info |
| **ServerError** masked as success | Error swallowed, default returned |

faultline's `--semantic` mutation (via Rewind's `mutate`) uses a small model to rewrite tool responses into *fluent but wrong* content — flipped recommendations, changed numbers — which is far harder to catch than obvious errors. ([faultline](https://pypi.org/project/faultline/))

---

## 5. Reproducibility — the central engineering challenge

Agent runs are non-deterministic by default. A chaos test is only useful if a failure it finds can be reproduced, bisected, and fixed. The reproducibility stack, from least to most robust:

### 5.1 Deterministic fault schedule
A chaos test must specify *exactly* which fault fires on which call, at which turn, with which parameters — not "inject 30% latency." Probabilistic toxics are fine for *exploration*; they are unacceptable for *regression tests*. AgentCrash YAML tests address faults by `(turn_index, call_index, fault)` tuples, not by probability.

### 5.2 Pinned model + captured model responses
Two layers:
- **Pin the model** (provider + model id + version + `temperature=0` where supported). This reduces but does not eliminate nondeterminism — providers can still return different tokens at temp 0.
- **Capture and replay model responses.** The robust approach (used by Rewind) records the full model I/O via a MITM proxy and replays the *exact* responses on rerun, so the only thing that varies between runs is the fault you inject. ([Rewind](https://github.com/llm-rewind/rewind))

### 5.3 Replayed tool responses (cassettes)
Tool calls are the primary source of nondeterminism (network, time, external state). AgentCrash's existing Record → Replay architecture is the foundation: record a real agent run into a cassette, then replay with faults injected at the replay/tool layer. The tool responses are fixed; only the injected fault varies.

### 5.4 Pinned clock and random seed
- Inject a deterministic clock (`time.time()` → fixed sequence) so time-dependent logic (backoff, TTL checks, "is this data stale?") is reproducible.
- Seed `random`/`numpy` so any jitter in the agent's own code is fixed.

### 5.5 Frozen external state
Any external system the agent touches (filesystem, DB, MCP servers) must be reset to a known snapshot before each run. For filesystem: a tempdir fixture. For MCP: a scripted mock server. For HTTP: recorded cassettes (not live calls).

### 5.6 Invariant-based outcome comparison
Because the final answer text may differ run-to-run even with pinned models, **reproducibility is checked at the invariant level, not the string level.** A test "passes" if its declared invariants hold; a test "fails reproducibly" if the same fault schedule reliably breaks the same invariant across N reruns. If a fault breaks an invariant only 1/10 times, that itself is a finding (a flaky-invariant bug).

### 5.7 Content-addressed evidence
Rewind and HEXFIRE both use SHA-256 content addressing for blobs and a tamper-evident audit chain for experiment logs. AgentCrash should hash (fault schedule + cassette + invariants + outcomes) into a single experiment digest so a result can be cited, verified, and re-run by digest.

---

## 6. The 2026 landscape — what AgentCrash should learn from

### 6.1 Rewind — record/replay + bisect
[github.com/llm-rewind/rewind](https://github.com/llm-rewind/rewind) (May 2026, MIT, Python). Records production agent runs via mitmproxy HTTPS MITM; works with any language/framework. Key commands:
- `rewind bisect` — classifies *why* two runs diverged: model version bump, tool output drift, prompt drift, or non-determinism.
- `rewind explain` — root-cause with downstream propagation + confidence scores.
- `rewind mutate` — Stryker-for-LLM-agents mutation testing: drops steps, returns 429s, truncates, replaces tool outputs with errors; `--semantic` rewrites responses into subtly wrong content.
- `rewind benchmark` — fragility scoring with a leaderboard; weekly GitHub Action re-scores recorded agents in replay.
- Content-addressed blob store (SHA-256, zstd), DuckDB metadata, pytest integration.

**Lesson for AgentCrash:** the record/replay + bisect loop is the killer feature. Chaos tests are most powerful when they run against *real recorded production traces*, not synthetic scenarios.

### 6.2 faultline — deterministic, CI-gateable, no LLM judge
[PyPI](https://pypi.org/project/faultline/) / [GitHub](https://github.com/aaravanmay/faultline) (v0.4.2 June 2026, MIT, Python). Six modes: `scan`, `probe`, `fuzz`, `scenarios`, `replay`, `mine`, `chaos`. Fault library: `WrongNumber`, `StaleData`, `Truncate`, `NullResponse`, `Timeout`, `ServerError`. Reusable invariants: `numeric_answer_finite()`, `abstain_when_context_empty()`, `no_poison_parroting()`, `no_silent_shrink()`. Runtime guard `fl.guard()` (shadow/enforce). Tamper-evident SHA-256 attestation reports.

**Lesson for AgentCrash:** a small, named, composable invariant library beats ad-hoc assertions. Invariants should be reusable across tests and shippable as a library. Avoid LLM-as-judge for CI gating — it flakes.

### 6.3 agentfuzz — broad fault catalog, multi-framework
[github.com/SubhashPavan/agentfuzz](https://github.com/SubhashPavan/agentfuzz) (v0.4.0 May 2026, Apache-2.0, Python). 11 fault types: `ToolTimeout`, `MalformedToolResponse`, `PartialToolFailure`, `LatencyJitter`, `CostSpiral`, `PromptInjection`, `PromptParaphrase`, `RateLimitBurst`, `SchemaDrift`, `AuthExpiry`, `NetworkPartition`. Adapters for LangChain 1.x, LangGraph, CrewAI, AutoGen 0.4+, plain callables. Reports: per-fault pass-rate, cost-blast radius, tool-call failure modes, prompt-injection survival, replay traces.

**Lesson for AgentCrash:** the fault catalog must include cost (`CostSpiral`) and auth (`AuthExpiry`) — both are real production failure modes that pure network chaos tools miss.

### 6.4 Agent Chaos — scenario-driven YAML, TypeScript
[github.com/reaatech/agent-chaos](https://github.com/reaatech/agent-chaos) (April 2026, MIT, TypeScript). 8 fault types: latency, timeouts, rate limits, malformed output, token exhaustion, stale context, contradictory results, partial failures. YAML/JSON scenario configs, glob-targeted injection, temporal patterns, cascading failures, hot reload, OpenTelemetry tracing, JUnit/HTML/JSON reports. Framework-agnostic (LangChain, LlamaIndex, Vercel AI SDK).

**Lesson for AgentCrash:** declarative YAML scenarios + glob targeting + OTel tracing + JUnit output is exactly the right shape for CI integration. The 8-fault catalog is narrower than agentfuzz but the DX is the model to follow.

### 6.5 HEXFIRE — DAG cascade + R-Score
[github.com/Shreekumar-Shah-AICTE/hexfire](https://github.com/Shreekumar-Shah-AICTE/hexfire) (May 2026, MIT, TypeScript). 6 vectors: hallucination injection, tool timeout, adversarial prompt, context corruption, latency spike, permission revoked (403). DAG Cascade Engine maps blast radius across nodes. **R-Score** formula: `R = (S×40) + (Rec×25) + (Iso×20) + (Gr×15)` (Steady-state, Recovery, Isolation, Graceful-degradation) with star ratings. SHA-256 audit chain. Gemini 2.5 Flash forensic reports.

**Lesson for AgentCrash:** a published, deterministic resilience formula is a strong differentiator. HEXFIRE's R-Score is a good starting point but underweights cost and correctness — AgentCrash should extend it.

---

## 7. Proposed AgentCrash chaos engine

### 7.1 Design principles
1. **Replay-first.** Chaos tests run against recorded cassettes, not live agents. Reproducibility is non-negotiable.
2. **Deterministic fault schedule.** Faults are addressed by `(turn, call)` coordinates, not probabilities. Exploration mode allows probabilities; regression mode does not.
3. **Invariant-checked outcomes.** Tests declare steady-state invariants; outcomes are compared at the invariant level, not the string level.
4. **Layered injection.** Network faults go through Toxiproxy; semantic/model/memory faults go through an in-process interceptor at the replay/tool layer. One config format, two backends.
5. **Composable invariants as a library.** Ship a named invariant set (faultline-style) and let users add their own.
6. **Content-addressed experiments.** Every run produces a digest over (schedule + cassette + invariants + outcomes) so results are citable and re-runnable.
7. **CI-native output.** JUnit XML + JSON + console; OTel spans for each fault injection; exit codes that gate CI.

### 7.2 Fault taxonomy (AgentCrash canonical set)

AgentCrash adopts a unified taxonomy spanning the layers in §4. Each fault has a stable string id, a layer, parameters, and a default injection backend.

| id | layer | params | backend |
|---|---|---|---|
| `tool.timeout` | tool | `ms`, `mode: hang\|close` | interceptor / Toxiproxy |
| `tool.exception` | tool | `exc_type`, `message` | interceptor |
| `tool.malformed_json` | tool | `corruption: syntax\|schema` | interceptor |
| `tool.schema_drift` | tool | `drop_fields[]`, `rename{}` | interceptor |
| `tool.http_429` | tool | `retry_after` | httpx transport |
| `tool.http_500` | tool | `count` | httpx transport |
| `tool.latency` | tool | `ms`, `jitter` | Toxiproxy |
| `tool.partial_response` | tool | `keep_bytes` | Toxiproxy `limit_data` / interceptor |
| `tool.missing` | tool | `tool_name` | registry mock |
| `tool.wrong_number` | tool-semantic | `field`, `delta` | interceptor |
| `tool.stale_data` | tool-semantic | `field`, `age_days` | interceptor |
| `tool.null_response` | tool-semantic | `field` | interceptor |
| `mcp.server_crash` | mcp | `server_id` | subprocess kill |
| `mcp.server_delay` | mcp | `ms` | transport sleep / Toxiproxy |
| `mcp.malformed_handshake` | mcp | `field` | mock transport |
| `mcp.tool_list_change` | mcp | `add[]`, `remove[]` | registry mutation |
| `model.fallback` | model | `to_model` | model client swap |
| `model.overload_429` | model | `retry_after` | httpx transport |
| `model.token_budget_cut` | model | `max_tokens` | context truncation |
| `model.context_truncation` | model | `keep_turns` | context assembler |
| `model.stream_interrupt` | model | `at_chunk` | mock SSE |
| `memory.corruption` | memory | `key`, `new_value` | store mutation |
| `memory.contradiction` | memory | `key`, `values[]` | store mutation |
| `memory.stale` | memory | `key`, `skew_seconds` | store mutation |
| `context.poisoning` | context | `payload` | context buffer append |
| `adv.prompt_injection` | adversarial | `payload`, `channel: user\|tool` | input/tool interceptor |
| `adv.malicious_tool_output` | adversarial | `payload` | tool response interceptor |
| `env.fs_permission` | env | `path` | fs mock raising `PermissionError` |
| `env.shell_failure` | env | `exit_code`, `stderr` | subprocess mock |
| `env.browser_failure` | env | `mode: crash\|nav_fail\|no_element` | browser driver mock |
| `env.disk_full` | env | `path` | fs mock raising `OSError` |
| `env.missing_env_var` | env | `var` | env unset |
| `cost.spiral` | cost | `budget`, `per_call_cost` | cost accumulator + abort |

Fault ids are namespaced by layer so users can glob (`tool.*`, `mcp.*`, `adv.*`) in scenario files.

### 7.3 YAML test format

```yaml
# agentcrash-chaos/v1
schema: agentcrash.io/chaos/v1
name: "research-agent-survives-tool-429-then-timeout"
description: |
  The research agent must fall back to cached summary when the search tool
  rate-limits then times out, and must not loop or hallucinate results.
record: rec_2026_07_15_a8f3c2          # cassette digest to replay against
model:
  pinned: anthropic:claude-opus-4.5@2026-07-01
  temperature: 0
  # if record contains captured model responses, replay them instead of calling live
  replay_model_responses: true
seed:
  random: 42
  clock: "2026-07-15T12:00:00Z"        # deterministic clock
target:
  agent: research_agent
  # glob over tool names; * matches all
  tools: ["web_search", "fetch.*"]
blast_radius:
  turns: [3, 4]                         # only inject on these turns
  calls: ["*"]                          # within those turns, all calls (or specific indices)
  max_parallel_faults: 1
schedule:
  - turn: 3
    call: 1
    fault: tool.http_429
    params: { retry_after: 30 }
  - turn: 4
    call: 1
    fault: tool.timeout
    params: { ms: 5000, mode: close }
invariants:
  - id: no_hallucinated_tool
    expr: "not any(call.tool == 'web_search_cached' and call.tool not in registered_tools for call in trace)"
  - id: abstains_when_no_source
    expr: "final_answer.citations == [] implies final_answer.text contains 'unable to retrieve'"
  - id: bounded_cost
    expr: "trace.total_cost_usd <= 0.25"
  - id: bounded_wall_clock
    expr: "trace.wall_clock_seconds <= 60"
  - id: no_infinite_loop
    expr: "trace.tool_call_count <= 20"
expected:
  # human-readable expected outcome for the report
  behavior: "agent reports inability to retrieve, cites no sources, stays under budget"
  verdict: pass                         # pass | fail | flaky
reproducibility:
  min_reruns: 5                         # run 5x; invariant must hold in all 5 to pass
  flaky_threshold: 0.2                  # >20% reruns breaking invariant => flaky finding
reporting:
  emit: [junit, json, console, otel]
  otel_service: agentcrash.chaos
```

Key design choices in the format:
- **`record`** binds the test to a specific cassette digest — the test is only defined relative to a recorded trace. This is what makes it reproducible.
- **`schedule`** is an ordered list of `(turn, call, fault, params)` — deterministic by construction. A separate `explore` mode (not shown) allows `probability` fields for discovery.
- **`invariants`** are expressions evaluated against the run `trace` (an AgentCrash trace object). Ship a standard library: `no_hallucinated_tool`, `abstains_when_no_source`, `bounded_cost`, `bounded_wall_clock`, `no_infinite_loop`, `no_poison_parroting`, `numeric_answer_finite`, `citations_back_claims`.
- **`reproducibility.min_reruns`** forces N reruns; a test that fails the invariant in only some reruns is reported as **flaky**, not pass/fail — flaky-invariant findings are first-class bugs.
- **`expected.verdict`** lets users declare the expected outcome so a chaos test that *should* pass but fails, or *should* fail but passes, both surface as findings.

### 7.4 Injection mechanism — intercept at the replay/tool layer

AgentCrash already has a Record → Replay architecture. The chaos engine plugs into it at three seams:

```
            ┌──────────────── AgentCrash replay runtime ────────────────┐
            │                                                            │
  cassette ─►  Replay driver ──►  [Fault Interceptor]  ──►  Agent loop   │
            │       │                  ▲                                 │
            │       │                  │ fault schedule                  │
            │       │          ┌───────┴────────┐                        │
            │       │          │  Fault Injector │                        │
            │       │          │  (turn,call) →  │                        │
            │       │          │  fault dispatch │                        │
            │       │          └───────┬────────┘                        │
            │       │                  │                                 │
            │       │       ┌──────────┼──────────┐                      │
            │       │       ▼          ▼          ▼                      │
            │       │   Tool layer  Model layer  Memory/Context          │
            │       │   (intercept   (swap       (mutate store,          │
            │       │    tool call   model       truncate context,       │
            │       │    response)   response)  poison buffer)           │
            │       │       │                                          │
            │       │       ▼                                          │
            │       │   Network layer (Toxiproxy sidecar)               │
            │       │   latency / 429 / 500 / reset / bandwidth         │
            │                                                            │
            │   Invariant Evaluator ──► Report (junit/json/otel)         │
            └────────────────────────────────────────────────────────────┘
```

- **Replay driver** reads the cassette and drives the agent loop turn by turn, exposing `(turn_index, call_index)` to the Fault Interceptor.
- **Fault Interceptor** is a single choke point: before each tool call, model call, or memory read, it consults the schedule and, if a fault is registered for `(turn, call)`, dispatches to the appropriate layer handler instead of letting the call proceed normally.
- **Layer handlers**:
  - *Tool layer*: wraps the tool callable; can raise, return malformed JSON, swap the response, or proxy through Toxiproxy for network faults.
  - *Model layer*: wraps the model client; can swap to a fallback model, return a captured response, truncate context, or interrupt streaming.
  - *Memory/context layer*: mutates the memory store or context buffer before the agent reads it.
  - *Network layer*: for genuine TCP-level faults, routes the tool's HTTP through a Toxiproxy sidecar started for the test run; the interceptor adds the toxic via the Toxiproxy HTTP API before the call and removes it after.
- **Invariant Evaluator** runs after the replay completes, evaluating each invariant expression against the trace and recording pass/fail per invariant per rerun.

This design means **one config format drives two backends** (in-process interceptor + Toxiproxy sidecar) and **the agent code under test never changes** — faults are applied at the seams the replay runtime already owns.

### 7.5 Reliability scoring

AgentCrash defines an **AgentCrash Reliability Score (ARS)** that extends HEXFIRE's R-Score with cost and correctness dimensions. ARS is computed per agent across a chaos test suite, on a 0–1000 scale.

```
ARS = 10 * (
    S  * 250 +     # Steady-state preservation: fraction of invariants that hold across all faults
    Rec * 200 +    # Recovery: fraction of faults after which agent returns to a valid state within K turns
    Iso * 150 +    # Isolation: blast radius containment — failure in tool A does not corrupt outputs for unrelated task B
    Gr * 150 +    # Graceful degradation: agent produces a useful (if lesser) answer rather than crashing or hallucinating
    Cst * 100 +   # Cost discipline: stays under budget under cost.spiral and retry storms
    Cor * 100 +   # Correctness under semantic faults: no_poison_parroting, numeric_answer_finite, citations_back_claims
    Rep * 50      # Reproducibility: low variance across reruns (penalize flaky invariants)
)
```

Each component is a 0..1 fraction measured across the test suite. Mapping to star bands:

| ARS | Rating | Meaning |
|---|---|---|
| 900–1000 | ★★★★★ | Production-hardened; survives most realistic fault combinations |
| 750–899 | ★★★★ | Solid; some graceful-degradation gaps |
| 600–749 | ★★★ | Breaks under common faults (429 + timeout combos) but recovers |
| 400–599 | ★★ | Silent-failure prone; hallucinates under semantic faults |
| < 400 | ★ | Crashes or hallucinates under single faults; not production-ready |

**Per-fault report:** for each fault id in the taxonomy, AgentCrash emits pass-rate, mean recovery turns, cost-blast, and which invariants broke. This is the leaderboard Rewind-style output that lets teams track reliability across commits.

**Why ARS over a single pass-rate:** a single pass-rate hides *which* dimension is weak. An agent can pass 90% of fault tests but always fail `adv.prompt_injection` — that is a security-critical 10% that a flat score obscures. The decomposed score makes the weak dimension visible and trackable.

### 7.6 Workflow integration

```
record (prod or staging) → curate cassette → write chaos YAML against cassette
  → run `agentcrash chaos run test.yaml` (replays N times, injects faults, evaluates invariants)
  → emit junit/json/otel → gate CI on exit code
  → on failure: `agentcrash chaos bisect <digest>` (replay with/without each fault to isolate the breaking fault)
  → `agentcrash chaos explain <digest>` (root-cause trace: which turn, which call, which invariant, propagation path)
```

The `bisect` and `explain` commands mirror Rewind's and are the bridge from "a chaos test failed" to "here is the fix."

---

## 8. Risks and open questions

1. **Model-response replay vs live model.** Replaying captured model responses gives perfect reproducibility but freezes the agent's reasoning at one trajectory — a fault that would change the *model's* behavior (e.g. context truncation changing the next token) cannot be tested by replaying the original response. AgentCrash must support a **hybrid mode**: replay tool responses, but call the live (pinned) model for model turns. This trades some reproducibility for the ability to test model-layer faults. Min-rerun + flaky-threshold absorbs the residual variance.
2. **Invariant expressiveness.** A small expression language (the `expr:` fields) is easy to start with but will hit limits (e.g. "the agent's answer is consistent with the cited sources" needs semantic checking). Plan to allow Python-callable invariants in addition to expressions, and reserve LLM-judge invariants for *non-CI* exploratory runs only — never gate CI on an LLM judge (faultline's lesson).
3. **Toxiproxy on Windows.** AgentCrash's primary user (Victor) is on Windows. Toxiproxy ships a native Windows binary, but the sidecar lifecycle (start/stop/port-management) must be handled carefully; the in-process interceptor should be the default and Toxiproxy an opt-in for pure network faults.
4. **Cost of running semantic mutations.** `--semantic` mutation (Rewind-style) calls a small model per mutated response — cheap per call, expensive across a large suite. Gate it behind a profile (`chaos run --profile deep`).
5. **Cassette drift.** A chaos test bound to a cassette digest becomes stale when the agent's tool set or prompt changes. AgentCrash should detect cassette/agent mismatch and prompt re-recording, not silently run tests against an obsolete trace.
6. **Adversarial-fault validity.** Prompt-injection tests are only as good as the injection payloads; a passing test does not prove robustness against novel injections. Frame these as *regression* tests for known payloads, not proof of security.

---

## 9. Summary

Classical chaos engineering gives AgentCrash its scientific method (steady-state hypothesis, blast radius, controlled experiments, automated cadence) and Toxiproxy gives it a mature network-fault backend. The agent-specific fault surface — tool failures, MCP failures, model downgrade, context truncation, memory corruption, prompt injection, semantic "200-OK-but-wrong" outputs — requires a new fault taxonomy and an in-process interceptor at the replay/tool layer, not just a network proxy. Reproducibility is achieved by binding tests to recorded cassettes, pinning models, deterministic fault schedules, pinned clocks, invariant-level (not string-level) comparison, and content-addressed experiment digests. The 2026 landscape (Rewind, faultline, agentfuzz, Agent Chaos, HEXFIRE) validates the approach and supplies concrete patterns to adopt: Rewind's record/replay/bisect, faultline's named invariant library and no-LLM-judge CI gating, agentfuzz's cost/auth faults, Agent Chaos's YAML+OTel+JUnit DX, and HEXFIRE's decomposed reliability score — which AgentCrash extends as ARS with cost and correctness dimensions.