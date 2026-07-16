"""OpenTelemetry GenAI span import — OTel -> agentcrash.schema.v1.

Import lane (``docs/research/opentelemetry.md`` §7.3): ingest OTel GenAI
traces in the OTLP/JSON shape (as written by the Collector ``file`` exporter
or any SDK span dump) into the AgentCrash store, so they are viewable,
searchable, and failure-identified through the web UI / MCP server.

Dependency-free: consumes plain span dicts — no ``opentelemetry`` SDK required.
The GenAI semconv is still **Development** (names drift between versions), so
every attribute key is centralized in :data:`_KEYS` below; a rename is a
one-file change (research §2.2).

Imported traces are **observational**: events carry no replay fixture, because
the agent function that produced them is not available — exact / counterfactual
replay is not possible for a foreign trace. That is the sidecar gap the
research doc §6/§7.4 identifies (targeted by Phase 5/6). What you get today:
``trace_search``, ``trace_get``, web-UI inspection, and failure-event
identification. To replay a run, produce it through the SDK / MCP recorder or
register its agent explicitly.

Secrets are redacted at ingest (prompts / tool I/O routinely carry PII and
keys); see :func:`redact_event`.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from agentcrash.redaction import redact_event
from agentcrash.schema import (
    Actor,
    ActorType,
    AgentCrashEvent,
    ErrorInfo,
    EventType,
    Source,
)

__all__ = ["import_bundle", "import_spans", "span_to_event"]

# Centralized GenAI attribute keys (research §2.2 — semconv is Development;
# renames are a one-file change here). OpenInference fallbacks included since
# auto-instrumentors vary (research §7.3).
_KEYS = {
    "operation": ("gen_ai.operation.name",),
    "provider": ("gen_ai.provider.name",),
    "request_model": ("gen_ai.request.model", "gen_ai.system", "llm.model_name"),
    "response_model": ("gen_ai.response.model",),
    "tool_name": ("gen_ai.tool.name", "tool.name"),
    "usage_input": ("gen_ai.usage.input_tokens", "gen_ai.usage.prompt_tokens", "llm.token_count.prompt"),
    "usage_output": ("gen_ai.usage.output_tokens", "gen_ai.usage.completion_tokens", "llm.token_count.completion"),
    "error_type": ("error.type", "exception.type"),
    "input": ("gen_ai.prompt", "gen_ai.tool.input", "llm.input_messages", "input.value", "input"),
    "output": ("gen_ai.completion", "gen_ai.tool.output", "llm.output_messages", "output.value", "output"),
    "agent_id": ("gen_ai.agent.id", "agent.id"),
    "service_name": ("service.name",),
}

# gen_ai.operation.name -> AgentCrash event type. (research §5.1)
_OP_TO_TYPE: dict[str, str] = {
    "chat": EventType.LLM_RESPONSE.value,
    "execute_tool": EventType.TOOL_COMPLETED.value,
    "retrieval": EventType.RETRIEVAL_COMPLETED.value,
    "embeddings": EventType.RETRIEVAL_COMPLETED.value,
    "invoke_agent": EventType.AGENT_DECISION.value,
    "invoke_workflow": EventType.AGENT_DECISION.value,
    "create_agent": EventType.AGENT_DECISION.value,
    "create_memory": EventType.MEMORY_WRITE.value,
    "search_memory": EventType.MEMORY_READ.value,
    "get_memory": EventType.MEMORY_READ.value,
    "upsert_memory": EventType.MEMORY_WRITE.value,
    "delete_memory": EventType.MEMORY_WRITE.value,
}

_OP_TO_ACTOR: dict[str, ActorType] = {
    "chat": ActorType.LLM,
    "execute_tool": ActorType.TOOL,
    "retrieval": ActorType.TOOL,
    "embeddings": ActorType.TOOL,
    "invoke_agent": ActorType.AGENT,
    "invoke_workflow": ActorType.AGENT,
    "create_agent": ActorType.AGENT,
}


def _first(attrs: dict[str, Any], key_group: tuple[str, ...]) -> Any:
    for k in key_group:
        if k in attrs and attrs[k] not in (None, ""):
            return attrs[k]
    return None


def _flatten_value(v: Any) -> Any:
    """Decode an OTLP `value` wrapper ({stringValue, intValue, doubleValue,
    boolValue, arrayValue: {values: [...]}}) to a plain Python value."""
    if not isinstance(v, dict):
        return v
    if "stringValue" in v:
        return v["stringValue"]
    if "intValue" in v:
        return int(v["intValue"])
    if "doubleValue" in v:
        return float(v["doubleValue"])
    if "boolValue" in v:
        return bool(v["boolValue"])
    if "arrayValue" in v:
        return [_flatten_value(x) for x in v["arrayValue"].get("values", [])]
    if "kvlistValue" in v:  # map-typed attribute
        return {_kv["key"]: _flatten_value(_kv["value"])
                for _kv in v["kvlistValue"].get("values", [])}
    return v


def _flatten_attrs(otel_attrs: list[dict[str, Any]] | None) -> dict[str, Any]:
    """OTLP attributes (list of {key, value: {...}}) -> flat dict."""
    out: dict[str, Any] = {}
    for a in otel_attrs or []:
        if not isinstance(a, dict) or "key" not in a:
            continue
        out[a["key"]] = _flatten_value(a.get("value"))
    return out


def _span_attrs(span: dict[str, Any]) -> dict[str, Any]:
    """Flat attrs for a span, preferring pre-merged resource attrs from the
    bundle flattener. Falls back to decoding the raw OTLP attribute list."""
    flat = span.get("_flat")
    return flat if flat is not None else _flatten_attrs(span.get("attributes"))


def _payload(span_attrs: dict[str, Any], span_events: list[dict[str, Any]]) -> tuple[Any, Any]:
    """Extract (input, output) payloads from span attrs then span events.

    GenAI moved full bodies into span events (research §1.1, §2.7); older
    instrumentations keep them in attributes. Search both, attrs first.
    """
    inp = _first(span_attrs, _KEYS["input"])
    out = _first(span_attrs, _KEYS["output"])
    if inp is None or out is None:
        for ev in span_events:
            ev_attrs = _flatten_attrs(ev.get("attributes"))
            if inp is None:
                inp = _first(ev_attrs, _KEYS["input"])
            if out is None:
                out = _first(ev_attrs, _KEYS["output"])
            if inp is not None and out is not None:
                break
    return inp, out


def _status(span: dict[str, Any]) -> tuple[str, str | None, str | None]:
    """Return (event_status, error_type, error_message) from an OTel span."""
    st = span.get("status") or {}
    code = st.get("code")
    # OTLP codes: "STATUS_CODE_UNSET"/"STATUS_CODE_OK"/"STATUS_CODE_ERROR" or 0/1/2
    failed = code in ("STATUS_CODE_ERROR", 2, "ERROR")
    if not failed:
        return "completed", None, None
    attrs = _flatten_attrs(span.get("attributes"))
    etype = _first(attrs, _KEYS["error_type"]) or "error"
    msg = st.get("message") or _first(attrs, _KEYS["error_type"]) or "span failed"
    return "failed", etype, msg


def span_to_event(span: dict[str, Any], *, run_id: str, seq: int, source: Source) -> AgentCrashEvent:
    """Map a single OTel GenAI span to a canonical AgentCrash event.

    ``run_id`` is the OTel ``traceId`` (one trace = one run). ``seq`` is the
    event's position (spans are ordered by start time by the caller).
    """
    attrs = _span_attrs(span)
    span_events = span.get("events") or []
    op = _first(attrs, _KEYS["operation"]) or "unknown"
    event_type = _OP_TO_TYPE.get(op, EventType.AGENT_DECISION.value)

    status, etype, msg = _status(span)
    if status == "failed" and event_type == EventType.TOOL_COMPLETED.value:
        event_type = EventType.TOOL_FAILED.value

    name = _first(attrs, _KEYS["tool_name"]) or span.get("name") or op
    inp, out = _payload(attrs, span_events)
    model = _first(attrs, _KEYS["response_model"]) or _first(attrs, _KEYS["request_model"])
    provider = _first(attrs, _KEYS["provider"])

    actor_type = _OP_TO_ACTOR.get(op, ActorType.AGENT)
    actor = Actor(type=actor_type, name=model if actor_type is ActorType.LLM else name)

    metadata: dict[str, Any] = {}
    if op:
        metadata["gen_ai.operation.name"] = op
    if provider:
        metadata["gen_ai.provider.name"] = provider
    if model:
        metadata["model"] = model
    usage = {
        "input": _first(attrs, _KEYS["usage_input"]),
        "output": _first(attrs, _KEYS["usage_output"]),
    }
    usage = {k: v for k, v in usage.items() if v is not None}
    if usage:
        # key is "usage" not "tokens": redaction's sensitive-key matcher flags any
        # key containing the fragment "token", which would scrub the whole dict.
        metadata["usage"] = usage
    aid = _first(attrs, _KEYS["agent_id"])
    if aid:
        metadata["gen_ai.agent.id"] = aid

    start_ns = int(span.get("startTimeUnixNano") or 0)
    end_ns = int(span.get("endTimeUnixNano") or start_ns)
    error = ErrorInfo(type=etype, message=msg) if status == "failed" else None

    event = AgentCrashEvent(
        id=span.get("spanId") or uuid4().hex,
        trace_id=run_id,
        parent_id=span.get("parentSpanId") or None,
        seq=seq,
        timestamp=start_ns // 1_000_000 if start_ns else 0,
        duration_ms=max(0, (end_ns - start_ns) // 1_000_000),
        type=event_type,
        name=name,
        source=source,
        actor=actor,
        input=inp,
        output=out,
        status=status,
        error=error,
        metadata=metadata,
        # replay=None: imported spans are observational, not replayable (research §6).
    )
    return redact_event(event)


def _spans_from_bundle(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten an OTLP/JSON `resourceSpans` bundle into a flat span list,
    pre-merging each span's flat attrs with its resource attrs (e.g. service.name).
    """
    spans: list[dict[str, Any]] = []
    for rs in bundle.get("resourceSpans", []):
        resource_attrs = _flatten_attrs((rs.get("resource") or {}).get("attributes"))
        for ss in rs.get("scopeSpans", []):
            for sp in ss.get("spans", []):
                merged = dict(resource_attrs)
                merged.update(_flatten_attrs(sp.get("attributes")))
                sp = dict(sp)
                sp["_flat"] = merged
                spans.append(sp)
    return spans


