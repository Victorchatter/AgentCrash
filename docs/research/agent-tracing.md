# Agent Execution Tracing — Best Practices & v1 Canonical Schema

Research notes for AgentCrash's tracing layer. Distills the state of the art (mid-2026) in GenAI/agent observability into a concrete v1 event schema and event-type taxonomy AgentCrash should implement.

**Status:** research / proposal — 2026-07-15
**Audience:** AgentCrash tracing-layer implementers

---

## 1. Landscape & standards we should align with

Three converging efforts define the canonical shape of agent telemetry today:

1. **OpenTelemetry GenAI semantic conventions** (`open-telemetry/semantic-conventions-genai`). Defines `gen_ai.*` spans for inference, embeddings, retrieval, memory, `execute_tool`, `create_agent`, `invoke_agent`, `invoke_workflow`, `plan`. Still *Development* status as of v1.41; opt in via `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental`. Two event types are standardized: `gen_ai.client.inference.operation.details` (opt-in, full I/O) and `gen_ai.evaluation.result` (recommended, eval scores).
2. **OpenInference** (Arize Phoenix). Stable, OTel-native, richer auto-instrumentor ecosystem. 11 span kinds: `LLM`, `CHAIN`, `AGENT`, `TOOL`, `RETRIEVER`, `EMBEDDING`, `RERANKER`, `GUARDRAIL`, `EVALUATOR`, `PROMPT`, `UNKNOWN`. Expected to converge with `gen_ai.*`. Use OpenInference naming for new instrumentation today; map to `gen_ai.*` on export.
3. **MCP trace-context propagation (SEP-414, Final).** W3C Trace Context (`traceparent` / `tracestate` / `baggage`) carried in `params._meta` of every MCP JSON-RPC request, because MCP is transport-agnostic (stdio has no headers; Streamable HTTP multiplexes many requests per connection). OTel semantic conventions for MCP define `mcp.method.name`, `mcp.session.id`, `mcp.protocol.version`, `jsonrpc.request.id`, with `gen_ai.operation.name=execute_tool` and `gen_ai.tool.name` on tool calls.

AgentCrash should emit OTel-compatible traces (so they can be shipped to Jaeger/Tempo/Honeycomb/Datadog/Phoenix unchanged) and consume MCP `traceparent` propagation so subprocess MCP servers attach under the correct parent span.

**Hierarchy:** `Session` → `Trace` → `Span` → `Span Events`. Sessions group multi-turn agent runs; a trace is one end-to-end invocation; spans are steps; events are point-in-time observations parented to a span (token-by-token deltas, eval scores, safety flags).

---

## 2. What to capture per event

### 2.1 Identity & nesting
Every record carries:
- `trace_id` — root correlation ID (W3C 32-hex). One per end-to-end agent invocation.
- `span_id` — 16-hex, unique within the trace.
- `parent_span_id` — establishes the tree. Root span has none.
- `span_link` — *non-parent* causal link (e.g. an MCP server span links back to the transport HTTP span even though its parent is the MCP client span). Use links for fan-in, retries, and transport boundaries.
- `session_id` — groups traces into a conversation/run.
- `schema_version` — see §9.

### 2.2 Timing
- `start_time_unix_ns`, `end_time_unix_ns` (spans).
- `timestamp_unix_ns` (events — point-in-time).
- Derived downstream: `duration_ms`, `time_to_first_token_ms` (LLM spans), `queue_wait_ms`.
- For streaming LLM calls: emit a `gen_ai.token` span event per chunk (or aggregate), record TTFT and inter-chunk gaps.

### 2.3 Actor
A typed `actor` field (single enum) on every span, independent of span *kind*:
- `agent` — an autonomous loop making decisions
- `llm` — a model inference
- `tool` — a function/MCP/filesystem/shell/browser call
- `user` — a human input or approval
- `system` — runtime/orchestrator (scheduler, memory store, sampler)
- `evaluator` — a judge scoring an output

The OpenInference `span.kind` covers *what* the span is; `actor` covers *who* initiated it. Keep both — they answer different questions in the UI.

### 2.4 Inputs / outputs
- `input.value` + `input.mime_type` (e.g. `application/json`, `text/plain`).
- `output.value` + `output.mime_type`.
- For LLM spans specifically: structured `llm.input_messages` / `llm.output_messages` (message-role array) **in addition to** flat `input/output.value`, so the UI can render chat correctly. Carry `llm.model_name`, `llm.provider`, `llm.invocation_parameters`, `llm.token_count.{prompt,completion,total}`, `llm.cost.*`.
- Content capture is **opt-in**, gated by a config flag (mirrors `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`). Off by default in production to bound storage and limit PII exposure.

