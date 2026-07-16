# Model Context Protocol (MCP) — Spec Research for AgentCrash

> Verified against the official MCP specification on 2026-07-15. Covers the
> current latest stable line (`2025-06-18`) and the **`2026-07-28` release
> candidate** that ships on July 28, 2026 (the largest protocol change since
> launch). AgentCrash must understand both, because MCP servers in the wild
> will span every era for years.

---

## 1. What MCP is

MCP is a **JSON-RPC 2.0** protocol for exchanging context between LLM
applications and external tools/data sources. It defines three roles:

| Role | What it is |
|---|---|
| **Host** | The LLM application (Claude Desktop, an IDE, a custom agent runtime). Owns the user, permissions, and model access. |
| **Client** | A protocol-level connector that lives inside the host. **One client per server.** Speaks JSON-RPC to exactly one server. |
| **Server** | A process exposing tools, resources, and prompts to a client. Local subprocess or remote HTTP service. |

The host multiplexes many clients; each client talks to one server. All
messages are JSON-RPC 2.0 — requests (with `id`), notifications (no `id`),
and responses (`result` or `error`).

---

## 2. Transport options

### 2.1 stdio (all versions)
- For **local subprocess** servers. The host spawns the server process and
  communicates over **stdin/stdout** (newline-delimited JSON-RPC) and
  **stderr** (logging, free-form).
- Lifecycle: close stdin / SIGTERM / SIGKILL to shut the server down.
- This is still the dominant transport for local tool servers (filesystem,
  git, shell, DB).

### 2.2 HTTP+SSE (2024-11-05) — **deprecated**
- Two endpoints: a `GET /sse` long-lived SSE stream for server→client, and a
  `POST /messages` endpoint for client→server.
- Eligible for future removal. AgentCrash may still encounter it on older
  servers.

### 2.3 Streamable HTTP — introduced 2025-03-26, refined 2025-06-18
A **single endpoint** (e.g. `https://example.com/mcp`) that accepts both
`POST` and `GET`.

- **POST** carries all client→server JSON-RPC messages. Client sends
  `Accept: application/json, text/event-stream`. Server responds with either
  a single JSON object or opens an SSE stream scoped to that request.
- **GET** opens a server→client SSE stream for server-initiated messages
  (notifications/requests). Server may return `405` if it has nothing to push.
- **Notifications/responses** get HTTP `202 Accepted` with no body.
- **Sessions**: server *may* assign `Mcp-Session-Id` in the `InitializeResult`;
  client must echo it on all subsequent requests; `HTTP DELETE` terminates.
- **Resumability**: SSE events may carry an `id`; client resumes via
  `Last-Event-ID` header (per-stream, not cross-stream).
- **Versioning**: `MCP-Protocol-Version` header required on every request.
- **Security**: servers MUST validate `Origin`, SHOULD bind to localhost when
  local, SHOULD implement auth (DNS-rebinding mitigation).

### 2.4 Streamable HTTP — **2026-07-28 stateless rewrite** (release candidate)
The headline change: **MCP is now stateless at the protocol layer.** Every
request is self-describing; any server instance behind a load balancer can
handle any request.

Removed:
- Standalone GET SSE stream endpoint (GET/DELETE now return `405`).
- `Mcp-Session-Id` header and protocol-level sessions.
- `initialize` handshake → replaced by **`server/discover`**. Client
  info/capabilities now travel in `_meta` on every request (SEP-2575).
- `Last-Event-ID` resumability.
- `notifications/cancelled` — closing the SSE response stream *is* the
  cancellation signal.

Added/changed:
- **Routable headers** (SEP-2243): `Mcp-Method` and `Mcp-Name` are REQUIRED on
  every POST, so gateways/load balancers can route without parsing the body.
  `MCP-Protocol-Version` also required on every POST.
