"""LangChain / LangGraph callback adapter -> agentcrash.schema.v1.

A duck-typed callback handler. LangChain dispatches callbacks by method name
(``getattr(handler, 'on_chat_model_start', None)``), so this class records the
canonical event tree **without requiring ``langchain_core`` installed** — and
one handler covers LangChain *and* LangGraph, which share the callback system.
Pass it to any Runnable::

    from agentcrash.sdk import CrashTracer
    from agentcrash.integrations.langchain import AgentCrashCallbackHandler

    tracer = CrashTracer(integration="langchain", framework="langchain")
    with tracer.run("my-agent", model="gpt-4o") as run:
        handler = AgentCrashCallbackHandler(run)
        chain.invoke(inputs, config={"callbacks": [handler]})
        # or: graph.invoke(state, config={"callbacks": [handler]})

Each callback pair (start/end) is folded into ONE canonical event with input +
output + duration:

* ``on_chat_model_start``/``on_llm_*`` -> ``LLM_RESPONSE``
* ``on_tool_*`` -> ``TOOL_COMPLETED`` / ``TOOL_FAILED``
* ``on_chain_*`` -> ``AGENT_DECISION`` (an observable agent step)
* ``on_retriever_*`` -> ``RETRIEVAL_COMPLETED``

Events are **observational** (``replay=None``): callbacks fire *around*
execution the framework already performed, so there is no ``fn`` to wrap on
the replay rail (unlike :class:`~agentcrash.integrations.mcp_client.MCPClientRecorder`,
which wraps the real call). The value is the same as the OTel importer —
``trace_search`` / ``trace_get`` / web-UI inspection / failure-event
identification, redacted at record time (redaction runs inside
``RunContext.event``). Replay / counterfactuals for a LangChain run need the
LLM-wrapper variant (a recording ``BaseChatModel``), which is a later, larger
surface — noted here so the scope is honest.

Sync methods serve both sync and async chains: langchain's async callback
manager calls the sync method and awaits only if the result is a coroutine, so
no ``*_async`` variants are needed.
"""

from __future__ import annotations

import time
from typing import Any

from agentcrash.schema import Actor, ActorType, ErrorInfo, EventType