### 2.5 Status / errors
- `status` enum: `ok`, `error`, `unset` (OTel-compat).
- On error: `exception.type`, `exception.message`, `exception.stacktrace` (as a span event, not an attribute — see §7).
- `error.type` — domain-specific (`tool_error`, `timeout`, `rate_limit`, `context_length_exceeded`, `policy_violation`, `parse_error`). MCP uses `error.type=tool_error` when `CallToolResult.isError` is true.

### 2.6 Artifacts
Side-effects produced or consumed by the span: files written, screenshots, HTTP bodies, generated images. Never inline; see §7.

- `artifacts[]`: `{ role: "produced"|"consumed", uri, content_type, size_bytes, sha256 }`.

### 2.7 Retries & parallelism
- **Retries:** do not collapse into one span. Emit one child span per attempt with `attempt_number` (1-indexed) and `retry_reason`. Link attempts together via `span_link` with `link.type="retry"` and the previous attempt's `span_id`.
- **Parallelism:** concurrent siblings share a parent and have overlapping `[start,end]` windows. Add an optional `sibling_group_id` to make fan-out explicit (e.g. parallel tool calls in a single LLM turn). Record `concurrency` on the parent (count of concurrent children).
- **Self-correction / loops:** emit one span per iteration; tag `iteration_number` and `termination_reason` on the loop span.

### 2.8 Parent-child nesting
- Tree structure via `parent_span_id`. Always spawn child spans under the active span context so the tree is correct by construction.
- Propagate context across process/transport boundaries via W3C `traceparent` (HTTP headers; `params._meta` for MCP).
- Use **span links** (not parent edges) for causality that isn't ownership: retries, fan-in merges, transport-layer spans for an MCP call.

---

## 3. Observable model output vs. private chain-of-thought

This is a policy decision, not just a schema decision. AgentCrash's stance:

### Policy
1. **Never record hidden reasoning.** Raw model chain-of-thought is not reliably faithful (Anthropic's own research: models often decide based on factors not in the visible thinking, so monitoring it cannot make strong safety arguments), exposes jailcraft surface area, and creates incentive distortion if models know they're being watched.
2. **Record only what the API exposes as observable output:** the final assistant message, tool-call requests, tool results, and (when the provider returns it) the *summarized* thinking the provider chose to surface.
3. **Treat `redacted_thinking` / encrypted `signature` blocks as opaque pass-through.** Store the signature for multi-turn continuity if needed; never attempt to decrypt, parse, or log the encrypted `data` field. Log a redacted placeholder only: `{ "type": "redacted_thinking", "redacted": true, "reason": "provider_safety_redaction" }`.
4. **Respect provider display modes.** On newer Claude models `display` defaults to `omitted`; do not override to `summarized` just to populate a trace. If the user explicitly opts into summarized thinking for debugging, record it under a clearly-marked `llm.thinking_summary` field, separate from `llm.output_messages`, with a `visibility: "provider_summarized"` tag.

### Schema representation
- `llm.output_messages[]` — observable output (assistant text, tool_use blocks). Always safe to record when content capture is on.
- `llm.thinking_summary` (optional, opt-in) — provider-surfaced summary only. Marked `visibility: "provider_summarized"`.
- `llm.thinking_redacted_count` — integer count of redacted blocks, no content. Safe to record always.
- `llm.thinking_signature_ref` — artifact URI to an opaque blob *only if* the user explicitly opts in to storing it for replay; otherwise omit entirely. Never inline.
- No field named `chain_of_thought`, `reasoning`, `inner_monologue`, or similar should ever exist in the schema. This is a deliberate non-field.

### Rationale
Aligns with Anthropic's trajectory (visible → summarized → omitted-by-default) and avoids the failure mode where a tracing tool becomes the vector by which hidden reasoning leaks into logs, dashboards, or exported datasets.

---

## 4. Representing specific action types

All of the below are spans (or span events when they're point observations) with `actor` set appropriately and a typed `kind`. The taxonomy in §6 enumerates them.

### 4.1 Tool calls (function calling)
- `kind=TOOL`, `actor=tool`.
- `tool.name`, `tool.id`, `tool.description`, `tool.parameters` (JSON), `tool.json_schema` (the call's declared schema).
- `gen_ai.operation.name=execute_tool` for interop.

### 4.2 MCP calls
- Client span: `kind=TOOL`, `span_kind=CLIENT`, named `{mcp.method.name} {target}`.
- Server span (in the MCP server process): `span_kind=SERVER`, parented via `traceparent` extracted from `params._meta`.
- Attributes: `mcp.method.name`, `mcp.session.id`, `mcp.protocol.version`, `jsonrpc.request.id`, `gen_ai.tool.name`, `network.transport` (`stdio`|`http`).
- If outer agent instrumentation already wraps the tool execution, MCP instrumentation adds attributes to the existing span rather than creating a duplicate (per OTel MCP conventions).
- Error: `error.type=tool_error` when `isError=true`; span status `ERROR`.

### 4.3 Filesystem ops
- `kind=TOOL`, `tool.name` ∈ {`fs.read`,`fs.write`,`fs.list`,`fs.delete`,`fs.mkdir`,`fs.move`}.
- `fs.path` (absolute, post-symlink-resolution), `fs.bytes`, `fs.mode`.
- **Path normalization is security-sensitive:** record the resolved absolute path and flag sandbox escapes. Never record file *contents* inline — emit an artifact reference with `sha256` for produced/consumed blobs; content capture stays opt-in and is subject to allow/deny rules (e.g. never capture `~/.ssh/*`, env files).

### 4.4 Shell commands
- `kind=TOOL`, `tool.name="shell.exec"`.
- `shell.command` (full argv string), `shell.argv[]` (structured), `shell.cwd`, `shell.exit_code`, `shell.signal`, `shell.duration_ms`.
- ** stdout/stderr are large and sensitive:** truncate to a configurable cap (default 4 KiB each) with `shell.stdout.truncated=true` and `shell.stdout.size_bytes` set; spill full output to an artifact and record `shell.stdout.ref.uri`.
- Record `shell.env_redacted[]` — names of env vars that were set but whose values were redacted.

### 4.5 Browser actions
- `kind=TOOL`, `tool.name` ∈ {`browser.navigate`,`browser.click`,`browser.type`,`browser.snapshot`,`browser.screenshot`,`browser.evaluate`}.
- `browser.url`, `browser.target` (selector/ref), `browser.action`, `browser.viewport`.
- Screenshots/DOM snapshots are artifacts, never inline (see §7). Record `browser.snapshot.ref.uri` + `content_type=image/png` or `text/html`.

### 4.6 HTTP
- `kind=TOOL` (for agent-initiated) or `kind=CHAIN` (for infra).
- OTel HTTP conventions: `http.request.method`, `url.full`, `http.response.status_code`, `network.transport`.
- Request/response bodies: opt-in, size-capped, artifact-spilled via the blob-reference pattern.

### 4.7 Memory
- `kind=CHAIN` or a dedicated `MEMORY` kind (OpenInference doesn't have one; OTel GenAI defines memory operations). Operations: `create_memory_store`, `search_memory`, `create_memory`, `update_memory`, `upsert_memory`, `delete_memory`.
- Attributes: `memory.store.id`, `memory.operation`, `memory.query`, `memory.result_count`, `memory.score[]`.
- Stored memory content is itself sensitive (may contain PII or reasoning-derived facts) — apply the same redaction policy as messages.

### 4.8 Retrieval (RAG)
- `kind=RETRIEVER`. `retrieval.query`, `retrieval.documents[]` (each: `id`, `score`, `content_ref.uri` — content is an artifact), `retrieval.top_k`.
- Add `reranker` spans (`kind=RERANKER`) when a second-stage reorder happens.

### 4.9 Human approval / input
- `kind=CHAIN`, `actor=user`.
- `approval.request` (what was asked), `approval.decision` ∈ {`approved`,`rejected`,`modified`,`timeout`}, `approval.user_id`, `approval.latency_ms`.
- Modification path: when a human edits a proposed action, record the original as an artifact and the edited version as the consumed input of the next span; link them with `link.type="human_edit"`.

---

## 5. Large payloads, truncation, and artifacts

Principles (drawn from OTel spec + community guidance):

1. **Span attributes are for indexing, not payload storage.** Keep attributes small and filterable. Default `AttributeValueLengthLimit` of 2048 bytes is a reasonable cap.
2. **Large content goes in span events or external blob storage.** Use the **blob-reference pattern**: replace an inlined `${prefix}` with `artifacts[].uri` (or `${prefix}.ref.uri` + `${prefix}.ref.content_type`) pointing at S3/GCS/local object store.
3. **Always record metadata even when content is spilled:** `size_bytes`, `sha256`, `content_type`, `truncated` (bool), `original_size_bytes`.
4. **Truncation must be explicit:** set `*.truncated=true` and keep `*.size_bytes` = original size so consumers know what was dropped. Truncation semantics vary across SDKs — AgentCrash should do truncation at the source rather than rely on backend.
5. **PII/sensitivity deny-list applied before spill:** redact secrets, SSH keys, env files, `.env*`, `*token*`, `*secret*`, `*password*` paths/keys. Redaction is recorded (`redacted_fields[]`) so the trace is auditable without exposing the data.
6. **Set SDK env caps at source:** `OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT=128`, `OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT=2048`, `OTEL_SPAN_EVENT_COUNT_LIMIT=128`.

---

## 6. v1 canonical event schema (concrete)

### Envelope (every record)
```
{
  "schema_version": "1.0.0",          // SemVer; see §9
  "record_type": "span" | "event",
  "trace_id": "<32 hex>",
  "span_id": "<16 hex>",
  "parent_span_id": "<16 hex>|null",
  "span_links": [ { "span_id": "...", "trace_id": "...", "type": "retry|transport|human_edit|fan_in" } ],
  "session_id": "<string>",
  "timestamp_unix_ns": <int>,
  "start_time_unix_ns": <int>,         // spans only
  "end_time_unix_ns": <int>,           // spans only
  "actor": "agent|llm|tool|user|system|evaluator",
  "kind": "LLM|CHAIN|AGENT|TOOL|RETRIEVER|EMBEDDING|RERANKER|GUARDRAIL|EVALUATOR|PROMPT|MEMORY|UNKNOWN",
  "name": "<human-readable span/event name>",
  "status": "ok|error|unset",
  "attributes": { ... },               // kind-specific, see below
  "events": [ ... ],                   // spans only; child point-events
  "artifacts": [ { "role":"produced|consumed", "uri":"...", "content_type":"...", "size_bytes":<int>, "sha256":"...", "truncated":<bool> } ],
  "metadata": { }                      // free-form JSON bag: user.id, tags, feature flags, request_id
}
```

### Error event (parented to the failing span)
```
{
  "schema_version": "1.0.0",
  "record_type": "event",
  "trace_id": "...", "span_id": "...", "parent_span_id": "<failing span>",
  "timestamp_unix_ns": <int>,
  "name": "exception",
  "attributes": {
    "exception.type": "<class>",
    "exception.message": "<string>",
    "exception.stacktrace": "<string|artifact_ref>",
    "error.type": "tool_error|timeout|rate_limit|context_length_exceeded|policy_violation|parse_error|...",
    "error.retryable": <bool>
  }
}
```

### LLM span (kind=LLM, actor=llm)
```
"attributes": {
  "gen_ai.operation.name": "chat|text_completion|generate_content|embeddings",
  "gen_ai.provider.name": "anthropic|openai|...",
  "gen_ai.request.model": "...",
  "gen_ai.response.model": "...",
  "llm.invocation_parameters": { "temperature": 0.2, "max_tokens": 4096, "thinking": {"display":"omitted"} },
  "llm.input_messages": [ {"role":"system","content": "..."}, ... ],   // opt-in
  "llm.output_messages": [ {"role":"assistant","content":"...", "tool_calls":[...]} ], // opt-in
  "llm.thinking_summary": "...",        // opt-in, visibility=provider_summarized
  "llm.thinking_redacted_count": 0,     // always safe
  "llm.token_count.prompt": <int>,
  "llm.token_count.completion": <int>,
  "llm.token_count.total": <int>,
  "llm.cost.amount": <float>,
  "llm.cost.currency": "USD",
  "gen_ai.client.time_to_first_token_ms": <int>     // streaming only
}
```
No `chain_of_thought` / `reasoning` / `inner_monologue` field exists. Ever.

### Tool span (kind=TOOL, actor=tool)
```
"attributes": {
  "gen_ai.operation.name": "execute_tool",
  "tool.name": "fs.write|shell.exec|browser.navigate|mcp.call|http.request|...",
  "tool.id": "<call_id>",
  "tool.parameters": { ... },            // structured; large fields artifact-spilled
  "tool.json_schema": { ... },           // optional
  // kind-specific sub-attributes per §4 (fs.*, shell.*, browser.*, mcp.*, http.*)
  "attempt_number": 1,
  "retry_reason": null
}
```

### Agent span (kind=AGENT, actor=agent)
```
"attributes": {
  "agent.name": "...",
  "agent.version": "...",
  "graph.node.id": "...", "graph.node.name": "...", "graph.node.parent_id": "...",
  "iteration_number": <int>,            // for looped agents
  "termination_reason": "complete|max_iterations|error|user_cancel|policy_stop",
  "concurrency": <int>                  // count of concurrent children spawned
}
```

### Memory span (kind=MEMORY)
```
"attributes": {
  "gen_ai.operation.name": "search_memory|create_memory|upsert_memory|...",
  "memory.store.id": "...",
  "memory.query": "...",
  "memory.result_count": <int>,
  "memory.score": [0.81, 0.73, ...]
}
```

### Retrieval span (kind=RETRIEVER)
```
"attributes": {
  "retrieval.query": "...",
  "retrieval.top_k": 5,
  "retrieval.documents": [ {"id":"...","score":0.81,"content_ref":{"uri":"...","sha256":"..."}} ]
}
```

### Human approval event
```
{
  "record_type": "event",
  "name": "human.approval",
  "actor": "user",
  "attributes": {
    "approval.request": "...",                 // what was asked
    "approval.decision": "approved|rejected|modified|timeout",
    "approval.user_id": "...",
    "approval.latency_ms": <int>,
    "approval.original_ref": {"uri":"..."},    // if modified
    "approval.modified_ref":  {"uri":"..."}
  }
}
```

### Eval event (kind=EVALUATOR)
```
"attributes": {
  "gen_ai.evaluation.name": "faithfulness|groundedness|toxicity|...",
  "gen_ai.evaluation.score.value": <float>,
  "gen_ai.evaluation.score.label": "pass|fail",
  "gen_ai.evaluation.explanation": "..."
}
```

---

## 7. Event-type taxonomy (v1)

| kind | actor | when | notes |
|---|---|---|---|
| `AGENT` | agent | one per autonomous loop / graph node | wraps LLM + tool children |
| `LLM` | llm | one per model inference (incl. embeddings→use `EMBEDDING`) | opt-in content; never hidden CoT |
| `TOOL` | tool | function/MCP/fs/shell/browser/HTTP call | `execute_tool` interop |
| `CHAIN` | system\|agent | structural grouping, orchestration step | carries only common attrs + metadata |
| `RETRIEVER` | system | RAG retrieval | documents as artifacts |
| `RERANKER` | system | second-stage reorder | |
| `EMBEDDING` | llm | embedding model call | |
| `MEMORY` | system | memory store CRUD/search | sensitive content — redact |
| `GUARDRAIL` | system | safety/policy/PII check | may block downstream span |
| `EVALUATOR` | evaluator | judge scoring an output | gen_ai.evaluation.result |
| `PROMPT` | system | prompt template render | capture template + vars |
| `UNKNOWN` | * | fallback | |

Span *events* (point-in-time, parented to a span): `exception`, `gen_ai.token` (streaming chunk), `human.approval`, `gen_ai.client.inference.operation.details` (opt-in full I/O spill), `gen_ai.evaluation.result`, `artifact.produced`, `artifact.consumed`.

---

## 8. Parallelism, retries, loops — representation rules

- **One span per attempt.** `attempt_number` increments; `span_link{type:"retry"}` chains attempts.
- **Concurrent siblings** share a parent; overlapping `[start,end]` windows encode parallelism. Optional `sibling_group_id` makes fan-out explicit. Parent records `concurrency`.
- **Loops** emit one child span per iteration; parent loop span carries `iteration_number`-less metadata and `termination_reason`.
- **Cross-process causality** uses span *links* (not parent edges) — e.g. an MCP server span's parent is the MCP client span; the underlying HTTP transport span is a `link{type:"transport"}`.

---

## 9. Schema versioning

Adopt the OTel / event-schema-evolution consensus:

1. **Carry `schema_version` (SemVer `MAJOR.MINOR.PATCH`) on every record.** Producers set it; consumers read it first and route.
2. **Consumers MUST ignore unknown fields** (forward-compatible reads). Producers MUST NOT rename/remove/retype within a major.
3. **Additive = MINOR** (new optional field, new enum variant, new kind). **Breaking = MAJOR** (rename, remove, retype, *semantic shift* of an existing field — the silent killer). **Fix/clarification = PATCH.**
4. **Provide forward-only upcasters** `V1→V2→…→Vn` (pure functions, no I/O) retained as long as old records exist in storage; retirement gated on TTL.
5. **Schema files are immutable and cacheable**; publish them to a registry for mechanical compatibility checks.
6. **Never change a field's semantics without a MAJOR bump.** A field that means "assistant message text" today cannot silently come to mean "assistant message text including redacted reasoning" tomorrow.
7. v1 of this schema is `1.0.0`. The next additive change (e.g. a new `kind`) is `1.1.0`. A rename of `llm.output_messages` is `2.0.0` with a published upcaster and a ≥6-month overlap window.

---

## 10. Concrete recommendations for AgentCrash

- **Emit OTel-compatible spans** with OpenInference `span.kind` names plus the `actor` extension; map to `gen_ai.*` on export via a transform processor. This maximizes backend portability.
- **Implement MCP `traceparent` propagation** in any AgentCrash MCP client (inject into `params._meta` per SEP-414) and in any MCP server it hosts (extract and parent). This fixes the cross-process trace-breaking problem by construction.
- **Content capture off by default**; a single config flag turns it on with per-kind overrides (capture LLM messages but not shell stdout, etc.). Always apply PII deny-list before spill.
- **Blob-reference pattern mandatory for:** file contents, shell stdout/stderr, HTTP bodies, browser screenshots/DOM, retrieval document contents, large tool parameters (>2 KiB). Inline only metadata + sha256.
- **Hidden-reasoning policy baked into the schema:** there is no field for raw chain-of-thought. Only `llm.thinking_summary` (opt-in, provider-surfaced) and `llm.thinking_redacted_count` (always-safe integer). Redacted/encrypted blocks are stored as opaque artifacts only when the user opts in, never parsed.
- **Record `schema_version=1.0.0` on every record** from day one; publish the schema file alongside the codebase.
- **Hybrid instrumentation:** manual `AGENT`/`CHAIN` spans for business logic, auto-instrumented `LLM`/`TOOL` children for detail. Both nest via shared OTel context.

---

## Sources

- [OpenTelemetry GenAI semantic conventions repo](https://github.com/open-telemetry/semantic-conventions-genai)
- [GenAI spans spec](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-spans.md)
- [GenAI events spec](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-events.md)
- [Greptime: How OTel traces LLM calls, agent reasoning, and MCP tools (May 2026)](https://greptime.com/blogs/2026-05-09-opentelemetry-genai-semantic-conventions)
- [OpenInference semantic conventions — Arize Phoenix](https://arize.com/docs/phoenix/tracing/concepts-tracing/otel-openinference/semantic-conventions)
- [OpenInference best practices](https://arize.com/docs/phoenix/cookbook/tracing/openinference-best-practices)
- [gen_ai.* metric RFC — Phoenix Discussion #13041](https://github.com/Arize-ai/phoenix/discussions/13041)
- [MCP SEP-414: Trace Context Propagation Conventions](https://modelcontextprotocol.io/seps/414-request-meta)
- [OTel semantic conventions for MCP](https://github.com/open-telemetry/semantic-conventions/blob/main/docs/gen-ai/mcp.md)
- [mcp-otel TypeScript bridge](https://github.com/studiomeyer-io/mcp-otel)
- [MCP Python SDK OTel PR #2381](https://github.com/modelcontextprotocol/python-sdk/pull/2381)
- [Google Cloud Trace — monitor MCP tool use](https://docs.cloud.google.com/mcp/monitor-mcp-tool-use-with-cloud-trace)
- [Anthropic — Extended thinking docs](https://platform.claude.com/docs/en/build-with-claude/extended-thinking)
- [Anthropic Research — Claude's extended thinking](https://www.anthropic.com/research/visible-extended-thinking)
- [AWS Bedrock — Thinking encryption](https://docs.aws.amazon.com/bedrock/latest/userguide/claude-messages-thinking-encryption.html)
- [OTel blob-reference attribute proposal — Issue #1428](https://github.com/open-telemetry/semantic-conventions/issues/1428)
- [OTel attribute limits spec](https://github.com/open-telemetry/opentelemetry-specification/blob/v1.8.0/specification/common/common.md)
- [Grafana Cloud — reduce trace size](https://grafana.com/docs/grafana-cloud/send-data/traces/configure/reduce-trace-size/)
- [OTEP-0152 — Telemetry schemas](https://github.com/open-telemetry/opentelemetry-specification/blob/main/oteps/0152-telemetry-schemas.md)
- [Event schema evolution without downtime](https://www.aidonow.com/articles/patterns/event-schema-evolution-without-downtime)
- [SpanForge schema versioning guide](https://www.getspanforge.com/docs/schema-versioning)