- **Multi Round-Trip Requests (MRTR, SEP-2322)**: server→client interactions
  (sampling, elicitation, roots) are no longer sent as separate JSON-RPC
  requests on a GET SSE stream. Instead the server returns an
  **`InputRequiredResult`** containing `inputRequests`; the client gathers
  answers and **retries the original call** with `inputResponses`.
- **Custom headers from tool params**: servers annotate tool inputSchema with
  `x-mcp-header`; clients mirror values into `Mcp-Param-{Name}` headers
  (Base64-encoded via `=?base64?...?=` for non-ASCII). Enables per-tenant /
  per-region routing and rate limiting without body inspection.
- **Header validation**: servers MUST reject header/body mismatches with
  `400 Bad Request` + JSON-RPC error `-32020` (`HeaderMismatch`).
- **W3C Trace Context** propagation in `_meta` (SEP-414) — first-class
  distributed tracing support.
- **Cache control**: `ttlMs` and `cacheScope` on list/read results (SEP-2549).
- **JSON Schema 2020-12** for tool input/output schemas (SEP-2106).
- **Extensions** become first-class (SEP-2133): reverse-DNS IDs, independently
  versioned. **Tasks** extension graduates from experimental.
- **MCP Apps** (SEP-1865): server-rendered HTML UIs in sandboxed iframes.
- **Authorization hardening**: `iss` validation (RFC 9207), `application_type`
  in DCR, credential binding to issuer.

### 2.5 Backwards compatibility
Servers may host both old and new endpoints. Clients probe by attempting a
modern POST `initialize`/`server/discover`; on `4xx` they fall back to the
prior era. Servers supporting only `2026-07-28` respond to GET/DELETE with
`405` and ignore `Mcp-Session-Id` / `Last-Event-ID`.

### 2.6 Beta SDKs for 2026-07-28
- Python v2 (`mcp==2.0.0b1`) — one endpoint answers both revisions.
- TypeScript v2 — split `@modelcontextprotocol/server` + `/client`, ESM-only,
  opt-in per request.
- Go `v1.7.0-pre.1` — `StreamableHTTPOptions.Stateless = true`.
- C# `2.0.0-preview.1` — HTTP transport defaults to stateless mode.

---

## 3. The primitive model: Tools, Resources, Prompts

A server declares **capabilities** during initialization (pre-2026-07-28) or
via `server/discover` (2026-07-28): `tools`, `resources`, `prompts`,
`logging`, `completions`. Each is optional.

### 3.1 Tools
- Capability: `tools` (optional `listChanged`).
- **`tools/list`** → paginated list of tool definitions: `name`, `description`,
  `inputSchema` (JSON Schema, 2020-12 in 2026-07-28), optional
  `annotations` (title, hints like `readOnlyHint`/`destructiveHint`,
  `x-mcp-header` param annotations in 2026-07-28).
- **`tools/call`** with `{ name, arguments }`.
- **Result**: `{ content: [text|image|audio|embeddedResource], isError: bool }`.
  Tool *execution* failures are NOT JSON-RPC errors — they are results with
  `isError: true`. This distinction matters enormously for AgentCrash (see
  §7).
- **Notifications**: `tools/list_changed` when the tool set changes (delivered
  via `subscriptions/listen` persistent SSE in 2026-07-28).

### 3.2 Resources
- Capability: `resources` (optional `subscribe`, `listChanged`).
- **`resources/list`** → list of `{ uri, name, description, mimeType }`.
- **`resources/read`** with `{ uri }` → `{ contents: [{ uri, mimeType, text | blob }] }`.
- **`resources/templates/list`** for parameterized URI templates.
- **`resources/subscribe`** + `resources/unsubscribe` for change notifications
  → `resources/updated`.
- Resources are application-controlled (the model decides when to read them,
  vs. tools which the model invokes).

### 3.3 Prompts
- Capability: `prompts` (optional `listChanged`).
- **`prompts/list`** → `{ prompts: [{ name, description, arguments }] }`.
- **`prompts/get`** with `{ name, arguments }` → `{ messages: [{ role, content }] }`
  ready to hand to a model.
