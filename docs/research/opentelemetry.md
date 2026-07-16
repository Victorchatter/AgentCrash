# OpenTelemetry for LLM/Agent Tracing — Research for AgentCrash

> Verified against the OpenTelemetry core spec, the
> `open-telemetry/semantic-conventions-genai` repository, and the W3C Trace
> Context / Baggage Recommendations on **2026-07-15**. Covers the GenAI
> semantic conventions at **v1.41.1** (tagged 2026-05-11) with v1.42.0 in
> development. AgentCrash must treat the GenAI conventions as a moving
> target: they are still **Development** status, not Stable.

---

## 1. The OpenTelemetry signal model

OpenTelemetry (OTel) is a vendor-neutral observability standard that
graduated from the CNCF on **2026-05-21**. It defines three signals that
all share a common `Resource` (the entity being observed — process,
container, function) and a common `Context` (the trace/span identity that
correlates signals across process boundaries):

| Signal | Unit | Purpose |
|---|---|---|
| **Traces** | `Span` | A unit of work with a start time, end time, parent, kind, attributes, events, links, and status. Spans form a tree (or DAG via links) under a `Trace`. |
| **Logs** | `LogRecord` | A timestamped, structured record with severity, body, and attributes. The Logs API is the youngest signal; many ecosystems still emit "logs as span events." |
| **Metrics** | `Metric` | Aggregated measurements (counters, histograms, gauges) recorded by instruments. |

A **Tracer** produces spans; a **Logger** produces log records; a
**Meter** produces metrics. All three hand data to a `TracerProvider` /
`LoggerProvider` / `MeterProvider`, which routes them through a
`SpanProcessor` / `LogRecordProcessor` to an `Exporter` (push) or the
SDK's in-memory readable streams (pull). The canonical wire format
between SDKs and the Collector is **OTLP** (OpenTelemetry Protocol),
over gRPC (port **4317**) or HTTP (port **4318**).

### 1.1 Span anatomy

```
Span {
  trace_id        : 16 bytes (32 hex)
  span_id         : 8  bytes (16 hex)
  parent_span_id  : 8 bytes, or empty for a root
  name            : string (e.g. "chat gpt-4o")
  kind            : INTERNAL | SERVER | CLIENT | PRODUCER | CONSUMER
  start_time      : nanosecond timestamp
  end_time        : nanosecond timestamp
  attributes      : map<string, scalar|array>
  events          : list<{ name, timestamp, attributes }>   // timed logs on the span
  links           : list<{ span_context, attributes }>      // up to 128, DAG edges
  status          : { code: UNSET|OK|ERROR, description }
  resource        : the enclosing Resource (service.name, etc.)
}
```

Key points for AgentCrash:

- **Attributes** are low-cardinality, indexed, filterable. Use them for
  `gen_ai.operation.name`, `gen_ai.provider.name`, token counts, model
  id, tool name.
- **Events** are high-cardinality, not-indexed, time-stamped blobs
  attached to a span. The GenAI spec deliberately moves full
  prompt/completion bodies **out of attributes and into events** (e.g.
  `gen_ai.client.inference.operation.details`) to avoid PII leaks and
  indexing blowup. AgentCrash should follow the same rule: put raw
  message payloads, tool I/O blobs, and reasoning traces in events,
  not attributes.
- **Links** relate a span to other spans *without* making them
  children. Essential for fan-out/fan-in, retries, and "this span was
  caused by those N earlier spans." AgentCrash replay/branching
  scenarios should use links heavily (see §6).
- **Status** carries `OK`/`ERROR` plus a description. The
  `error.type` attribute is **Stable**; pair `status.code=ERROR` with
  `error.type` for dashboards and alerts.

### 1.2 Logs vs span events

The OTel Logs signal became stable later than traces and metrics, and
the GenAI ecosystem still predominantly uses **span events** for
structured content (prompts, completions, tool payloads). The Logs
signal is the right home for free-form application logs and for
out-of-band evidence (e.g. a chaos injection notice emitted by the
AgentCrash harness that is not naturally a child of any agent span).
AgentCrash should emit both: span events for in-line agent content,
and log records for harness-level events, correlating them by
`trace_id` / `span_id`.

---

## 2. GenAI semantic conventions — `gen_ai.*`

### 2.1 Where the spec lives

The GenAI conventions have moved out of the core
`open-telemetry/semantic-conventions` repo into a dedicated
**`open-telemetry/semantic-conventions-genai`** repository (created
2026-05-05; ~v1.41.1 tagged 2026-05-11; v1.42.0 in development). The
core repo still hosts the YAML model
(`model/gen-ai/spans.yaml`) but the human-facing docs and new
attributes now live in the genai repo. Weaver manages the dependency
on core conventions.

### 2.2 Status — Development, not Stable

Every GenAI span, event, and attribute page is badged
**![Development]** (blue). Names and structures can still change. In
the last ~11 months the conventions went through *material* breaking
changes:

| Version | Change |
|---|---|
| v1.37 | `gen_ai.system` → `gen_ai.provider.name` |
| v1.37 | `gen_ai.usage.prompt_tokens` → `gen_ai.usage.input_tokens` |
| v1.37 | `gen_ai.usage.completion_tokens` → `gen_ai.usage.output_tokens` |
| v1.37 | `gen_ai.prompt` / `gen_ai.completion` removed; content moves to the Event API |
| v1.41.0 | Added `gen_ai.usage.reasoning.output_tokens` (for reasoning models) |
| v1.41.x | Added memory operations, `gen_ai.workflow.name`, `gen_ai.conversation.compacted`, `gen_ai.evaluation.*` |

