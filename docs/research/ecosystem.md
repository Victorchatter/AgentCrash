# AI Agent Ecosystem Map (2026)

Research snapshot of the current (mid-2026) landscape of **AI agent frameworks** and **autonomous coding agents**, classified by primary instrumentation surface. Verified via web search (June-July 2026); landscape changes fast so dates are noted.

Goal of this doc for AgentCrash: identify the stable, structured-event surfaces we can record/replay/analyze across heterogeneous agents — and the divergence points we must normalize.

---

## TL;DR classification

Two orthogonal axes matter for AgentCrash:

1. **Extension mechanism** — how you inject/observe behavior (SDK callbacks vs. out-of-process hooks vs. OTel auto-instrumentation vs. log/trajectory parsing vs. none).
2. **Structured-event emission** — does the tool emit typed, machine-readable event records on a documented surface, and is that surface stable?

Cross-cutting standards converging in 2026:
- **OpenTelemetry GenAI semantic conventions** (`open-telemetry/semantic-conventions-genai`, split out May 2026) — spans: `invoke_agent`, `invoke_workflow`, `execute_tool`, `generate_content {model}`; events: `gen_ai.client.inference.operation.details`, `gen_ai.evaluation.result`. Still **Development status** (not stable), opt-in content capture.
- **OpenInference** (Arize) — *stable*, broader auto-instrumentor coverage, `llm.*`/`tool.*`/`agent.*` span kinds. Expected to converge with GenAI semconv.
- **agent-trace** (`cursor/agent-trace`, RFC 0.1.0 Jan 2026) — vendor-neutral **code-attribution** JSON spec (which lines were AI-written), VCS-anchored. Complementary to runtime OTel, not competing. RFC #6 proposes an OTel mapping.
- **MCP (Model Context Protocol)** — universal tool surface; many agents expose hook-handler MCP servers and emit MCP tool events.

Nearly every modern framework and coding agent now converges on the **OTel GenAI semconv span hierarchy** (`invoke_agent` → `invoke_workflow`/`generate_content` → `execute_tool`) plus a **lifecycle hook system** with stdin-JSON/stdout-JSON contract and exit-code 2 = block. This convergence is the single most important finding for AgentCrash's design.

---

## PART A — AI Agent Frameworks

### A1. OpenAI Agents SDK (Python, TypeScript)
- **Language:** Python (`openai-agents`), TypeScript.
- **Instrumentation surface:** Two SDK callback layers — `RunHooks` (run-wide: `on_agent_start/end`, `on_llm_start/end`, `on_tool_start/end`, `on_handoff`) and `AgentHooks` (per-agent). Plus a **built-in tracing system** on by default: `trace()`, `agent_span()`, `generation_span()`, `function_span()`, `guardrail_span()`, `handoff_span()`, `custom_span()`. Custom exporters via `TracingProcessor` (`on_trace_start/end`, `on_span_start/end`).
- **Structured events:** Yes — typed spans with parent/child hierarchy. Trace processors are the integration point.
- **OTel:** Not native; 30+ third-party backends (Datadog, Langfuse, LangSmith, Phoenix, W&B, MLflow, Braintrust, Logfire, AgentOps, Portkey, HoneyHive, PostHog) via custom trace processors.
- **Stability:** High. Documented public API (`Runner.run(..., hooks=...)`, `RunConfig.tracing_disabled`). Tracing default-on with env kill-switch `OPENAI_AGENTS_DISABLE_TRACING=1`.
- **AgentCrash hook:** Implement a `TracingProcessor` + a `RunHooks`/`AgentHooks` subclass. Cleanest in-process surface of any framework.

### A2. Anthropic Claude Agent SDK (Python, TypeScript)
- **Language:** Python (`claude-agent-sdk`, Py≥3.10), TypeScript (`@anthropic-ai/claude-agent-sdk`).
- **Instrumentation surface:** **Hooks** configured via `options.hooks` / `ClaudeAgentOptions`. Events: `PreToolUse`, `PostToolUse`, `SessionStart`, `SessionEnd`, `Setup`, `Stop`, `SubagentStart/Stop`, `Notification`, `PermissionRequest`, `UserPromptSubmit`, `MessageDisplay`, `PostToolBatch`, `PreCompact`, `PostCompact`, `TeammateIdle`, `TaskCompleted`, `WorktreeCreate/Remove`, `ConfigChange`, `CwdChanged`, `FileChanged`, `InstructionsLoaded`, `Elicitation`. Matchers filter by tool name. Hooks return `hookSpecificOutput` (`permissionDecision: allow|deny|ask|defer`, `updatedInput`, `updatedToolOutput`, `additionalContext`).
- **Structured events:** Yes — `Session.stream()` emits `task_started`, `task_progress`, `task_notification`, `session_state_changed`, plus opt-in hook lifecycle events `hook_started`/`hook_progress`/`hook_response` (`includeHookEvents`). Structured outputs via `outputFormat` JSON Schema (Zod/Pydantic).
- **OTel:** Indirect; `prompt_id` field correlates hooks with OpenTelemetry `prompt.id` attributes (v2.1.196+).
- **Stability:** High and actively evolving (docs reference runtime v2.1.195/208, indexed June 2026). Some hooks TS-only.
- **AgentCrash hook:** Best-in-class. Subclass hooks for control, or stream `Session.stream()` for pure observation. Same event taxonomy as Claude Code (below) — shared design.