- **`completion/complete`** for argument autocomplete.

---

## 4. Client-provided features (server→client requests)

### 4.1 Sampling — `sampling/createMessage`
Server asks the client/host to produce an LLM completion. Client keeps
control of model access, permissions, and human-in-the-loop approval.
Supports `modelPreferences` (cost/speed/intelligence + model hints), tool
use, multi-turn tool loops (`toolChoice`: `auto`/`required`/`none`).

- **SEP-2260 (Final, Feb 2026)**: `sampling/createMessage`, `roots/list`, and
  `elicitation/create` MUST be nested within an originating client request
  (e.g. during a `tools/call`). Standalone server-initiated requests of these
  types are forbidden. `ping` excepted.
- **SEP-2577 (Final, May 2026)**: **Sampling is DEPRECATED** as of
  `2026-07-28`. New implementations should NOT adopt it; migrate to calling
  LLM provider APIs directly. Remains in spec ≥12 months before removal
  eligibility. Wire format unchanged — deprecation is advisory.

### 4.2 Roots — `roots/list`
Client tells the server about filesystem boundaries (`file://` URIs) it
SHOULD respect. Advisory coordination, **not** a security boundary.
- **DEPRECATED** as of `2026-07-28` (SEP-2577) — vague semantics, overlaps
  with tool params / server config.

### 4.3 Elicitation — `elicitation/create`
Server requests structured info from the user (e.g. a booking confirmation)
with a JSON schema. **Not deprecated.** In 2026-07-28 this, along with
sampling and roots, is delivered via MRTR (`InputRequiredResult`) instead of
out-of-band server requests.

### 4.4 Logging — `notifications/message`
Server-emitted log entries with level. **DEPRECATED** as of `2026-07-28`
(SEP-2577) — overlaps with stderr and OpenTelemetry.

---

## 5. Lifecycle

### 5.1 Pre-2026-07-28 (initialize handshake)
1. **Initialize**: client sends `initialize` with
   `{ protocolVersion, capabilities, clientInfo }` → server responds with
   `{ protocolVersion, capabilities, serverInfo, instructions }`.
2. **Initialized**: client sends `notifications/initialized`.
3. **Operation**: normal JSON-RPC traffic respecting negotiated capabilities.
4. **Shutdown**: HTTP — close connections / `DELETE` the session; stdio —
   close stdin / SIGTERM / SIGKILL.

### 5.2 2026-07-28 (stateless)
- No `initialize`. Server metadata/capabilities arrive via **`server/discover`**.
- Client info/capabilities travel in `_meta` on every request (SEP-2575).
- No session to terminate. State is carried by explicit application handles
  (e.g. `basket_id`, `browser_id`) passed as ordinary tool arguments.
- Long-lived notifications (e.g. `tools/list_changed`) are delivered via a
  `subscriptions/listen` request whose response is a persistent SSE stream.

---

## 6. How a tool call flows (canonical trace)

```
Host (agent)            Client              Server
    │                      │                    │
    │  user turn           │                    │
    ├─────────────────────>│  tools/list        │
    │                      ├───────────────────>│
    │                      │<───────────────────│  result: [tool defs]
    │                      │  tools/call {name, │
    │                      │   arguments}       │
    │                      ├───────────────────>│
    │                      │   (SSE stream,     │
    │                      │    progress notes) │
    │                      │<───────────────────│  notifications/progress
    │                      │<───────────────────│  result: {content, isError}
    │<─────────────────────│                    │
    │  model consumes      │                    │
    │  tool result         │                    │
```

On 2026-07-28 the same logical flow, but:
- Each message is an independent POST with `Mcp-Method`, `Mcp-Name`,
  `MCP-Protocol-Version` headers.
- If the server needs user input mid-call, it returns
  `InputRequiredResult{ inputRequests }`; the client retries the *same*
  `tools/call` with `inputResponses` (MRTR) — no separate server→client
  request.
- Cancellation = close the SSE response stream.