def _by_trace(spans: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    traces: dict[str, list[dict[str, Any]]] = {}
    for sp in spans:
        tid = sp.get("traceId") or uuid4().hex
        traces.setdefault(tid, []).append(sp)
    return traces


def import_spans(storage: Any, spans: list[dict[str, Any]], *,
                 project: str = "otel", integration: str = "otel") -> str:
    """Import a single trace's spans as one AgentCrash run. Returns the run_id
    (= the OTel traceId). Spans need not be pre-sorted; they are ordered by
    start time. ``storage`` is an :class:`agentcrash.storage.Storage`."""
    if not spans:
        raise ValueError("no spans to import")
    trace_id = spans[0].get("traceId") or uuid4().hex
    spans_sorted = sorted(spans, key=lambda s: int(s.get("startTimeUnixNano") or 0))

    pid = storage.get_or_create_default_project(project)
    # derive agent/model from resource + the first chat span, if present
    agent = "otel"
    model = None
    for sp in spans_sorted:
        a = _span_attrs(sp)
        sn = _first(a, _KEYS["service_name"])
        if sn:
            agent = sn
            break
    for sp in spans_sorted:
        a = _span_attrs(sp)
        m = _first(a, _KEYS["request_model"])
        if m:
            model = m
            break

    run_id = storage.create_run(pid, agent=agent, model=model, run_id=trace_id,
                                metadata={"source": "otel", "trace_id": trace_id})
    source = Source(integration=integration, framework="opentelemetry")
    events: list[AgentCrashEvent] = []
    # synthetic run.started for UI/analyzer consistency
    first_ns = int(spans_sorted[0].get("startTimeUnixNano") or 0)
    started = AgentCrashEvent(
        id=uuid4().hex, trace_id=run_id, parent_id=None, seq=1,
        timestamp=first_ns // 1_000_000 if first_ns else 0,
        type=EventType.RUN_STARTED.value, name="run.started", source=source,
        actor=Actor(type=ActorType.AGENT, name=agent), status="started",
    )
    events.append(redact_event(started))
    any_failed = False
    for sp in spans_sorted:
        ev = span_to_event(sp, run_id=run_id, seq=len(events) + 1, source=source)
        if ev.status == "failed":
            any_failed = True
        events.append(ev)
    terminal_type = EventType.RUN_FAILED.value if any_failed else EventType.RUN_COMPLETED.value
    terminal = AgentCrashEvent(
        id=uuid4().hex, trace_id=run_id, parent_id=None, seq=len(events) + 1,
        timestamp=0, type=terminal_type, name=terminal_type, source=source,
        actor=Actor(type=ActorType.AGENT, name=agent),
        status="failed" if any_failed else "completed",
    )
    events.append(redact_event(terminal))
    storage.insert_events(run_id, events)
    error = next((e.error.message for e in reversed(events) if e.error), None) if any_failed else None
    storage.finish_run(run_id, "failed" if any_failed else "completed", error=error)
    return run_id


def import_bundle(storage: Any, bundle: dict[str, Any] | str, *,
                  project: str = "otel", integration: str = "otel") -> list[str]:
    """Import every trace in an OTLP/JSON bundle (``{"resourceSpans": [...]}``,
    or a JSON string of the same). Each trace becomes one run. Returns run_ids."""
    if isinstance(bundle, str):
        bundle = json.loads(bundle)
    spans = _spans_from_bundle(bundle)
    if not spans:
        return []
    run_ids: list[str] = []
    for _trace_id, trace_spans in _by_trace(spans).items():
        run_ids.append(import_spans(storage, trace_spans, project=project, integration=integration))
    return run_ids