The transition plan requires instrumentations using v1.36.0 or prior
to gate the new attributes behind
`OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental`. AgentCrash
should pin the `opentelemetry-semantic-conventions` library version
and centralize every attribute key in one constants module so a
rename is a one-file change.

### 2.3 The five agent span operations

The GenAI spans doc defines the operations that matter for an agent
runtime. The canonical agent-loop tree as of mid-2026:

```
invoke_agent_client   (CLIENT)     — caller side, treats the agent as a remote service
  └─ invoke_agent_internal (INTERNAL) — the agent's top-level reasoning loop
       ├─ chat                 (CLIENT)   — model inference (gen_ai.operation.name=chat)
       ├─ execute_tool         (CLIENT|INTERNAL) — the agent running a tool
       └─ invoke_workflow      (CLIENT)   — a discrete workflow step / sub-graph node
```

| Operation | `gen_ai.operation.name` | Span kind | Notes |
|---|---|---|---|
| Create agent | `create_agent` | CLIENT | Wraps agent instantiation. |
| Invoke agent (caller) | `invoke_agent` | CLIENT | The caller side, agent as remote service. |
| Invoke agent (internal) | `invoke_agent` | INTERNAL | The reasoning loop; parent of model + tool spans. |
| Invoke workflow | `invoke_workflow` | CLIENT | A sub-graph node / workflow step. |
| Execute tool | `execute_tool` | CLIENT (remote) / INTERNAL (in-process) | Span name: `execute_tool {gen_ai.tool.name}`. |
| Chat / inference | `chat` | CLIENT | Span name: `{gen_ai.operation.name} {gen_ai.request.model}`. |
| Embeddings | `embeddings` | CLIENT | |
| Retrieval | `retrieval` | CLIENT | Vector store / RAG retrieval. |
| Memory | `create_memory`, `search_memory`, `upsert_memory`, … | CLIENT / INTERNAL | Memory store ops. |

**Kind guidance** (from the spec and the agent-tracing community):
- `CLIENT` when the call crosses a process/service boundary — model
  inference, a hosted agent runtime, a remote vector store.
- `INTERNAL` for in-process work — the agent reasoning loop, a tool
  executed in your own codebase.
- `SERVER` would be appropriate for the *receiving* side of a hosted
  agent runtime that AgentCrash instruments (rare today).

### 2.4 Required and sampling-relevant attributes

Required on every GenAI inference span:
- `gen_ai.operation.name`
- `gen_ai.provider.name`

Sampling-relevant (must be set at span *creation* so a sampler can see
them, not added after the fact):
- `gen_ai.operation.name`, `gen_ai.provider.name`,
  `gen_ai.request.model`, `server.address`, `server.port`

### 2.5 Token usage attributes

| Attribute | Notes |
|---|---|
| `gen_ai.usage.input_tokens` | Includes cached tokens. |
| `gen_ai.usage.output_tokens` | Includes reasoning tokens. |
| `gen_ai.usage.cache_creation.input_tokens` | Subset of input_tokens. |
| `gen_ai.usage.cache_read.input_tokens` | Subset of input_tokens. |
| `gen_ai.usage.reasoning.output_tokens` | Subset of output_tokens (added v1.41.0). |
| `gen_ai.token.type` | **Deprecated** legacy (`input`/`output`). |

Provider quirks: for Anthropic, `input_tokens` *excludes* cached
tokens, so the instrumentation must compute
`gen_ai.usage.input_tokens = input_tokens + cache_read_input_tokens + cache_creation_input_tokens`.
When a provider reports both billed and model-consumed counts, emit
the **billed** count.

### 2.6 Well-known `gen_ai.provider.name` values

`anthropic`, `aws.bedrock`, `azure.ai.inference`, `azure.ai.openai`,
`cohere`, `deepseek`, `gcp.gemini`, `gcp.gen_ai`, `gcp.vertex_ai`,
`groq`, `ibm.watsonx.ai`, `mistral_ai`, `moonshot_ai`, `openai`,
`perplexity`, `x_ai`.

### 2.7 Events defined by the spec

- `gen_ai.client.inference.operation.details` — carries the full
  prompt/completion content (the replacement for the removed
  `gen_ai.prompt` / `gen_ai.completion` attributes).
- `gen_ai.evaluation.result` — an evaluation/evaluator span event with
  `gen_ai.evaluation.*` attributes (score, judge model, etc.).

### 2.8 Other ecosystems (interop, not reinvention)

- **OpenInference** (Arize) — stable, domain-specific attribute
  prefixes (`llm.*`, `tool.*`, `agent.*`, `retriever.*`, `chain.*`,
  `embedding.*`, `reranker.*`, `guardrail.*`, `evaluator.*`,
  `prompt.*`). Defines first-class span kinds (`LLM`, `TOOL`,
  `AGENT`, `CHAIN`, `RETRIEVER`, `EMBEDDING`, `RERANKER`, `GUARDRAIL`,
  `EVALUATOR`, `PROMPT`) which OTel GenAI does not. Auto-instrumentors
  for 30+ frameworks (LangChain, LlamaIndex, OpenAI, CrewAI, Google
  ADK, AWS Strands, …). Arize Phoenix 15.10.0+ (May 2026) auto-converts
  `gen_ai.*` → OpenInference at ingest.