---

## 7. How errors surface

Two distinct channels — AgentCrash must capture both.

### 7.1 Protocol errors (JSON-RPC `error`)
Standard JSON-RPC 2.0 codes:
| Code | Meaning |
|---|---|
| `-32700` | Parse error |
| `-32600` | Invalid request |
| `-32601` | Method not found |
| `-32602` | Invalid params (also: unknown tool, missing resource in 2026-07-28) |
| `-32603` | Internal error |
| `-32000 … -32099` | Server error (server-defined) |
| `-32020` | `HeaderMismatch` (2026-07-28 only) |
| `-1` | User rejected sampling/elicitation request |

These come back as `{ "error": { code, message, data } }` with the original
request `id`. A protocol error means the call never executed semantically.

### 7.2 Tool execution errors (`isError: true`)
A `tools/call` that *ran* but failed returns a normal `result` with
`isError: true` and content describing the failure. **This is not a
JSON-RPC error** — it is a successful RPC whose payload signals failure. The
host/agent decides what to do (retry, surface to the model, abort). This is
the channel for "the DB query threw", "the file doesn't exist at runtime",
"the API returned 500", etc.

### 7.3 Transport errors
- HTTP non-2xx that aren't valid JSON-RPC envelopes (e.g. `405` for GET on a
  stateless 2026-07-28 server, `400` + `-32020` for header mismatch, `404`
  for stale session id in 2025-06-18).