__all__ = ["AgentCrashCallbackHandler"]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _to_payload(obj: Any) -> Any:
    """Coerce a langchain callback argument to JSON-able data, duck-typed.

    Handles primitives, dicts/lists, pydantic models (``model_dump``),
    ``LLMResult``/``ChatResult`` (``.generations`` -> text/content), and
    ``BaseMessage`` (``.content``/``.type``). Falls back to ``str(obj)`` so an
    exotic shape is still recorded (and redacted) rather than dropping it.
    """
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {k: _to_payload(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_payload(x) for x in obj]
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump(mode="json")
        except Exception:  # noqa: BLE001  — be defensive; fall through
            pass
    gens = getattr(obj, "generations", None)
    if gens is not None:
        out: list[Any] = []
        for batch in gens:
            for g in batch:
                msg = getattr(g, "message", None)
                text = getattr(g, "text", None)
                if msg is not None:
                    out.append(_to_payload(msg))
                elif text is not None:
                    out.append(text)
        return out
    content = getattr(obj, "content", None)
    if content is not None:
        return {"type": getattr(obj, "type", getattr(obj, "role", "message")),
                "content": _to_payload(content)}
    return str(obj)


def _name_from_serialized(serialized: Any) -> str | None:
    if not isinstance(serialized, dict):
        return None
    if serialized.get("name"):
        return serialized["name"]
    ids = serialized.get("id") or []
    return ids[-1] if ids else None


def _model_from_serialized(serialized: Any) -> str | None:
    if not isinstance(serialized, dict):
        return None
    kwargs = serialized.get("kwargs") or {}
    for k in ("model", "model_name", "model_id"):
        if kwargs.get(k):
            return kwargs[k]
    return _name_from_serialized(serialized)


def _err(exc: Any) -> ErrorInfo:
    if isinstance(exc, BaseException):
        return ErrorInfo(type=type(exc).__name__, message=str(exc))
    return ErrorInfo(message=str(exc))


class AgentCrashCallbackHandler:
    """Records a LangChain/LangGraph execution into a live AgentCrash run.

    ``ctx`` is a :class:`~agentcrash.sdk.RunContext`. The handler folds each
    callback start/end pair into one canonical event via ``ctx.event`` (which
    redacts). See module docstring for the type mapping and the observational
    scope (no replay fixture).
    """

    def __init__(self, ctx: Any):
        self.ctx = ctx
        # langchain run_id (UUID) -> pending start info, to pair start with end.
        self._pending: dict[Any, dict] = {}

    # ----- shared recording -----
    def _start(self, kind: str, run_id: Any, parent_run_id: Any,
               name: str | None, model: str | None, input_payload: Any) -> None:
        self._pending[run_id] = {
            "kind": kind, "name": name, "model": model,
            "input": input_payload, "start_ms": _now_ms(),
        }

    def _actor(self, kind: str, name: str | None, model: str | None) -> Actor:
        if kind == "llm":
            return Actor(type=ActorType.LLM, name=model or name)
        if kind in ("tool", "retrieval"):
            return Actor(type=ActorType.TOOL, name=name or ("retriever" if kind == "retrieval" else None))
        return Actor(type=ActorType.AGENT, name=name or self.ctx.agent)

    def _end(self, run_id: Any, event_type: str, output: Any, *,
             status: str = "completed", error: ErrorInfo | None = None) -> None:
        p = self._pending.pop(run_id, {})
        kind = p.get("kind", "chain")
        metadata: dict[str, Any] = {}
        if p.get("model"):
            metadata["model"] = p["model"]
        self.ctx.event(
            event_type,
            actor=self._actor(kind, p.get("name"), p.get("model")),
            name=p.get("name"),
            input=p.get("input"),
            output=output,
            status=status,
            duration_ms=_now_ms() - p.get("start_ms", _now_ms()),
            error=error,
            metadata=metadata,
        )

    # ----- LLM (chat models fire on_chat_model_start; legacy LLM fires on_llm_start) -----
    def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, **kw):
        self._llm_start(serialized, messages, run_id, parent_run_id)

    def on_llm_start(self, serialized, prompts, *, run_id, parent_run_id=None, **kw):
        self._llm_start(serialized, prompts, run_id, parent_run_id)

    def _llm_start(self, serialized, payload, run_id, parent_run_id):
        self._start("llm", run_id, parent_run_id, _name_from_serialized(serialized),
                    _model_from_serialized(serialized), _to_payload(payload))

    def on_llm_end(self, response, *, run_id, parent_run_id=None, **kw):
        self._end(run_id, EventType.LLM_RESPONSE.value, _to_payload(response))

    def on_llm_error(self, error, *, run_id, parent_run_id=None, **kw):
        self._end(run_id, EventType.LLM_RESPONSE.value, None, status="failed", error=_err(error))

    # ----- tools -----
    def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None, **kw):
        self._start("tool", run_id, parent_run_id, _name_from_serialized(serialized), None,
                    _to_payload(input_str))

    def on_tool_end(self, output, *, run_id, parent_run_id=None, **kw):
        self._end(run_id, EventType.TOOL_COMPLETED.value, _to_payload(output))

    def on_tool_error(self, error, *, run_id, parent_run_id=None, **kw):
        self._end(run_id, EventType.TOOL_FAILED.value, None, status="failed", error=_err(error))

    # ----- chains (an observable agent step) -----
    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, **kw):
        self._start("chain", run_id, parent_run_id, _name_from_serialized(serialized), None,
                    _to_payload(inputs))

    def on_chain_end(self, outputs, *, run_id, parent_run_id=None, **kw):
        self._end(run_id, EventType.AGENT_DECISION.value, _to_payload(outputs))

    def on_chain_error(self, error, *, run_id, parent_run_id=None, **kw):
        self._end(run_id, EventType.AGENT_DECISION.value, None, status="failed", error=_err(error))

    # ----- retrieval -----
    def on_retriever_start(self, serialized, query, *, run_id, parent_run_id=None, **kw):
        self._start("retrieval", run_id, parent_run_id, _name_from_serialized(serialized), None,
                    _to_payload(query))

    def on_retriever_end(self, output, *, run_id, parent_run_id=None, **kw):
        self._end(run_id, EventType.RETRIEVAL_COMPLETED.value, _to_payload(output))

    def on_retriever_error(self, error, *, run_id, parent_run_id=None, **kw):
        self._end(run_id, EventType.RETRIEVAL_COMPLETED.value, None, status="failed", error=_err(error))