- **OpenLLMetry / OpenLIT** — a shim that emits OTel-shaped traces and
  absorbs GenAI attribute renames. Recommended as an insulation layer
  while the spec is in Development.

AgentCrash should emit `gen_ai.*` as the primary, canonical form (it
is the community-default trajectory) and optionally dual-emit
OpenInference attributes for richer UI treatment in Phoenix/Langfuse.
Both run on OTel, so the convention choice shapes detail, not
portability.

---

## 3. Trace context propagation

### 3.1 W3C Trace Context (`traceparent` / `tracestate`)

The default OTel propagator serializes context via two HTTP headers
(or env-var carriers — see §3.3):

```
traceparent: <version>-<trace-id>-<parent-id>-<trace-flags>
            00 - 0af7651916cd43dd8448eb211c80319c - b7ad6b7169203331 - 01
```

- `trace-id`: 32 hex (16 bytes), all-zeros invalid.
- `parent-id` (span id): 16 hex (8 bytes), all-zeros invalid.
- `trace-flags`: 2 hex; bit 0 = `sampled`.

`tracestate` carries vendor-specific data as up to 32 comma-separated
key/value pairs. AgentCrash can use `tracestate` to carry an
`agentcrash-run-id` so downstream collectors/backend see which
AgentCrash session a trace belongs to, without putting it in baggage.

### 3.2 W3C Baggage (`baggage`)

Baggage propagates **arbitrary application key-value pairs** alongside
the trace context. It is independent of Trace Context and can be used
without tracing.

```
baggage: key1=value1;prop1, key2=value2
```

Limits: up to 64 list-members, max 8192 bytes total. **Security**:
baggage crosses trust boundaries — never put credentials or PII in
it. AgentCrash should use baggage for *non-sensitive* experiment
metadata that down-stream services (and the SUT itself) might want to
read: `agentcrash.experiment`, `agentcrash.fault`,
`agentcrash.replay_of`. For anything sensitive, use `tracestate` set
by the harness only, or attributes on root spans.

### 3.3 Environment-variable carriers (Alpha)

For non-HTTP scenarios — **batch jobs, CI, CLI tools, agent
subprocesses, MCP stdio servers** — context can be propagated via env
vars instead of headers:

| W3C header | Env var |
|---|---|
| `traceparent` | `TRACEPARENT` |
| `tracestate` | `TRACESTATE` |
| `baggage` | `BAGGAGE` |

This is the mechanism AgentCrash should use to propagate trace context
into a subprocess-based SUT (an MCP stdio server, a spawned agent
runtime) where there is no HTTP request to hang headers on. The OTel
SDK's `Baggage` and `Context` APIs expose
`propagator.inject(carrier=env)` / `extract(carrier=env)`.

### 3.4 Span links for non-tree causality

Spans normally form a tree via `parent_span_id`. **Links** (up to 128
per span) relate a span to other spans *without* parentage. They are
the OTel-native way to model:

- **Fan-out / fan-in** — one span links to the N parallel spans it
  collected.
- **Retries** — a retry span links to the prior attempt.
- **Replay / branching** (AgentCrash-specific) — a replayed span links
  to the original-run span it reproduces, and a counterfactual branch
  links to the baseline span it diverges from.

AgentCrash should use links as the primary causal glue between a
baseline run and its replays / counterfactual variants (see §6).

### 3.5 Security considerations

- **Incoming** context from untrusted sources should be sanitized or
  ignored (forged `traceparent` can DoS a tracing backend).
- **Outgoing** context to external services can leak internal
  architecture details.
- Baggage is readable (and forgeable) by every hop — keep it
  non-sensitive.

---

## 4. The Collector + exporters (OTLP)

### 4.1 What the Collector is

The OpenTelemetry Collector is a vendor-neutral proxy that receives
telemetry (OTLP, plus contrib receivers for Jaeger, Zipkin, Prometheus,
…), processes it (batching, attributes processing, tail-based sampling,
filtering, redaction), and exports it to one or more backends (OTLP,
Jaeger, Prometheus, Loki, ClickHouse, file, debug). One collector
binary, configured via YAML, with a pipeline model:

```
receivers → processors → exporters
            (with extensions: health_check, zpages, ...)
```

Two distributions:
- **Core** (`otel/opentelemetry-collector`) — receivers/exporters for
  open-source backends.
- **Contrib** (`otel/opentelemetry-collector-contrib`) — adds
  vendor-specific and specialized connectors (file exporter, clickhouse
  exporter, aws-xray, etc.). AgentCrash local dev should use contrib.

### 4.2 OTLP — the canonical wire format

OTLP is the primary protocol from SDKs to the Collector and Collector
to backends. Two transports:

| Transport | Port | Content-Type |
|---|---|---|
| gRPC | 4317 | `application/grpc` |
| HTTP | 4318 | `application/json` (protobuf-encoded JSON) on `/v1/traces`, `/v1/metrics`, `/v1/logs` |

SDKs are configured with an `OTEL_EXPORTER_OTLP_ENDPOINT` (e.g.
`http://localhost:4317` or `http://localhost:4318`). For local dev,
bind to `127.0.0.1` only.

### 4.3 Running a collector locally (Docker, contrib, debug)

Minimal `config.yaml`:

```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318

processors:
  batch:
    timeout: 1s
  memory_limiter:
    check_interval: 1s
    limit_percentage: 80
    spike_limit_percentage: 25

exporters:
  debug:
    verbosity: detailed            # prints spans/logs to collector stdout
  file/traces:
    path: /var/lib/otelcol/traces.json   # contrib file exporter
  otlp/upstream:
    endpoint: backend.example:4317
    tls:
      insecure: false

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter, batch]
      exporters: [debug, file/traces, otlp/upstream]
    logs:
      receivers: [otlp]
      processors: [memory_limiter, batch]
      exporters: [debug]
    metrics:
      receivers: [otlp]
      processors: [memory_limiter, batch]
      exporters: [debug]
  extensions: [health_check, zpages]
```

Run it:

```bash
docker run --name otel-collector \
  -p 127.0.0.1:4317:4317 \
  -p 127.0.0.1:4318:4318 \
  -p 127.0.0.1:55679:55679 \
  -p 127.0.0.1:13133:13133 \
  -v "$(pwd)/config.yaml:/etc/otelcol-contrib/config.yaml" \
  otel/opentelemetry-collector-contrib:0.149.0

# validate config without starting:
docker run --rm -v "$(pwd)/config.yaml:/etc/otelcol-contrib/config.yaml" \
  otel/opentelemetry-collector-contrib:0.149.0 validate --config=/etc/otelcol-contrib/config.yaml

# hot-reload after editing config:
docker kill --signal=SIGHUP otel-collector
```

Ports:
- **4317** OTLP gRPC, **4318** OTLP HTTP — SDKs push here.
- **55679** zPages debug UI (`/debug/tracez` for sampled trace
  inspection).