### A3. LangGraph (Python, JS)
- **Language:** Python, JS/TS.
- **Instrumentation surface:** Callbacks API refactored April 2026 (PR #7473) to **typed event payload objects** (`GraphInterruptEvent`, `GraphResumeEvent`) replacing dicts. `GraphCallbackHandler` receives one typed event. New **`stream_events(version='v3')`** (PR #7677, May 2026, langgraph 1.2.0a1) with content-block protocol (start/delta/finish) and channels: `values`, `updates`, `messages`, `tools`, `lifecycle`, `checkpoints`, `custom`. Extensible via `StreamTransformer`.
- **Structured events:** Yes — typed projections + channels, very rich.
- **OTel:** Via `openinference-instrumentation-langchain` → OpenInference OTel spans (every node/LLM/tool = span). Portable to Phoenix, Grafana Tempo, any OTLP backend. Also official `opentelemetry-instrumentation-langchain` migrating to `opentelemetry-python-genai` repo (mid-2026).
- **Stability:** Medium-high. v3 streaming is alpha (1.2.0a1) — expect changes. Callbacks refactor is GA. v1.x langchain-core dependency.
- **AgentCrash hook:** Subscribe to `stream_events(v3)` for the richest live event stream; add an OTel processor for span capture.

### A4. LangChain (Python, JS)
- **Language:** Python, JS/TS.
- **Instrumentation surface:** Classic callback handlers (`BaseCallbackHandler`) with `on_chain_*`, `on_llm_*`, `on_chat_model_*`, `on_tool_*`, `on_retriever_*`, `on_agent_action/finish`, `on_custom_event`. `BaseTracer` aggregates; LangSmith tracer is the first-party subclass.
- **Structured events:** Yes — callback events with `run_id`/`parent_run_id` reconstructing span trees.
- **OTel:** `openinference-instrumentation-langchain` (Arize, stable, broad) or official `opentelemetry-instrumentation-langchain` (migrating to `opentelemetry-python-genai`, PR #4449 closed May 2026 in favor of new repo). PR added `invoke_workflow`/`invoke_agent` span classification.
- **Stability:** Medium. Callback API is mature but being layered under OTel; the OTel instrumentation is mid-migration. GenAI semconv still Development status.
- **AgentCrash hook:** OTel auto-instrumentor is the lowest-friction path; callback handlers for custom logic. **Implemented** (dependency-free, duck-typed — no `langchain_core` required): `agentcrash/integrations/langchain.py` `AgentCrashCallbackHandler` records both LangChain and LangGraph via the shared callback system; observational events (`replay=None`), redacted at record time. Replay needs the LLM-wrapper variant (future).

### A5. CrewAI (Python)
- **Language:** Python.
- **Instrumentation surface:** Singleton **event bus** (`CrewAIEventsBus`) emitting ~60+ typed Pydantic event classes across Crew/Agent/Task/Tool/LLM/Flow/Human-in-loop/Memory/Knowledge/Reasoning/Planner/MCP/A2A/Guardrail categories. `BaseEventListener` + `@crewai_event_bus.on(EventType)`. Enterprise **webhook streaming** (AMP) delivers structured JSON `{id, execution_id, timestamp, type, data}` with `realtime: true`.
- **Structured events:** Yes — the most exhaustive typed event catalog of any framework here.
- **OTel:** Dual-layer: `TraceCollectionListener` (high-level) + native **OpenTelemetry** spans (`Crew execution`, `Task execution`, `Agent execution`, `LLM call`) via `SafeOTLPSpanExporter` (fault-tolerant). Kill switches `OTEL_SDK_DISABLED`, `CREWAI_DISABLE_TELEMETRY`. Integrations: Langfuse, Portkey, Weave, Langtrace, AgentOps, OpenLIT, Datadog, Phoenix, MLflow.
- **Stability:** High. v1.15.2 (July 2026), 213 releases, deeply integrated. Production-grade.
- **AgentCrash hook:** Register a `BaseEventListener` — richest structured event surface in the framework ecosystem.

### A6. Microsoft AutoGen / Agent Framework (Python, .NET)
- **Language:** Python (AutoGen), .NET (Microsoft Agent Framework / MAF). (Note: "Microsoft Agent Framework" is the .NET successor/rebrand; AutoGen is the Python line — both from Microsoft, converging.)
- **Instrumentation surface:** Native **OpenTelemetry**. AutoGen Python: runtime/tools/agents instrumented (`SingleThreadedAgentRuntime`, `GrpcWorkerAgentRuntime`, `BaseTool` `execute_tool`, `BaseChatAgent` `create_agent`/`invoke_agent`). Disable via `AUTOGEN_DISABLE_RUNTIME_TRACING=true`. MAF .NET: `UseOpenTelemetry()` auto-registers `Experimental.Microsoft.Agents.AI*` activity sources; `OpenTelemetryAgent` auto-wraps chat client; spans `invoke_agent`, `chat`, `execute_tool`; metrics `gen_ai.client.operation.duration`, `gen_ai.client.token.usage`, `agent_framework.function.invocation.duration`. One-line config.
- **Structured events:** Partial — spans + metrics per GenAI semconv v1.37. **Gap:** MAF does NOT emit `ActivityEvent` objects (e.g. `gen_ai.client.inference.operation.details`) — Issue #3637 (Feb-May 2026) closed stale; events are spec-optional ("MAY").
- **OTel:** Native, first-class. GenAI semconv v1.37 (experimental).
- **Stability:** Medium-high. Semconv experimental; event emission gap is a real limitation for time-series event analysis.
- **AgentCrash hook:** OTel processor for spans/metrics. No structured discrete-event stream — must derive events from span boundaries.

### A7. Google ADK (Python, Go, Kotlin)
- **Language:** Python (`google-adk`, v1.17.0+), Go v1.0.0, Kotlin v0.1.0.
- **Instrumentation surface:** Two layers. (1) **Events** are the fundamental unit of information flow — immutable `google.adk.events.Event` (extends `LlmResponse` with `author`, `invocation_id`, `id`, `timestamp`, `actions`, `branch`). `session.events` = full chronological log; `invocation_id` correlates. (2) **Tracing** built on OTel GenAI semconv: spans `invoke_agent`, `invoke_workflow`, `execute_tool`, `generate_content {model}`. Programmatic via `telemetry.maybe_set_otel_providers()` or CLI `adk web --otel_to_cloud`.
- **Structured events:** Yes — `Event` objects are first-class, immutable, timestamped, with state deltas and control signals (`transfer_to_agent`, `escalate`). Excellent.
- **OTel:** Native, OTLP wire format. PII controls: `ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS`, `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=EVENT_ONLY`.
- **Stability:** High. Copyright 2026 Google, v1.17.0. Semconv v1.37. Cleanest "events + traces" dual model.
- **AgentCrash hook:** Subscribe to `session.events` stream for structured events; add OTel exporter for spans. Event objects are the primary replay substrate.

### A8. LlamaIndex (Python, TS)
- **Language:** Python (primary), TS.
- **Instrumentation surface:** `instrumentation` module (replacing legacy `callbacks` since v0.10.20): `Event` (point in time), `EventHandler`, `Span`, `SpanHandler`, `Dispatcher` (hierarchical, logging-style). Workflows emit internal events (`StepStateChanged` PREPARING→RUNNING→NOT_RUNNING) via `handler.stream_events(expose_internal=True)` — also used for durable-workflow checkpointing. Custom events subclass `BaseEvent`.
- **Structured events:** Yes — typed events + spans, dispatcher hierarchy.
- **OTel:** `llama-index-observability-otel` first-party (`LlamaIndexOpenTelemetry`): all workflow steps, LLM calls, custom events → OTel spans. Integrations: Phoenix/LlamaTrace, Langfuse, Opik, SigNoz, MLflow (≥2.18 AgentWorkflow), W&B Weave, Literal, OpenLIT, Langtrace.
- **Stability:** Medium-high. instrumentation module is the active system; legacy callbacks deprecated but not removed.
- **AgentCrash hook:** Register an `EventHandler` + `SpanHandler`; or use the OTel connector for auto-capture.

### A9. PydanticAI (Python)
- **Language:** Python.
- **Instrumentation surface:** **`Instrumentation` capability** (PR #4967, merged May 2026) — moved from hard-wired `Agent(instrument=...)` (deprecated) to first-class capability using the hook pipeline: `wrap_run`, `wrap_model_request`, `wrap_tool_execute`, `wrap_output_process`. `InstrumentationSettings` (tracer_provider, meter_provider, include_content, version 2-5 default 5). `InstrumentedModel` wraps models.
- **Structured events:** Spans for agent runs / model requests / tool exec / output processing; control-flow exceptions (`CallDeferred`, `ApprovalRequired`, `ToolRetryError`) specially handled per v5 contract. Metrics: `gen_ai.client.token.usage`, `operation.cost`, `gen_ai.client.operation.time_to_first_chunk`.
- **OTel:** Native, via capabilities. Exportable to any OTel backend (Logfire built on OTel). Data format versions 2-5 (v5 current).
- **Stability:** Medium-high. Capability refactor is fresh (May 2026); `InstrumentedModel` slated for eventual removal.
- **AgentCrash hook:** Add an `Instrumentation` capability or wrap with a custom `wrap_*` hook. OTel processor for spans.

### A10. Semantic Kernel (Python, .NET; Java not yet)
- **Language:** C#/.NET (primary), Python.
- **Instrumentation surface:** Three **Filters** (the hook mechanism, docs updated May 2026): `IFunctionInvocationFilter`, `IPromptRenderFilter`, `IAutoFunctionInvocationFilter`. Python: `kernel.add_filter(...)` or `@kernel.filter`. C#: DI `AddSingleton<IFunctionInvocationFilter,...>` or `kernel.FunctionInvocationFilters.Add(...)`. Filters see args, result, duration, token usage, rendered prompt, chat history.
- **Structured events:** Logs (ILogger), metrics (Meter histograms: function duration, streaming duration, token usage), traces (`ActivitySource` `Microsoft.SemanticKernel` / `.Planning`). Sensitive data at Trace/Debug level only. `TelemetryWithFilters.cs` sample recreates full telemetry from filters.
- **OTel:** Native-compatible (logs/metrics/traces follow OTel). GenAI semconv experimental. Sensitive-data switch `Microsoft.SemanticKernel.Experimental.GenAI.EnableOTelDiagnostics=true` (C#).
- **Stability:** High. Enterprise-ready, Microsoft-supported. Java observability pending.
- **AgentCrash hook:** Implement all three Filters — gives function/prompt/auto-call visibility. Plus OTel processor for spans/metrics.

### A11. Haystack (deepset) (Python)
- **Language:** Python.
- **Instrumentation surface:** `Tracer` / `Span` interfaces. Enable via connector components (`OpenTelemetryConnector`, `DatadogTracer`, `LangfuseTracer`, `WeaveTracer`, `MLflowTracer`, `LoggingTracer`) or `haystack.tracing.enable_tracing(...)`. Tracing **never automatic**. Content tracing opt-in via `HAYSTACK_CONTENT_TRACING_ENABLED=true` (set before import). Custom tracer subclasses `Tracer`. `include_outputs_from` param for intermediate outputs.
- **Structured events:** Spans (component execution); no first-class discrete-event bus. LoggingTracer for real-time flow inspection.
- **OTel:** `opentelemetry-haystack` package; deeper via `opentelemetry-instrumentation-openai-v2`.
- **Stability:** Medium. Tracing opt-in and manual; span-level only (no typed event catalog). Versions 2.26-2.32-unstable.
- **AgentCrash hook:** Implement a custom `Tracer` (or LoggingTracer) to capture span pipeline. No event-bus surface — span-centric.

### A12. smolagents (HuggingFace) (Python)
- **Language:** Python.
- **Instrumentation surface:** `step_callbacks` param on `MultiStepAgent` — `list[Callable]` or `dict[Type[MemoryStep], Callable]`, called each step with `(memory_step, agent)`. Can mutate memory between steps. `agent.memory.steps` (list of `TaskStep`/`ActionStep`/`PlanningStep`) for inspection; `agent.replay()` for pretty replay; `agent.run(stream=True)` yields steps.
- **Structured events:** Step-level (memory steps are typed), not fine-grained per-LLM-call events. `get_full_steps()` returns full dicts incl. model input messages.
- **OTel:** Via `openinference-instrumentation-smolagents` (`SmolagentsInstrumentor`) → Phoenix/MLflow/Langfuse. Cost/latency/user-feedback scoring via backends.
- **Stability:** Medium. Step callbacks + memory inspection stable; OTel is community/OpenInference. v1.26.0.
- **AgentCrash hook:** `step_callbacks` for live step events; `agent.memory.steps` for replay; OpenInference instrumentor for OTel spans.

### A13. Mastra (TypeScript)
- **Language:** TypeScript (Node.js; Apache-2.0, ~26K stars).
- **Instrumentation surface:** Typed **streaming events** from `.stream()`: `start`, `step-start`, `text-delta`, `tool-call`, `tool-result`, `step-finish`, `finish`; network/foreach variants. **Tool Hooks** (blog Jul 14 2026) observe/control every tool call. **Custom Signal Providers** (`@mastra/core@1.42.0+`, Jul 9 2026) — `SignalProvider` with `.start/.stop/.poll/.handleWebhook/.notify/.watch/.unwatch()` for external-event-driven agents (Postgres LISTEN, webhooks).
- **Structured events:** Yes — typed streaming events; tool hooks for tool-call observation.
- **OTel:** Bidirectional **`@mastra/otel-bridge`** (Mastra↔OTel, GenAI semconv v1.38.0, span names `chat {model}`, `execute_tool {tool_name}`, `invoke_agent {agent_id}`, W3C trace context propagation) + **`@mastra/otel-exporter`** (HTTP/gRPC to Datadog/NewRelic/SigNoz/MLflow/etc.). Exports traces + logs correlated by traceId/spanId.
- **Stability:** Medium-high. Active weekly releases (Jul 2026 blogs). OTel bridge/exporter documented and versioned.
- **AgentCrash hook:** Subscribe to `.stream()` events; register tool hooks; or attach the OTel exporter to an AgentCrash collector.

### A14. Vercel AI SDK (TypeScript)
- **Language:** TypeScript (`ai`, `@ai-sdk/otel`).
- **Instrumentation surface:** `registerTelemetry(new OpenTelemetry())` once at startup → **all AI SDK calls emit telemetry by default**; per-call opt-out `telemetry:{isEnabled:false}`. `Telemetry` interface lifecycle methods: `onStart`, `onStepStart`, `onLanguageModelCallStart/End`, `onToolExecutionStart/End`, `onStepEnd`, `onEmbedEnd`, `onRerankEnd`, `onEnd`, `onAbort`, `onError`. `enrichSpan` for custom attrs. Node.js `ai:telemetry` diagnostics channel (`AI_SDK_TELEMETRY_TRACING_CHANNEL`) for provider subscription without registration.
- **Structured events:** Span hierarchy (added step-level span Apr 2026, commit 152c67c): `invoke_agent {modelId}` (root, INTERNAL) → `step {n}` → `chat {modelId}` (CLIENT) + `execute_tool {toolName}` (INTERNAL). Supplemental AI SDK attrs opt-in (Apr 2026, commit 18651f6): usage, providerMetadata, embedding, reranking, runtimeContext, headers, toolChoice, schema.
- **OTel:** Native, primary. GenAI semconv (`gen_ai.*`). `LegacyOpenTelemetry` (`ai.*`) being deprecated.
- **Stability:** High. v7 docs. Most mature TS OTel story.
- **AgentCrash hook:** Implement `Telemetry` interface OR run an OTel collector. Diagnostics channel is a zero-registration observation point — ideal for AgentCrash.

### A15. DSPy (Stanford) (Python)
- **Language:** Python.
- **Instrumentation surface:** `BaseCallback` (`dspy/utils/callback.py`) with hooks: `on_module_start/end`, `on_lm_start/end`, `on_adapter_format_start/end`, `on_adapter_parse_start/end`, `on_tool_start/end`, `on_evaluate_start/end`. Set via `dspy.configure(callbacks=[...])` (global) or per-module. `ACTIVE_CALL_ID` ContextVar links start/end across async. `dspy.inspect_history(n)` prints recent LLM calls. `dspy.settings.trace` list captures steps.
- **Structured events:** Callback events are typed by hook; not a rich event catalog. Caveat: don't mutate inputs in-place.
- **OTel:** Via `openinference-instrumentation-dspy` (`DSPyInstrumentor`) → Phoenix/any OTLP. MLflow `mlflow.dspy.autolog()` (recommended primary). W&B Weave, LangWatch (DSPy-specific optimization visualizer), Langtrace, Langfuse, OpenLIT.
- **Stability:** Medium. Callback system stable; OTel is OpenInference/community. Optimization-run tracing (compile) off by default due to volume.
- **AgentCrash hook:** `BaseCallback` subclass for module/LM/tool/eval events; OpenInference instrumentor for OTel spans.

---

## PART B — Autonomous Coding Agents

Coding agents share a strikingly consistent hook contract in 2026: **stdin JSON in, stdout JSON out, exit 2 = block**, configured via `hooks.json` at user/project/enterprise levels, with `conversation_id`/`session_id`/`generation_id` as correlation keys and `transcript_path` pointing at a replayable log. AgentCrash should target this shared contract.

### B1. Claude Code (Anthropic)
- **Language/runtime:** Node/TypeScript CLI + Python/TS SDK.
- **Instrumentation surface:** **27+ hook events** (SessionStart/End, Setup, UserPromptSubmit, Stop, StopFailure, UserPromptExpansion, PreToolUse, PostToolUse, PostToolUseFailure, PostToolBatch, PermissionRequest/Denied, SubagentStart/Stop, TaskCreated/Completed, TeammateIdle, Notification, MessageDisplay, PreCompact/PostCompact, ConfigChange, CwdChanged, FileChanged, WorktreeCreate/Remove, Elicitation/ElicitationResult, InstructionsLoaded). **5 handler types**: `command`, `http`, `mcp_tool`, `prompt`, `agent`. Matchers by tool name.
- **Structured events:** Yes — structured JSON I/O; common input fields `session_id`, `prompt_id`, `transcript_path`, `cwd`, `permission_mode`, `effort.level`, `hook_event_name`. `prompt_id` (v2.1.196+) correlates with OTel `prompt.id`.
- **OTel:** Indirect via `prompt_id` correlation + analytics events. Telemetry captures original tool output **before** PostToolUse (so `updatedToolOutput` doesn't change telemetry).
- **Stability:** High. Documented, versioned, actively growing (32+ events per third-party refs). The reference design other coding agents emulate.
- **AgentCrash hook:** Register command/http hooks for the full event set; read `transcript_path` for replay. Best-documented surface in the coding-agent space.

### B2. OpenAI Codex CLI
- **Language/runtime:** Rust CLI + IDE extension.
- **Instrumentation surface:** **11 hook events** in two tiers: **Interception** (v0.128+, can block/rewrite): `SessionStart`, `PreToolUse`, `PermissionRequest`, `PostToolUse`, `UserPromptSubmit`, `Stop`. **Observation** (v0.133, 21 May 2026, read-only, async): `SubagentStart`, `SubagentStop`, `ToolExecution`, `TurnMetadata`, `AsyncApproval`. Plugin manifest declares `capabilities:["lifecycle_events"]`. Config: `~/.codex/hooks.json`, `config.toml`, project `.codex/hooks.json` (trust required, hash-pinned), plugin-bundled, enterprise managed (`requirements.toml`, MDM).
- **Structured events:** Yes — stdin JSON (common fields `session_id`, `transcript_path`, `cwd`, `hook_event_name`, `model`, turn-scoped `turn_id`, `permission_mode`), stdout JSON + exit codes (0 success, 2 block). `TurnMetadata` payload includes `input_tokens`, `cached_input_tokens`, `output_tokens`, `reasoning_output_tokens`, `turn_duration_ms`, `tools_executed` — ideal for cost dashboards.
- **OTel:** Native via `[otel]` config (PR #2103, Sep 2025): exporter `otlp-http`/`otlp-grpc`/`none`; events `codex.api_request`, `codex.sse_event`, `codex.user_prompt`, `codex.tool_decision`, `codex.tool_result`, `codex.hooks.run`; metrics `codex.hooks.run`, `codex.hooks.run.duration_ms` (PR #18026, Apr 2026). `log_user_prompt=false` by default.
- **Stability:** Medium-high. Interception hooks GA; observation events **beta** (payloads may change). Observation events do NOT fire for Codex cloud tasks (local/IDE only). `PreToolUse` doesn't intercept all shell calls or WebSearch.
- **AgentCrash hook:** Register observation hooks for telemetry + interception hooks for control; enable `[otel]` exporter pointed at AgentCrash's collector. TurnMetadata is the cost/latency goldmine.

### B3. Cursor
- **Language/runtime:** IDE (TypeScript) + cloud agents.
- **Instrumentation surface:** **12+ hook events** (`.cursor/hooks.json` at project/user/team/enterprise). Agent: `sessionStart/End`, `preToolUse/postToolUse/postToolUseFailure`, `subagentStart/Stop`, `beforeShellExecution/afterShellExecution`, `beforeMCPExecution/afterMCPExecution`, `beforeReadFile/afterFileEdit`, `beforeSubmitPrompt`, `preCompact`, `stop` (with `followup_message` auto-continue capped by `loop_limit` default 5), `afterAgentResponse/afterAgentThought`. Tab: `beforeTabFileRead/afterTabFileEdit`. App: `workspaceOpen`. Command or prompt-based hooks; `failClosed` for security gates; matchers.
- **Structured events:** Yes — common envelope: `conversation_id`, `generation_id`, `model`, `model_id`, `model_params`, `hook_event_name`, `cursor_version`, `workspace_roots`, `user_email`, `transcript_path`. `conversation_id` is the join key.
- **OTel:** Via community exporters: `last9/cursorscope` (Node, one-command, GenAI semconv OTLP) and `o11y-dev/opentelemetry-hooks` (Python `otel-hook`, multi-agent incl. Cursor). Both map hooks → OTel spans correlated by `conversation_id`/`generation_id`: session span → prompt span → tool spans → subagent spans.
- **Stability:** Medium-high. Cloud agent support landed Cursor 3.11 (Jul 10 2026) — conversation-level hooks now run in cloud VMs (gaps: `sessionStart/End`, `beforeMCPExecution`, Tab hooks, `workspaceOpen` don't run in cloud; user-level hooks don't run in cloud VMs).
- **AgentCrash hook:** Commit `.cursor/hooks.json` with observers on `beforeSubmitPrompt`/`afterAgentThought`/`afterAgentResponse`/`subagentStart`/`stop`, each appending JSONL keyed by `conversation_id`. Cursor also authored **agent-trace** (code attribution).

### B4. Windsurf / Codeium
- **Language/runtime:** IDE (Cascade agent).
- **Instrumentation surface:** **12 Cascade hook events**: pre `pre_read_code`, `pre_write_code`, `pre_run_command`, `pre_mcp_tool_use`, `pre_user_prompt`; post `post_read_code`, `post_write_code`, `post_run_command`, `post_mcp_tool_use`, `post_cascade_response`, `post_cascade_response_with_transcript`, `post_setup_worktree`. System/user/workspace JSON levels merged. Common input: `agent_action_name`, `trajectory_id`, `execution_id`, `timestamp`, `model_name`, `tool_info`. Enterprise MDM/Ansible deploy.
- **Structured events:** Yes — trajectory-correlated JSON. Transcripts at `~/.windsurf/transcripts/{trajectory_id}.jsonl`.
- **OTel:** Community: `o11y-dev/opentelemetry-hooks` PR #78 (Windsurf uses Antigravity-compatible protocol, `~/.codeium/windsurf/settings.json`); `git-ai` PR #621 (Mar 2026) parses JSONL transcripts. Grafana via WindMetric exporter (tails `~/.windsurf/metrics/sessions.log`, Prometheus port 9100).
- **Stability:** Medium. Model name historically "unknown" in hooks (PR #621 hardcoded). Native Prometheus endpoint in private alpha. Hook event set stable.
- **AgentCrash hook:** Configure Cascade hooks writing JSONL by `trajectory_id`; parse transcripts for replay. Same contract shape as Cursor.

### B5. Cline
- **Language/runtime:** VS Code extension (TypeScript).
- **Instrumentation surface:** Internal typed **OpenTelemetry events** (opt-in). Event namespaces: `user.*`, `task.*`, `workspace.*`, `ui.*`, `hooks.*`, `worktree.*`, `host.*`, `cline.test.*`. Rich task events: `task.created/completed`, `task.conversation_turn`, `task.tokens`, `task.tool_used` (with `auto_approved`, `duration_ms`, `success`), `task.mcp_tool_called`, `task.browser_tool_start/end`, `task.terminal_execution`, `task.subagent_started/completed`, `task.skill_used`, `task.summarize_task`, `task.ai_output.accepted/rejected` (lines added/deleted/changed), `task.provider_api_error`, `task.diff_edit_failed`. Hooks events: `hooks.execution` (unified, `status` started/completed/failed/cancelled, `duration_ms`, `context_modified`).
- **Structured events:** Yes — log events + metrics (counters/histograms/gauges) via `TelemetryService` with category-level enable/disable and `safeCapture()` (telemetry errors never break execution). Metrics: `cline.hooks.executions.total`, `cline.hooks.duration.seconds`, `cline.hooks.failures.total`, `cline.hooks.cancellations.total`, `cline.hooks.context_modifications.total`.
- **OTel:** Native — `OpenTelemetryTelemetryProvider` uses `MeterProvider` + `LoggerProvider`, lazy instruments, dot-notation flattening, circular-ref guards. Privacy: paths/commands/branches **hashed**, user IDs anonymized, errors truncated 500 chars. Datadog/Grafana-Loki/NewRelic/BigQuery/ClickHouse query examples shipped.
- **Stability:** High. Documented enterprise monitoring page, source-backed. SDK core telemetry at `sdk/packages/core/src/services/telemetry/core-events.ts`.
- **AgentCrash hook:** Enable Cline's OTel export → AgentCrash collector. Among the richest event catalogs of the coding agents, and privacy-hashing is already done.

### B6. Roo Code (rebranding to Zoo Code)
- **Language/runtime:** VS Code extension (TypeScript).
- **Instrumentation surface:** Typed internal event system `RooCodeEventName` (Zod-validated) via IPC API: `TaskCreated`, `TaskStarted/Completed/Aborted/Focused/Unfocused/Active/Interactive/Resumable/Idle`, `TaskPaused/Unpaused`, `TaskSpawned`, `TaskDelegated/DelegationCompleted/Resumed`, `Message`, `TaskModeSwitched`, `TaskAskResponded`, `TaskUserMessage`, `TaskTokenUsageUpdated`, `TaskToolFailed`, `ModeChanged`, `ProviderProfileChanged`, `CommandsResponse`, `ModesResponse`, `ModelsResponse`, `EvalPass/Fail`.
- **Structured events:** Yes — typed payload tuples via IPC. TelemetryService singleton (PostHog `PostHogTelemetryClient`, `ph.roocode.com`, `distinctId=vscode.env.machineId`) captures task/LLM/mode/tool/checkpoint/context/error/UI events. PR #12281 (May 2026) added Zoo Code backend LLM telemetry (provider/model/mode/tokens/cost, auth-gated, fire-and-forget). **Excludes** `TASK_MESSAGE`/`LLM_COMPLETION` from PostHog; strips git repo props.
- **OTel:** **Not yet.** Issue #11185 (Feb 2026, open) requests OpenTelemetry-compatible tracing (MLflow/Langfuse replay); maintainer clarified agent-trace ≠ runtime observability. Community wants it; not implemented.
- **Stability:** Medium. IPC event enum stable; OTel export is the biggest gap. PostHog is product analytics, not distributed tracing.
- **AgentCrash hook:** Subscribe to IPC `RooCodeEventName` stream; for now must bridge to OTel ourselves (Roo won't). A clear integration opportunity for AgentCrash.

### B7. Aider
- **Language/runtime:** Python CLI.
- **Instrumentation surface:** No public hook system. Existing **PostHog analytics** (`aider/analytics.py`, opt-in, 10% deterministic sampling, `~/.aider/analytics.json`): events `launched`, `cli session`/`gui session`, `message_send` (prompt/completion/total tokens, cost, total_cost), `message_send_starting`, `command_*`, `repo`/`no-repo`, `exit`, `auto_commits`. `--analytics-log FILENAME` for local JSONL. `--analytics-posthog-project-api-key`/`--analytics-posthog-host` for custom PostHog.
- **Structured events:** Analytics events (not lifecycle hooks). Token/cost computed internally per `message_send`.
- **OTel:** **Not implemented.** Issue #4360 (Jul 2025, open) requests OTel (`session.count`, `lines_of_code.count`, `cost.usage`, `token.usage`, etc.). PR #4731 (open, Jan 2026) adds LangSmith observability (tracer context manager, SQLite metrics at `~/.aider/observability.db`, cost calc, `--disable-observability`/`--langsmith-project`) — ~3.2ms overhead, but unmerged with review issues.
- **Stability:** Low for observability. Analytics stable; OTel/LangSmith pending. No hooks.
- **AgentCrash hook:** Must wrap the subprocess + parse `--analytics-log` JSONL or the git commit stream. Aider edits files and commits to git — filesystem watching + git-log parsing is the practical surface (Aider is `git`-native). No in-process hook to ride.

### B8. OpenHands
- **Language/runtime:** Python (SDK + runtime).
- **Instrumentation surface:** **Lifecycle hooks** (PreToolUse, PostToolUse, UserPromptSubmit, Stop, SessionStart, SessionEnd) with exit-code contract matching Claude Code (0 proceed, 2 block, other non-blocking). Two hook types: shell-command and **agent-based** (`type="agent"`, LLM-driven sub-agent for semantic decisions, runs in isolated sub-conversation with own metrics bucket `agent-hook:`). `HookConfig`/`HookMatcher`/`HookDefinition`.
- **Structured events:** Yes — immutable **Pydantic event framework** (append-only log). LLM-convertible: `MessageEvent`, `ActionEvent`, `ObservationEvent`, `SystemPromptEvent`, `CondensationSummaryEvent`, `AgentErrorEvent`, `UserRejectObservation`. Internal: `ConversationStateUpdateEvent`, `CondensationRequest`, `Condensation`, `PauseEvent`. Distinct `AgentErrorEvent` (tool-level, conversation continues) vs `ConversationErrorEvent` (terminal). Events stream to observability platforms as read-only observers.
- **OTel:** Native built-in — traces `conversation` → `conversation.run` → `agent.step` → `llm.completion`/`tool.execute`. Env-var config (`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, `_HEADERS`, `_PROTOCOL`). Laminar primary; MLflow/Honeycomb/Jaeger/Datadog/NewRelic. LiteLLM instruments LLM calls. Custom attrs `conversation_id`, `tool_name`, `action.kind`, `session_id`. Browser session replay via Laminar.
- **Stability:** High. Documented SDK, immutable event log, native OTel. One of the most coherent designs.
- **AgentCrash hook:** Register lifecycle hooks OR attach as an event-log observer (append-only stream) + OTel processor. Event framework is the replay substrate.

### B9. SWE-agent (Princeton) — maintenance-only; superseded by mini-SWE-agent
- **Language/runtime:** Python.
- **Instrumentation surface:** **Hook abstractions** — `AbstractAgentHook`/`CombinedAgentHook` (agent-level: `on_init`, `on_setup_done`, `on_step_start`, `on_model_query`, `on_action_started`, `on_action_executed`, `on_actions_generated`, `on_step_done`, `on_run_start`, `on_run_done`, `on_query_message_added`) and `RunHook` (run-level: `on_init`, `on_instance_start`, `on_instance_completed`). `add_hook()` on `DefaultAgent`/`RetryAgent`.
- **Structured events:** **Trajectory `.traj` files** (JSON): `trajectory` (per-step `response`, `thought`, `action`, `observation`, `state`, `query`), `info` (`exit_status`, `submission`, `model_stats`, `edited_files`), `history`, `replay_config`. Built-in `save_trajectory()` after each step + at run completion. `preds.json` + `run_batch_exit_statuses.yaml` for batch. Issue #1310 (Oct 2025) proposes `SaveTrajectoryHook` for remote.
- **OTel:** **None.** No OTel/event-streaming. Observability = hooks + `.traj` files + Python logging.
- **Stability:** Low/declining. Maintenance-only; latest v1.1.0 (May 2025); **superseded by mini-SWE-agent** (simpler, exception-based control flow: `InterruptAgentFlow`/`Submitted`/`LimitsExceeded`/`FormatError`/`TimeExceeded`; `save()` serializes `{info, messages, trajectory_format}`). Repo last push Jul 2026.
- **AgentCrash hook:** Implement `AbstractAgentHook`/`RunHook` for live events; parse `.traj` for replay. For new work, target mini-SWE-agent's `save()` format.

### B10. Gemini CLI (Google)
- **Language/runtime:** Node/TypeScript CLI.
- **Instrumentation surface:** **11 hook events** (settings.json, matchers, sequential/parallel, timeouts): `SessionStart`, `SessionEnd`, `BeforeAgent`, `AfterAgent`, `BeforeModel`, `AfterModel`, `BeforeToolSelection`, `BeforeTool`, `AfterTool`, `PreCompress`, `Notification`. Stable SDK-agnostic `LLMRequest`/`LLMResponse` model API so hooks don't break across SDK updates. `HookEventHandler` (`hookEventHandler.ts`) emits `coreEvents.emitHookStart/End/SystemMessage/Feedback`.
- **Structured events:** Yes — structured log events: `gemini_cli.hook_call` (`hook_name`, `hook_type`, `duration_ms`, `success`), `gemini_cli.agent.start/finish` (`agent_id`, `agent_name`, `duration_ms`, `turn_count`, `terminate_reason`), `gemini_cli.agent.recovery_attempt`, `gemini_cli.tool_call` (`decision`, `tool_type`, `metadata`), `gemini_cli.model_routing` (`routing_latency_ms`, `reasoning`, `failed`). Common attrs: `session.id`, `installation.id`, `active_approval_mode`, `user.email`.
- **OTel:** Native built-in — logs + metrics + traces via `.gemini/settings.json` (`GEMINI_TELEMETRY_ENABLED`, `GEMINI_TELEMETRY_TARGET`, `GEMINI_TELEMETRY_OTLP_ENDPOINT`). Metrics: `gemini_cli.agent.run.count/duration/turns`, `gemini_cli.tool.call.count/latency`, `gemini_cli.token.usage`, `gemini_cli.api.request.latency`. Traces: GenAI semconv. Hook telemetry infra merged PR #9082 (Nov 2025) — `HookCallEvent`, `logHookCall`, `recordHookCallMetrics`, `HookTranslator` (`HookTranslatorGenAIv1`).
- **Stability:** High. Documented, source-backed, OTel-native. Among the strongest end-to-end observability stories.
- **AgentCrash hook:** Enable Gemini CLI OTel export → AgentCrash collector; or register hooks. `gemini_cli.*` log events map cleanly to AgentCrash's event model.

### B11. GitHub Copilot coding agent (VS Code + Copilot CLI + cloud agent)
- **Language/runtime:** VS Code extension (TS), Copilot CLI (agent host process), cloud agent (Linux sandbox).
- **Instrumentation surface:** **Copilot SDK hooks** (TS/Python/Go/.NET/Java): session start/end, user prompt submitted, pre-tool use, post-tool use, error handling. Copilot CLI/cloud **hooks reference**: `sessionStart`, `sessionEnd`, `userPromptSubmitted`, `preToolUse`, `postToolUse`, `postToolUseFailure`, `permissionRequest`, `notification`, `agentStop`, `subagentStart`, `subagentStop`, `errorOccurred`, `preCompact`. Hook types: command (bash/powershell), HTTP, prompt. Locations: policy (machine-wide, un-disableable), repo (`.github/hooks/*.json`), user (`~/.copilot/hooks/`), inline, plugin. Dual payload formats: camelCase native + PascalCase (VS Code-compatible snake_case). Cloud agent constraints: Linux, ephemeral FS, restricted network, non-interactive, pre-approved tools.
- **Structured events:** Yes — OTel events: `gen_ai.client.inference.operation.details`, `copilot_chat.session.start`, `copilot_chat.tool.call`, `copilot_chat.agent.turn`, edit-feedback, user-feedback, cloud-session-invoke. Span tree `invoke_agent` → `chat` + `execute_tool`. Metrics: token usage, tool call counts/durations, agent invocation duration, edit acceptance rate, edit survival score, cloud session counts.
- **OTel:** Native — enterprise-managed OTel export (GitHub Changelog Jul 8 2026): admins mandate OTLP endpoint/protocol/service-name/resource-attrs/headers/content-capture via `telemetry` block in enterprise-managed settings; managed values override env vars and user settings; applies to Copilot Chat + Copilot CLI agent host. Works with Jaeger/Grafana/Azure Monitor/Datadog/Honeycomb. **Off by default, zero overhead, no content by default.** SDK issue #602 (closed Mar 2026) — built-in OTel moved to runtime layer (not SDK) for better background-agent observability.
- **Stability:** High and enterprise-grade. Subagent trace propagation, background agents, Claude agent sessions supported.
- **AgentCrash hook:** Enterprise OTel config → AgentCrash collector is the cleanest path; or repo-level `.github/hooks/*.json` for per-project control.

### B12. Devin (Cognition)
- **Language/runtime:** Cloud agent + Devin CLI.
- **Instrumentation surface:** **Devin CLI hooks** (`.devin/hooks.v1.json` project, `~/.config/devin/config.json` user): `PreToolUse`, `PostToolUse`, `PermissionRequest`, `UserPromptSubmit`, `Stop`, `SessionStart`, `SessionEnd`, `PostCompaction`. Command or prompt hooks, matchers, JSON stdin/stdout control (approve/block, inject context).
- **Structured events:** Yes via API — v3 API: Session Messages (`GET /messages`), Session Insights (`POST .../insights/generate` — AI-generated timeline events, action items, issues, classification; message counts, session size xs-xl, ACU consumption), Audit Logs API, Guardrail Violations (`GET /v3beta1/enterprise/guardrail-violations` — type, reasoning, confidence, action, triggering message), Queue Monitoring, Metrics API (DAU/WAU/MAU/PRs/sessions). Session `origin` field (`webapp`/`slack`/`teams`/`api`/`linear`/`jira`/`automation`/`cli`/`desktop`/`code_scan`). 2026 added MCP "View Logs" on error, Axiom/Datadog/PostHog MCP integrations, MCP audit logs.
- **OTel:** Indirect — hooks + API + MCP integrations (Axiom, Datadog as remote MCP servers). No native OTel exporter documented; observability is API-centric.
- **Stability:** Medium-high. Cloud/API-first; CLI hooks documented. Enterprise audit mature. Less self-host-observable than Claude Code/Codex.
- **AgentCrash hook:** Devin CLI hooks for local sessions; v3 API polling for cloud sessions (Session Insights, Messages, Metrics, Guardrail Violations). Cloud-agent observability requires API integration, not subprocess wrapping.

---

## Cross-cutting findings for AgentCrash architecture

1. **The hook contract is converging.** Claude Code, Codex CLI, Cursor, Windsurf, Gemini CLI, Copilot CLI, Devin, OpenHands all use some variant of: JSON-on-stdin → JSON-on-stdout, exit 2 = block, `hooks.json` at user/project/enterprise tiers, `session_id`/`conversation_id`/`transcript_path` correlation. AgentCrash should define one normalizer over this contract.

2. **Two complementary telemetry layers exist.** (a) Runtime OTel (GenAI semconv spans `invoke_agent`/`invoke_workflow`/`generate_content`/`execute_tool`) — adopted by Vercel AI SDK, Gemini CLI, Copilot, OpenHands, Mastra, CrewAI, AutoGen/MAF, Google ADK, PydanticAI, Semantic Kernel, LangChain/LangGraph (via instrumentation). (b) Code-attribution `agent-trace` (Cursor-led, RFC 0.1.0) — persistent, VCS-anchored, complementary. AgentCrash should ingest both.

3. **GenAI semconv is still Development status (mid-2026).** OpenInference (Arize) is stable with broader instrumentor coverage and is the pragmatic default for new instrumentation today; convergence expected. AgentCrash should model events in its own canonical schema and project to both, not hardcode either.

4. **The event richness ranking** (most → least structured observable surface):
   - Frameworks: CrewAI (60+ typed events) > Claude Agent SDK ≈ Google ADK > LangGraph v3 > Mastra > Vercel AI SDK > LlamaIndex > PydanticAI > DSPy > smolagents > OpenAI Agents SDK (tracing spans, not event bus) > Semantic Kernel (filters+spans) > AutoGen/MAF (spans, no events) > Haystack (spans only).
   - Coding agents: Cline (named OTel event catalog) ≈ Claude Code (27+ hooks) ≈ Codex CLI (11 hooks + OTel) ≈ Gemini CLI (OTel-native) ≈ Copilot (enterprise OTel) > Cursor ≈ Windsurf ≈ Devin > OpenHands (events+OTel) > Roo Code (IPC events, no OTel) > SWE-agent (hooks+traj) > Aider (analytics only, no hooks).

5. **Gaps to bridge (AgentCrash value-add):** Roo Code (no OTel), Aider (no hooks/OTel — needs subprocess+git/log parsing), SWE-agent (trajectory files only, maintenance-only), AutoGen/MAF (no discrete ActivityEvents), Haystack (span-only, no event bus). These are exactly where a vendor-neutral recorder/replayer fills in.

6. **Privacy is a first-class concern everywhere.** Hashed paths/commands (Cline), opt-in content capture (`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`, `ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS`, Codex `log_user_prompt=false`, Copilot no-content-by-default), PII redaction. AgentCrash must default to no-content and provide explicit, auditable content-capture modes.

7. **MCP is pervasive as a tool surface AND a hook-handler type.** Claude Code supports `mcp_tool` hook handlers; many agents emit MCP tool events; Devin/Datadog/Axiom expose MCP servers for log querying. AgentCrash can be an MCP server itself (universal observability tool) to be adoptable inside any MCP-capable agent.

8. **Cloud vs. local divergence.** Codex observation hooks don't fire for cloud tasks; Cursor cloud skips sessionStart/End/MCP/Tab hooks and user-level hooks; Copilot cloud is sandboxed/non-interactive; Devin cloud is API-only. AgentCrash must support both local hook capture and cloud API polling (Devin/Copilot/Cursor cloud) — one recorder, two transports.

---

## Sources

Frameworks: OpenAI Agents SDK (openai.github.io/openai-agents-python, developers.openai.com), Claude Agent SDK (code.claude.com/docs/en/agent-sdk, deepwiki.com/anthropics/claude-agent-sdk-typescript), LangGraph (github.com/langchain-ai/langgraph PR #7473 #7677, docs.langchain.com), LangChain (github.com/open-telemetry/opentelemetry-python-contrib, futureagi.com), CrewAI (docs.crewai.com, deepwiki.com/crewAIInc/crewAI), AutoGen/MAF (microsoft.github.io/autogen, github.com/microsoft/agent-framework #3637), Google ADK (github.com/google/adk-docs, docs.cloud.google.com/stackdriver), LlamaIndex (developers.llamaindex.ai), PydanticAI (pydantic.dev/docs/ai, github.com/pydantic/pydantic-ai #4967), Semantic Kernel (learn.microsoft.com, github.com/microsoft/semantic-kernel), Haystack (docs.haystack.deepset.ai), smolagents (huggingface.co/docs/smolagents), Mastra (mastra.ai/docs/observability), Vercel AI SDK (ai-sdk.dev/v7/docs/ai-sdk-core/telemetry, github.com/vercel/ai), DSPy (dspy.ai/tutorials/observability, arize-ai.github.io/openinference).

Coding agents: Claude Code (code.claude.com/docs/en/hooks), Codex CLI (developers.openai.com/codex/hooks, github.com/openai/codex #2103 #17996 #18026), Cursor (cursor.com/docs/hooks.md, startdebugging.net), Windsurf (cognitionai.mintlify.app/desktop/cascade/hooks, github.com/o11y-dev/opentelemetry-hooks #78), Cline (docs.cline.bot/enterprise-solutions/monitoring/opentelemetry-events, github.com/cline/cline), Roo Code (github.com/RooCodeInc/Roo-Code #11185 #12281), Aider (github.com/Aider-AI/aider #4731 #4360), OpenHands (docs.openhands.dev/sdk/guides/observability + hooks + arch/events), SWE-agent (swe-agent.com, github.com/SWE-agent/SWE-agent #1310), Gemini CLI (geminicli.com/docs/cli/telemetry + /docs/hooks, github.com/google-gemini/gemini-cli #9082), GitHub Copilot (docs.github.com/en/copilot, github.blog/changelog/2026-07-08, github.com/microsoft/vscode extensions/copilot/docs/monitoring), Devin (docs.devin.ai/cli/extensibility/hooks, cognitionai-enterprise.mintlify.app).

Standards: open-telemetry/semantic-conventions-genai (github.com), opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai, arize.com/docs/ax/concepts/otel-openinference/semantic-conventions, cursor/agent-trace (github.com, agent-trace.dev).