- Stream closed mid-response (in 2026-07-28, that's the cancellation signal).
- stdio process exit / stderr noise.

---

## 8. (1) How AgentCrash instruments MCP traffic

AgentCrash is an agent-observability + failure-replay tool. MCP traffic is a
first-class signal because every tool call, resource read, and prompt render
is a structured JSON-RPC exchange. Three instrumentation mechanisms, ranked
by fidelity:

### 8.1 Mechanism A — Client-side instrumentation (preferred, highest fidelity)
Wrap the MCP **client** inside the host/agent runtime. This is where
AgentCrash has the richest context: it sees the originating agent turn, the
prompt that led to the call, and the result the model consumed.

> **Implemented** in `agentcrash/integrations/mcp_client.py`: a
> dependency-free `MCPClientRecorder` (any client/SDK) and an optional
> `RecordingClientSession` wrapping `mcp.ClientSession`. Every interaction is
> funneled through `ctx.call_external(kind="mcp")`, so it inherits exact
> replay, counterfactuals, the analyzer's disambiguation root-cause, and
> redaction — with no core changes. `isError: true` is recorded as a returned
> value (replayed verbatim, not raised); JSON-RPC/transport errors raise and
> re-raise on replay.

- **What to observe per call**:
  - **Server identity**: `serverInfo` from `initialize` / `server/discover`
    (name, version), negotiated `protocolVersion`, transport type and
    endpoint, server capabilities.
  - **Tool name + arguments** (full `tools/call` params) — redact per policy.
  - **Resource/prompt** reads and gets, with URIs / prompt names + args.
  - **Response**: content items (text/image/audio/embedded resource),
    `isError` flag, structured content.
  - **Errors**: JSON-RPC error code + message + data, transport status
    codes, stream-close/cancellation events.
  - **Latency**: time-to-first-byte, total call duration, progress
    notification timestamps. Per-request `id` correlates request↔response.
  - **Trace context**: 2026-07-28 carries W3C Trace Context in `_meta`
    (SEP-414) — AgentCrash should propagate/emit `traceparent` so its spans
    stitch into the host's OpenTelemetry trace.
  - **MRTR round-trips** (2026-07-28): record each `InputRequiredResult` →
    `inputResponses` retry as a child span so multi-round calls reconstruct
    faithfully.
- **How**: ship a thin client wrapper / SDK shim (e.g. a drop-in
  `@agentcrash/mcp-client` that wraps `@modelcontextprotocol/client`), or a
  monkey-patch of the host's client class. Emit OpenTelemetry spans + a
  structured AgentCrash event log (JSON lines). No protocol changes needed.

### 8.2 Mechanism B — Proxy / transparent interposer (transport-agnostic)
Stand up an AgentCrash MCP **proxy** that the agent points at instead of the
real server URL (for HTTP) or that wraps the subprocess (for stdio via a
local sidecar). The proxy forwards every JSON-RPC message verbatim and
records it.

- **Pros**: works with any client, any language, zero code changes in the
  agent; captures transport-level errors (TLS, 4xx/5xx, stream closes) the
  client wrapper might miss.
- **Cons**: lacks agent-side context (which prompt triggered the call); for
  stdio, requires launching the server through the proxy; TLS/`Origin`
  validation and auth passthrough must be correct to avoid breaking
  security assumptions (especially 2026-07-28 `Mcp-Method`/`Mcp-Name` header
  routing).
- **For 2026-07-28**: the proxy must forward the routable headers
  (`Mcp-Method`, `Mcp-Name`, `MCP-Protocol-Version`, `Mcp-Param-*`) intact
  and preserve `Accept` negotiation, or gateways downstream will reject.

### 8.3 Mechanism C — Server-side instrumentation
Instrument the **server** itself (SDK middleware). Sees the call after it
arrives, before it's executed, plus internal execution timing and stderr.

- **Pros**: captures server-internal failures (DB errors, exceptions, the
  path between receiving `tools/call` and returning `isError: true`) that
  are invisible on the wire.
- **Cons**: only works for servers AgentCrash controls; third-party servers
  are opaque. Best used as a *supplement* on AgentCrash-owned reference
  servers and as the instrumentation story for AgentCrash-as-MCP-server
  (§9).

### 8.4 What to record (canonical event shape)
Every MCP interaction should produce an AgentCrash event with:
```
{
  trace_id, span_id, parent_span_id,          // OTel / W3C trace context
  timestamp_start, timestamp_ttfb, timestamp_end,
  transport: "stdio" | "streamable-http" | "http+sse",
  protocol_version: "2025-06-18" | "2026-07-28" | ...,
  direction: "request" | "response" | "notification",
  server: { name, version, endpoint, capabilities },
  method: "tools/call" | "resources/read" | ... ,
  params: { name, arguments, uri, ... },      // redacted per policy
  result: { content, isError, structuredContent },
  error: { code, message, data } | null,
  http: { status, headers_subset } | null,
  mrtr_round: 0 | 1 | 2 ...                   // 2026-07-28 multi-round
}
```
This shape is what `replay_run` and `analyze_failure` will consume.

### 8.5 Redaction & safety
Tool arguments and resource contents routinely carry secrets (API keys, PII,
file contents). AgentCrash MUST redact at the client wrapper before persist,
using a policy layer (allowlist of fields, max content size, secret
scanners). The 2026-07-28 `x-mcp-header` mechanism means sensitive values
can also appear in `Mcp-Param-*` headers — redact there too.

---

## 9. (2) AgentCrash exposed AS an MCP server

AgentCrash should ship a first-class MCP server surface so any agent/host
can introspect traces, replay failures, and generate regression tests
without leaving its tool loop. Design:

### 9.1 Server identity
```
serverInfo: { name: "agentcrash", version: "<semver>" }
capabilities: { tools: { listChanged: true }, resources: { listChanged: true, subscribe: true }, logging: {} }
protocolVersions supported: "2025-06-18", "2025-11-25", "2026-07-28"
instructions: "AgentCrash observability & replay. Use trace_search to find failures, replay_run to reproduce, analyze_failure for root cause, test_generate to mint regression tests."
```
Transport: expose **both** stdio (for local hosts like Claude Desktop / IDEs)
and **Streamable HTTP** (stateless 2026-07-28 mode, single `/mcp` endpoint,
`Mcp-Method`/`Mcp-Name` headers) for remote/CI hosts. Stateless mode fits
AgentCrash perfectly — each call is self-describing and the trace handle is
just a tool argument.

### 9.2 Tool surface

| Tool | Input | Output | Notes |
|---|---|---|---|
| `trace_search` | `{ run_id?, agent?, status?: "failed"\|"all", since?, limit?, text? }` | list of trace summaries (run_id, agent, status, error_class, ts) | Pagination via `cursor`. Replaces a flat `trace_get` for discovery. |
| `trace_get` | `{ run_id, span_filter?, include_content?: bool }` | full trace tree: spans, MCP events, model turns, tool calls, errors | `include_content=false` by default to avoid dumping huge/redacted payloads. |
| `replay_run` | `{ run_id, span_filter?, mutate?: {span_id, patch}, dry_run?: bool, record_new_run?: bool }` | replay result: matched/ diverged per span, new run_id if recorded | Re-executes recorded MCP calls against the *live* servers (or stubs). `mutate` lets the agent perturb one tool arg and observe the blast radius — core of failure reproduction. |
| `analyze_failure` | `{ run_id, depth?: "quick"\|"deep" }` | structured root-cause: failing span, error class, JSON-RPC code or `isError` flag, contributing spans, suggested fix, linked trace_search results | Returns both the JSON-RPC *protocol* error (if any) and the tool-execution `isError` narrative, since agents conflate them. |
| `test_generate` | `{ run_id, target?: "pytest"\|"vitest"\|"go", span_filter?, name? }` | generated test file(s) + expected-outcome assertions minted from the recorded trace | Mints regression tests from the golden (pre-failure) path and the failing path. |
| `diff_runs` | `{ run_id_a, run_id_b, span_filter? }` | per-span diff of args/results/errors | For "what changed between this run and the last green run". |
| `mcp_inventory` | `{ run_id }` | servers contacted, their `serverInfo`, capabilities, protocol versions, tool catalogs | Useful for env drift detection. |

All tool `inputSchema`s use **JSON Schema 2020-12** (matches 2026-07-28).
Long-running `replay_run` / `test_generate` should stream
`notifications/progress` (pre-2026-07-28) or SSE progress on the per-request
stream (2026-07-28) so the host can show progress and cancel by closing the
stream.

### 9.3 Resources (read-only views)
- `agentcrash://runs` — recent runs (paginated).
- `agentcrash://runs/{run_id}/trace` — raw trace JSON.
- `agentcrash://runs/{run_id}/tests` — generated test files.
- `agentcrash://servers` — known MCP server inventory + drift.
Subscribe-able for `listChanged` so the host refreshes when new runs land.

### 9.4 Prompts (opinionated workflows)
- `agentcrash:debug_last_failure` — renders a prompt that loads the most
  recent failed run's trace + `analyze_failure` output and asks the model to
  propose a fix.
- `agentcrash:regression_pack` — renders a prompt that calls `test_generate`
  for a run and produces a PR-ready test bundle.
- `agentcrash:compare_runs` — renders a prompt feeding `diff_runs` output for
  triage.

### 9.5 Error surface from the AgentCrash server
- Unknown run_id → JSON-RPC `-32602` (Invalid params), matching 2026-07-28's
  consolidated missing-resource code.
- Replay target unreachable / server stub missing → result with
  `isError: true` + diagnostic content (it's a *tool execution* failure, not
  a protocol error — semantically correct and lets the agent reason about
  retry).
- Replay divergence is **not an error** — it's a normal result whose payload
  describes which spans diverged.
- Auth/permission denied → `-32001` (server-defined) with `data.scope`.

### 9.6 Security & multi-tenancy
- Stateless 2026-07-28 mode: tenant/project carried as an explicit tool
  argument (`run_id` is globally unique and already scopes everything) — no
  session state needed.
- Expose `x-mcp-header` on `run_id` so hosts can route per-tenant via
  `Mcp-Param-run-id` headers if fronted by a gateway.
- Require `Origin` validation on the HTTP transport; recommend localhost bind
  for the stdio-spawned case.

### 9.7 Observability of the observer
The AgentCrash MCP server should emit W3C Trace Context (SEP-414) for its own
tool calls and log via stderr (pre-deprecation) / OTel (2026-07-28 path) so
AgentCrash can observe itself without infinite regress.

---

## 10. Version summary table

| Spec version | Status | Transport highlights | Notable |
|---|---|---|---|
| `2024-11-05` | Deprecated | stdio, HTTP+SSE (two-endpoint) | Original. |
| `2025-03-26` | Stable | stdio, Streamable HTTP introduced | Single endpoint, sessions. |
| `2025-06-18` | Stable | Streamable HTTP refined, OAuth 2.1 | `Mcp-Session-Id`, `Last-Event-ID`. |
| `2025-11-25` | Draft | Elicitation added | |
| `2026-07-28` | RC → final Jul 28 2026 | **Stateless** Streamable HTTP, `server/discover`, MRTR, routable headers, Trace Context, deprecates Roots/Sampling/Logging | Largest change since launch. |

---

## 11. AgentCrash action list

1. Support **both** `2025-06-18` (session + GET SSE) and `2026-07-28`
   (stateless) when instrumenting — the wild population will be mixed for
   12–24 months.
2. Build the **client-side wrapper** first (Mechanism A) — it's where the
   agent-turn context lives and where `isError` vs JSON-RPC error is easiest
   to disambiguate.
3. Add the **proxy** (Mechanism B) for language-agnostic / no-code-change
   capture; forward 2026-07-28 routable headers intact.
4. Record the **canonical event shape** (§8.4) including MRTR round counters
   and W3C trace context.
5. Ship the **AgentCrash MCP server** (§9) on stdio + stateless Streamable
   HTTP, with the tool surface in §9.2.
6. Distinguish **protocol errors** (JSON-RPC `error`) from **tool execution
   errors** (`isError: true`) everywhere — in the event schema, in
   `analyze_failure` output, and in generated tests.
7. Redact tool args + `Mcp-Param-*` headers before persist.
8. Propagate W3C Trace Context (SEP-414) so AgentCrash spans stitch into
   host/agent OTel traces.

---

## Sources

- [Streamable HTTP — MCP Specification (Draft / 2026-07-28)](https://modelcontextprotocol.io/specification/draft/basic/transports/streamable-http)
- [The 2026-07-28 MCP Specification Release Candidate — MCP Blog](https://blog.modelcontextprotocol.io/posts/2026-07-28-release-candidate/)
- [Beta SDKs for the 2026-07-28 MCP Spec Release Candidate — MCP Blog](https://blog.modelcontextprotocol.io/posts/sdk-betas-2026-07-28/)
- [Architecture Overview — MCP](https://modelcontextprotocol.io/docs/learn/architecture)
- [Transports — MCP Specification (2025-03-26)](https://modelcontextprotocol.io/specification/2025-03-26/basic/transports)
- [Transports — MCP Specification (2025-06-18)](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)
- [Lifecycle — MCP Specification (2025-06-18)](https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle)
- [Tools — MCP Specification (2025-03-26)](https://modelcontextprotocol.io/specification/2025-03-26/server/tools)
- [Client Concepts — MCP](https://modelcontextprotocol.io/docs/learn/client-concepts)
- [Sampling — MCP Specification (draft)](https://modelcontextprotocol.io/specification/draft/client/sampling)
- [SEP-2260: Require Server requests to be associated with a Client request](https://modelcontextprotocol.io/seps/2260-Require-Server-requests-to-be-associated-with-Client-requests)
- [SEP-2577: Deprecate Roots, Sampling, and Logging (PR)](https://github.com/modelcontextprotocol/specification/pull/2577)
- [MCP Spec 2025-06-18: Streamable HTTP & OAuth 2.1 — The Stack Dispatch](https://blogs.abhipanseriya.dev/blog/mcp-became-a-remote-first-protocol-the-spec-changed-underneath-you)