- **13133** health check.
- **8888/8889** internal metrics (collector's own telemetry).

Verify with `telemetrygen` (Go) or a `curl` to `/v1/traces`:

```bash
telemetrygen traces --otlp-insecure --endpoint localhost:4317 --traces 3
docker logs otel-collector          # see the debug exporter output
```

For AgentCrash local dev, the **contrib `file` exporter** is the most
useful sink: it writes one JSON object per span/log to disk, which is
exactly what a replay/counterfactual engine wants to consume. Pair the
collector with a small script that reads the file and loads it into
the AgentCrash store.

---

## 5. Mapping a canonical agent event model to OTel spans

Assume AgentCrash defines a canonical agent event as:

```
AgentEvent {
  event_id        : uuid
  run_id          : uuid            // one agent run = one trace
  parent_event_id : uuid | null     // span parent
  timestamp       : ns
  kind            : enum            // AGENT_TURN | LLM_CALL | TOOL_CALL
                                    // | RETRIEVAL | MEMORY | EVAL | HARNESS
  name            : string
  status          : OK | ERROR | TIMEOUT | CANCELED
  attributes      : map
  payload         : blob (prompt/completion/tool I/O)
  caused_by       : [event_id]      // non-tree causality
  agent_id        : string
  model_id        : string
  provider        : string
  tokens          : {input, output, reasoning, cache_read, cache_creation}
}
```

Mapping to OTel:

| AgentCrash field | OTel mapping |
|---|---|
| `run_id` | `trace_id` (one run = one trace). Also set `tracestate` with `agentcrash-run-id` for cross-hop correlation. |
| `event_id` | `span_id`. |
| `parent_event_id` | `parent_span_id`. |
| `kind` | Span **kind** + `gen_ai.operation.name` attribute (see §5.1). |
| `name` | `span.name` following spec templates (`{operation.name} {model}`, `execute_tool {tool.name}`). |
| `status` | `status.code` (UNSET/OK/ERROR) + `error.type` attribute. `TIMEOUT`/`CANCELED` map to `ERROR` with a specific `error.type`. |
| `attributes` (low-cardinality) | span attributes. |
| `payload` (large, sensitive) | a span **event** (e.g. `gen_ai.client.inference.operation.details` for LLM payloads, `agentcrash.tool.io` for tool I/O). Never in attributes. |
| `caused_by` (non-tree) | span **links** (one link per causal ancestor). |
| `agent_id` | `gen_ai.agent.id` (spec-recognized) + resource attribute `service.name` for the runtime. |
| `model_id` | `gen_ai.request.model` / `gen_ai.response.model`. |
| `provider` | `gen_ai.provider.name` (well-known value). |
| `tokens.*` | `gen_ai.usage.*` attributes (see §2.5). |

### 5.1 Kind → operation.name mapping

| AgentCrash `kind` | OTel span kind | `gen_ai.operation.name` |
|---|---|---|
| `AGENT_TURN` (in-process reasoning loop) | INTERNAL | `invoke_agent` |
| `AGENT_TURN` (caller side of remote agent) | CLIENT | `invoke_agent` |
| `LLM_CALL` | CLIENT | `chat` |
| `TOOL_CALL` (in-process tool) | INTERNAL | `execute_tool` |
| `TOOL_CALL` (remote tool / MCP server call) | CLIENT | `execute_tool` |
| `RETRIEVAL` | CLIENT | `retrieval` |
| `MEMORY` | CLIENT or INTERNAL | `create_memory` / `search_memory` / `upsert_memory` |
| `EVAL` | INTERNAL | (use `gen_ai.evaluation.result` event) |
| `HARNESS` (AgentCrash fault injection, replay control) | INTERNAL | (AgentCrash sidecar op — see §6) |

### 5.2 Status mapping detail

OTel `status.code` only has `UNSET`, `OK`, `ERROR`. AgentCrash richer
statuses fold into `ERROR` + `error.type`:

| AgentCrash status | `status.code` | `error.type` |
|---|---|---|
| `OK` | OK | — |
| `ERROR` | ERROR | `<exception class or fault id>` |
| `TIMEOUT` | ERROR | `timeout` |
| `CANCELED` | ERROR | `canceled` |
| (counterfactual divergence) | ERROR | `agentcrash.divergence` (custom) |

### 5.3 What goes in events, not attributes

Always events (high-cardinality / large / sensitive):
- Full prompt and completion message arrays.
- Tool input and output blobs.
- Reasoning / chain-of-thought text.
- Retrieved document chunks.
- AgentCrash snapshot diffs.

Always attributes (low-cardinality / filterable):
- `gen_ai.operation.name`, `gen_ai.provider.name`, `gen_ai.request.model`.
- Token counts, cost, latency.
- `gen_ai.tool.name`, `gen_ai.agent.id`, `gen_ai.conversation.id`.
- `agentcrash.*` experiment metadata (see §6).

---

## 6. What does NOT map cleanly to OTel — the sidecar schema

OTel is an **observability** standard: it records what happened, once,
in a tree (with links). AgentCrash is an **experimentation / chaos /
replay** platform: it needs to express *hypothetical* runs, *modified*
runs, *interventions*, and *regression expectations* — things that
never "happened" as a single observed trace. The mismatches:

### 6.1 Replay

A replay reproduces a prior run, ideally with identical inputs, to
verify determinism or to re-instrument a run that crashed before
emitting traces. OTel has no concept of "this trace is a copy of that
other trace."

- **What OTel gives you**: a link from each replayed span to the
  original span (`SpanLink` with attributes). The `trace_id` is
  different (it's a new run), so OTel backends will not treat them as
  the same trace.
- **What's missing**: a typed, queryable relationship between a
  baseline run and its replay; per-span equivalence classes; a
  deterministic-comparison result (matched / diverged / missing /
  extra).

### 6.2 Counterfactual / what-if branches

A counterfactual is a run that *deliberately* diverges from a baseline
(changed prompt, swapped model, injected fault) and must be compared
against the baseline span-by-span. OTel links can express "this span
diverged from that one" but cannot express *how* (the diff), the
intended divergence point, or the comparison verdict.

### 6.3 Interventions

An intervention is an *in-flight* modification: "at span X in run R,
replace the model's next tool call with this one." OTel records events
that happened; an intervention is a *control-plane* action that
alters the future of the run. It needs its own event type, a target
span selector, a trigger condition, and the applied mutation. The
SUT's resulting spans are observable in OTel, but the intervention
itself is a harness concept.

### 6.4 Chaos / fault injection

A chaos fault (kill a tool, inject a 500, add 5s latency, corrupt a
retrieval result) is a harness action that *causes* downstream spans
to fail. OTel can record the *effects* (failed tool span with
`error.type=timeout`), but the fault itself — what was injected,
where, when, with what severity, under which experiment id — is
metadata that belongs on a **harness span/log** correlated to the
SUT spans, not derivable from them.

### 6.5 Regression tests / expectations

A regression test asserts: "for this fixture, the agent's trace must
contain a tool call to `X` within `N` turns, and must not call `Y`."
OTel has no assertion layer. Expectations are a test artifact that
*references* OTel spans by selector; they live outside the trace.

### 6.6 Determinism / sampling

OTel's sampling model is built to *drop* spans for cost. AgentCrash
replay needs **complete** traces (head-based sampling must be 100%
for runs under test), and needs to re-derive the *same* span ids when
replaying deterministically. OTel span ids are random by default;
deterministic replay requires AgentCrash to override span id
generation — which the SDK supports via custom `Sampler` / id
generator hooks, but it is not the OTel default behavior.

### 6.7 Summary of the gap

| AgentCrash concept | OTel coverage | Needs sidecar? |
|---|---|---|
| Run / span / causality | Native (trace, span, parent, links) | No |
| Token usage, model, provider | Native (`gen_ai.*`) | No |
| Prompt/completion content | Native (span events) | No |
| Replay | Link only — no equivalence, no diff | **Yes** |
| Counterfactual | Link only — no diff, no verdict | **Yes** |
| Interventions | Not expressible | **Yes** |
| Chaos faults | Effects observable; fault metadata not | **Yes** |
| Regression expectations | Not expressible | **Yes** |
| Deterministic span ids | Possible via custom hooks, not default | **Partial** |
| Sampling control | Native, but must be forced 100% for test runs | Config |

---

## 7. Proposed adapter layer: AgentCrash Event Model ↔ OTel

The adapter is a bidirectional bridge with three lanes: **export**
(AgentCrash → OTel, for observability/interop), **import** (OTel →
AgentCrash, for capturing SUT traces), and **sidecar** (AgentCrash
concepts that have no OTel home, stored alongside but linked by
`trace_id` / `span_id`).

### 7.1 Architecture

```
            ┌─────────────────────────────────────────────────────┐
            │                    SUT (agent runtime)               │
            │  instrumented with OTel SDK (gen_ai.* + agentcrash.*)│
            └───────────────────────┬─────────────────────────────┘
                                    │ OTLP gRPC/HTTP (4317/4318)
                                    ▼
            ┌─────────────────────────────────────────────────────┐
            │           OTel Collector (contrib, local)            │
            │  receivers: otlp                                     │
            │  processors: batch, attributes/agentcrash-redaction  │
            │  exporters: file (→ AgentCrash store), debug, upstream│
            └──────────────┬──────────────────────┬────────────────┘
                           │                      │ OTLP
                           ▼                      ▼
            ┌──────────────────────┐   ┌─────────────────────────┐
            │  AgentCrash Importer  │   │  External backend       │
            │  (OTel → AC Event)    │   │  (Phoenix/Langfuse/etc) │
            └──────────┬───────────┘   └─────────────────────────┘
                       ▼
            ┌─────────────────────────────────────────────────────┐
            │              AgentCrash Event Store                  │
            │  spans (from OTel) + sidecar records (replay, cf,   │
            │  interventions, faults, expectations) keyed by      │
            │  trace_id / span_id                                  │
            └──────────┬───────────────────────────┬──────────────┘
                       │                           │
                       ▼ (export)                  ▼ (sidecar emit)
            ┌──────────────────────┐   ┌──────────────────────────┐
            │  AgentCrash Exporter  │   │  AgentCrash Sidecar      │
            │  (AC Event → OTel)    │   │  Schema (this doc §6)    │
            │  for replaying into   │   │  - ReplayRecord          │
            │  OTel-native backends │   │  - CounterfactualRecord  │
            └──────────────────────┘   │  - InterventionRecord    │
                                       │  - FaultRecord            │
                                       │  - ExpectationRecord      │
                                       └──────────────────────────┘
```

### 7.2 Export lane (AgentCrash → OTel)

Used when AgentCrash has captured a run (or synthesized a
replay/counterfactual) and wants to push it into an OTel-native
backend (Phoenix, Langfuse, Jaeger) for visualization.

- Implement an `AgentCrashOtelExporter` that produces OTLP
  `ExportTraceServiceRequest` payloads.
- Each `AgentEvent` becomes a span per §5. Use the spec span-name
  templates.
- `caused_by` becomes span links. Non-tree replay/counterfactual
  causality is preserved via links with `agentcrash.link.type =
  replay_of | counterfactual_of | intervention_by`.
- Set a **resource** attribute `service.name = "agentcrash.run.<run_id>"`
  so replays are distinguishable from original runs in backends.
- Set `tracestate` with `agentcrash-run-id` and
  `agentcrash-run-kind` (baseline | replay | counterfactual).
- For spans that originate from AgentCrash (not the SUT), set
  `agentcrash.span.origin = harness` so a backend can filter them.

### 7.3 Import lane (OTel → AgentCrash)

Used when the SUT is OTel-instrumented and AgentCrash wants to ingest
the live trace.

- Point the SUT's OTel SDK at a local collector (4317/4318). The
  collector's `file` exporter writes JSON to disk; an
  `AgentCrashIngester` tail-reads that file and upserts spans into the
  AgentCrash Event Store.
- Alternatively, the AgentCrash ingester can be an OTLP receiver
  itself (skip the collector) for lower latency in tight test loops.
  Keep the collector in the dev path anyway for its processors
  (redaction, batching, debug visibility).
- Map spans back to `AgentEvent` per §5 (inverse table). Preserve
  `trace_id` as `run_id`, `span_id` as `event_id`, links as
  `caused_by`.
- Token counts, model, provider come from `gen_ai.*` attributes; fall
  back to OpenInference `llm.*` attributes if `gen_ai.*` is absent
  (the ingester should understand both, since auto-instrumentors vary).
- Payloads come from span events (preferred) or, for older
  instrumentations, from `gen_ai.prompt`/`gen_ai.completion` legacy
  attributes (deprecated but still emitted by some libraries).

### 7.4 Sidecar schema (the parts OTel cannot hold)

All sidecar records are keyed by `(run_id, span_id)` (which are OTel
`trace_id` / `span_id`) so they join cleanly to the imported span
tree. They are stored in the AgentCrash Event Store, not emitted as
OTel signals — though each can *also* be emitted as an OTel **log
record** (correlated by `trace_id`/`span_id`) so OTel-only backends
still see them.

```
ReplayRecord {
  replay_run_id        : trace_id   // the new run
  baseline_run_id      : trace_id   // the original
  deterministic        : bool
  span_map             : map<baseline_span_id, replay_span_id>
  diff                 : list<{ span_id, kind: matched|diverged|missing|extra, detail }>
  verdict              : PASS | FAIL | INCONCLUSIVE
  comparison_criteria  : string      // e.g. "token_count_within_5pct AND same_tool_calls"
}

CounterfactualRecord {
  cf_run_id            : trace_id
  baseline_run_id      : trace_id
  divergence_point     : span_id     // where the cf deliberately branches
  mutation             : { target, op, before, after }
  diff                 : list<{...}> // same shape as ReplayRecord.diff
  verdict              : BETTER | WORSE | NEUTRAL | INCONCLUSIVE
  metrics              : map         // e.g. {tokens, cost, success_rate}
}

InterventionRecord {
  run_id               : trace_id
  target_span_selector : { run_id, span_id | turn_index | tool_name }
  trigger_condition    : { on_event: span_created | on_status: ERROR | at_turn: N }
  applied_mutation     : { replace_tool_call | inject_message | kill_span | ... }
  applied_at           : ns
  resulting_span_ids   : [span_id]   // spans the SUT produced after the intervention
  success              : bool
}

FaultRecord {
  run_id               : trace_id
  experiment_id        : string
  fault_type           : kill_tool | http_500 | latency | corrupt_retrieval | drop_message
  target               : { tool_name | provider | span_selector }
  severity             : float
  injected_at          : ns
  affected_span_ids    : [span_id]
  expected_effect      : string      // hypothesis
  observed_effect      : string      // what actually happened
}

ExpectationRecord {
  expectation_id       : uuid
  fixture_id           : string
  run_id               : trace_id    // the run under test
  assertions           : list<{
    selector   : SpanSelector,        // e.g. "kind=TOOL_CALL AND tool.name=X within N turns"
    assertion  : must_exist | must_not_exist | count_eq | attribute_eq | status_eq,
    expected   : value
  }>
  verdict              : PASS | FAIL
  failing_span_ids     : [span_id]
}
```

### 7.5 Span selectors

Both `InterventionRecord` and `ExpectationRecord` reference spans by a
**selector**. Define a small selector DSL that compiles to OTel
attribute predicates:

```
SpanSelector :=
  { kind: <AgentCrash kind>,
    operation.name: <gen_ai.operation.name>,
    tool.name: <string>,
    provider.name: <string>,
    within_turns: <int>,
    after_span: <span_id>,
    status: <OK|ERROR|TIMEOUT|...> }
```

A selector matches a span iff every specified field matches the
span's corresponding attribute. `within_turns` counts `AGENT_TURN`
ancestors. This keeps expectations/interventions portable across runs
and fixtures rather than hardcoding span ids.

### 7.6 Deterministic span ids for replay

For deterministic replay, AgentCrash must reproduce span ids across
runs. Implement a custom OTel `IDGenerator` that derives
`span_id = hash(run_seed, parent_span_id, operation.name, turn_index,
invocation_counter)`. The SDK exposes this hook in all mainstream
languages. Set `run_seed` from the `ReplayRecord.baseline_run_id` (or
a fixed fixture seed) so a replay of the same baseline yields the
same span ids, making `span_map` trivially identity. Disable this in
non-replay (live) runs so ids stay random and collision-free.

### 7.7 Sampling policy

- **Live (non-test) runs**: default OTel sampling is fine (parent
  ratio, tail-based). AgentCrash does not need every span.
- **Runs under test** (replay, counterfactual, chaos, regression):
  force `ParentBased(AlwaysOn)` so every span is recorded. Set this
  via the SDK's sampler config gated on an env var
  (`AGENTCRASH_RUN_KIND=test`). The Collector should also be told not
  to tail-sample these runs (route by `tracestate`/resource attribute
  to a non-sampling pipeline).

### 7.8 Attribute redaction

Because prompts and completions can contain PII, and because OTel
attributes are often indexed by backends, the Collector config should
include an `attributes/redaction` processor (contrib) that strips or
hashes known-sensitive `gen_ai.*` event attributes for the upstream
exporter, while the local `file` exporter (feeding AgentCrash) keeps
the full payload (AgentCrash stores payloads out-of-band, encrypted,
with access control). Never put PII in `baggage`.

### 7.9 The `agentcrash.*` attribute namespace

Reserve a private namespace for AgentCrash-specific span attributes
that *do* belong on the span (low-cardinality, filterable):

| Attribute | Values | Purpose |
|---|---|---|
| `agentcrash.run.id` | trace_id echo | redundant with trace_id but queryable as attribute |
| `agentcrash.run.kind` | baseline \| replay \| counterfactual \| chaos \| regression | run classification |
| `agentcrash.experiment.id` | string | groups a set of runs |
| `agentcrash.span.origin` | sut \| harness | did the SUT or AgentCrash emit this span |
| `agentcrash.fault.id` | string | which FaultRecord caused this span's state |
| `agentcrash.intervention.id` | string | which InterventionRecord altered this span |
| `agentcrash.link.type` | replay_of \| counterfactual_of \| intervention_by \| fault_from | disambiguates span links |
| `agentcrash.replay.match` | matched \| diverged \| missing \| extra | per-span comparison verdict |

Everything in §6's sidecar (diffs, payloads, expectations, full
mutations) stays in the Event Store, not on spans.

---

## 8. Concrete integration hooks for AgentCrash

1. **OTel SDK in the SUT** — instrument the agent runtime with
   `gen_ai.*` (primary) and `agentcrash.*` (run metadata). Pin the
   semconv library version. Centralize attribute keys in one
   constants module to absorb renames.
2. **OTLP export to local collector** — SUT SDK configured with
   `OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318`. Use env-var
   carriers (`TRACEPARENT`, `BAGGAGE`) to propagate context into
   subprocess-based SUTs (MCP stdio servers, spawned runtimes).
3. **Collector with `file` exporter** — contrib image, local config
   per §4.3. The `file` exporter's JSON output is AgentCrash's
   primary ingestion source in dev.
4. **AgentCrash Ingester** — tail-reads the collector file output,
   maps OTel spans → AgentCrash events (§5 inverse), writes to the
   Event Store alongside sidecar records.
5. **Custom `IDGenerator`** — for deterministic replay (§7.6).
6. **Custom `Sampler`** — `AlwaysOn` for test runs, default for live
   (§7.7), gated on `AGENTCRASH_RUN_KIND`.
7. **Span-event payloads** — full prompts/completions/tool I/O as
   events, never attributes (§5.3, §7.8).
8. **Span links** — `agentcrash.link.type` for replay / counterfactual
   / intervention / fault causality (§7.2, §7.9).
9. **`tracestate`** — carry `agentcrash-run-id` + `agentcrash-run-kind`
   for cross-hop correlation without baggage PII risk.
10. **`baggage`** — non-sensitive experiment metadata only
    (`agentcrash.experiment`, `agentcrash.replay_of`).
11. **OTel Logs signal** — emit harness-level events (fault injection
    notices, intervention applications, expectation verdicts) as log
    records correlated by `trace_id`/`span_id`, in addition to the
    sidecar records in the Event Store.
12. **AgentCrashOtelExporter** — re-emit stored runs (including
    synthesized replays/counterfactuals) into OTel-native backends for
    visualization (§7.2).

---

## 9. Risks and mitigations

| Risk | Mitigation |
|---|---|
| GenAI semconv is still **Development** — names will change again (v1.42.0 in flight) | Pin semconv version; centralize keys in one module; use OpenLLMetry/OpenInference as a shim; write golden-exporter tests that lock expected attribute sets. |
| `gen_ai.*` ↔ OpenInference divergence — different detail levels, different span kinds | Ingester understands both; exporter emits `gen_ai.*` primary; optionally dual-emit OpenInference for richer UI. |
| Span trees get huge (200+ spans for a 30-min run) — unreadable, slow backends | Use span events (not nested spans) for fine-grained content; consider tail-based sampling for non-test runs; group tool iterations under one `invoke_agent_internal` span with per-iteration events. |
| PII / secrets in prompts leak into indexed attributes or baggage | Redaction processor in collector for upstream; payloads as events only; never PII in baggage; AgentCrash stores payloads out-of-band with access control. |
| Deterministic span ids collide or leak across runs | Derive from `(run_seed, parent, op, counter)`; disable custom IDGenerator in live runs; keep live runs random. |
| Replay/counterfactual traces confuse OTel backends (they look like real runs) | `agentcrash.run.kind` attribute + `service.name` prefix + `tracestate` so backends can filter; consider routing to a separate collector pipeline. |
| Sampling drops spans AgentCrash needs for test verdicts | Force `AlwaysOn` sampler for test runs; disable collector tail-sampling for test pipeline. |
| Env-var context carriers are Alpha — may change | Isolate behind a small `ContextPropagator` abstraction; prefer HTTP headers when an HTTP transport exists; only use env carriers for subprocess/MCP stdio. |
| Counterfactual "verdict" semantics are domain-specific and not OTel-standardized | Keep verdicts in the sidecar `CounterfactualRecord`; do not attempt to encode BETTER/WORSE as OTel status. |

---

## 10. Sources

- OpenTelemetry — Context propagation: https://opentelemetry.io/docs/concepts/context-propagation
- W3C Trace Context (REC): https://www.w3.org/TR/trace-context-1/
- W3C Baggage (CR 2024-05-30): https://www.w3.org/TR/2024/CR-baggage-20240530/
- OTel Spec — env carriers (Alpha): https://github.com/open-telemetry/opentelemetry-specification/blob/v1.53.0/specification/context/env-carriers.md
- OTel — Install the Collector with Docker: https://opentelemetry.website.cncfstack.com/docs/collector/install/docker/
- OTel — Collector Quick Start: https://opentelemetry.website.cncfstack.com/docs/collector/quick-start/
- OneUptime — Run the Collector locally in Docker (Feb 2026): https://oneuptime.com/blog/post/2026-02-06-run-collector-locally-docker-quick-testing/view
- `open-telemetry/semantic-conventions-genai` (repo): https://github.com/open-telemetry/semantic-conventions-genai
- GenAI spans doc: https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-spans.md
- GenAI events doc: https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-events.md
- GenAI attribute registry: https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/
- GenAI spans YAML model: https://github.com/open-telemetry/semantic-conventions/blob/main/model/gen-ai/spans.yaml
- Gen α AI — Agent observability with OTel GenAI conventions: https://genalphai.com/agent-observability-with-opentelemetry-genai-conventions/
- DEV — Tracing agent tool calls to catch a stuck loop: https://dev.to/gabrielanhaia/tracing-agent-tool-calls-so-you-can-catch-a-stuck-loop-24a9
- Morphllm — Agent tracing (2026): https://www.morphllm.com/agent-tracing
- DEV — AI agent decision tracing with OTel: https://dev.to/toxsec/instrument-ai-agent-decision-tracing-with-opentelemetry-5b2k
- Effloow — OTel GenAI LLM agent tracing sandbox PoC: https://effloow.com/articles/opentelemetry-genai-llm-agent-tracing-sandbox-poc-2026
- Arize Phoenix — OpenInference semantic conventions: https://arize.com/docs/phoenix/tracing/concepts-tracing/otel-openinference/semantic-conventions
- Arize Phoenix — Translating semantic conventions: https://arize.com/docs/phoenix/tracing/concepts-tracing/translating-conventions
- Arize Phoenix — OTel GenAI auto-conversion (15.10.0+, May 2026): https://arize.com/docs/phoenix/release-notes/05-2026/05-15-2026-otel-semconv-conversion
- Arthur AI — OpenInference vs OTel GenAI for agent tracing: https://www.arthur.ai/column/openinference-vs-opentelemetry-genai-conventions-agent-tracing