"""AgentCrash SDK — the in-process tracer developers use to record agent runs.

Design: every external call (LLM, tool, MCP, HTTP, retrieval, shell, fs read)
goes through :meth:`RunContext.call_external`. During **record** the real
function runs and its response is captured and marked ``replay.frozen`` with a
stable ``fixture_key`` derived from the call signature. During **replay** the
same call returns the frozen (or counterfactually modified) response *without*
running the real function — that is what makes deterministic and
counterfactual replay real instead of mocked.

This means an agent authored against the SDK is automatically replayable. An
integration adapter does the same mapping for foreign frameworks.

Example::

    from agentcrash.sdk import CrashTracer

    tracer = CrashTracer()
    with tracer.run("support", model="gpt-4o") as run:
        result = run.tool("search_customer", {"q": "john"}, lambda: search("john"))
        answer = run.llm({"prompt": ...}, lambda: model.generate(...))
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

from agentcrash.redaction import redact_event
from agentcrash.schema import (
    Actor,
    ActorType,
    AgentCrashEvent,
    ErrorInfo,
    EventType,
    ReplayMeta,
    Source,
)
from agentcrash.storage import Storage

DEFAULT_DB = os.path.join(os.getcwd(), ".agentcrash", "agentcrash.db")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _fixture_key(kind: str, name: str, signature: dict[str, Any]) -> str:
    blob = json.dumps({"kind": kind, "name": name, "sig": signature}, sort_keys=True, default=str)
    return f"{kind}:{name}:{hashlib.sha256(blob.encode()).hexdigest()[:16]}"


class RunContext:
    """A live recording run. Emits canonical events and exports them on close."""

    def __init__(self, storage: Storage, run_id: str, project_id: str, *,
                 agent: str, model: str | None, metadata: dict[str, Any],
                 source: Source):
        self.storage = storage
        self.run_id = run_id
        self.project_id = project_id
        self.agent = agent
        self.model = model
        self.source = source
        self._events: list[AgentCrashEvent] = []
        self._seq = 0
        self._stack: list[str] = []  # event ids for parent nesting
        self._tool_calls = 0
        self._retries = 0
        self._start_ms = _now_ms()
        self.metadata = metadata
        self._closed = False
        # emit run.started
        self._emit(
            EventType.RUN_STARTED.value,
            actor=Actor(type=ActorType.AGENT, name=agent),
            status="started",
            input=metadata,
        )

    # ----- low-level emit -----
    def _emit(self, type_: str, *, actor: Actor | None = None, input: Any = None,
              output: Any = None, status: str = "completed", duration_ms: int | None = None,
              error: ErrorInfo | None = None, name: str | None = None,
              replay: ReplayMeta | None = None, metadata: dict[str, Any] | None = None) -> AgentCrashEvent:
        self._seq += 1
        eid = uuid.uuid4().hex
        parent_id = self._stack[-1] if self._stack else None
        event = AgentCrashEvent(
            id=eid,
            trace_id=self.run_id,
            parent_id=parent_id,
            seq=self._seq,
            timestamp=_now_ms(),
            type=type_,
            name=name,
            source=self.source,
            actor=actor,
            input=input,
            output=output,
            status=status,
            duration_ms=duration_ms,
            error=error,
            replay=replay,
            metadata=metadata or {},
        )
        redact_event(event)
        self._events.append(event)
        return event

    @contextmanager
    def span(self, type_: str, *, actor: Actor | None = None, name: str | None = None,
             input: Any = None, metadata: dict[str, Any] | None = None):
        """A timed span. Emits a started event, then a completed/failed event on exit."""
        started = self._emit(type_, actor=actor, name=name, input=input, status="started", metadata=metadata)
        self._stack.append(started.id)
        t0 = _now_ms()
        try:
            yield started
            self._emit(
                type_,
                actor=actor,
                name=name,
                status="completed",
                duration_ms=_now_ms() - t0,
                metadata=metadata,
            )
        except Exception as exc:
            self._emit(
                type_,
                actor=actor,
                name=name,
                status="failed",
                duration_ms=_now_ms() - t0,
                error=ErrorInfo(type=type(exc).__name__, message=str(exc)),
                metadata=metadata,
            )
            raise
        finally:
            self._stack.pop()

    def event(self, type_: str, **kwargs: Any) -> AgentCrashEvent:
        """Emit a standalone event (no auto open/close)."""
        return self._emit(type_, **kwargs)

    # ----- external calls (the replayable surface) -----
    def call_external(self, *, kind: str, name: str, signature: dict[str, Any],
                      fn: Callable[[], Any], request_type: str, response_type: str,
                      actor: Actor | None = None, input_payload: Any = None) -> Any:
        """Run an external call, capture its response frozen for replay.

        ``kind``/``name``/``signature`` form the stable fixture key. ``fn`` is
        the real function; it is called during record and NOT during replay.
        """
        fkey = _fixture_key(kind, name, signature)
        req = self._emit(
            request_type, actor=actor, name=name, input=input_payload or signature,
            status="started", replay=ReplayMeta(replayable=True, frozen=False, fixture_key=fkey,
                                                call_signature=signature),
        )
        self._stack.append(req.id)
        t0 = _now_ms()
        try:
            result = fn()
            self._emit(
                response_type, actor=actor, name=name, output=result, status="completed",
                duration_ms=_now_ms() - t0,
                replay=ReplayMeta(replayable=True, frozen=True, fixture_key=fkey,
                                  call_signature=signature),
            )
            return result
        except Exception as exc:
            self._emit(
                response_type, actor=actor, name=name, status="failed",
                duration_ms=_now_ms() - t0,
                error=ErrorInfo(type=type(exc).__name__, message=str(exc)),
                replay=ReplayMeta(replayable=True, frozen=True, fixture_key=fkey,
                                  call_signature=signature),
            )
            raise
        finally:
            self._stack.pop()

    def llm(self, request: dict[str, Any], fn: Callable[[], Any], *, name: str = "llm") -> Any:
        return self.call_external(
            kind="llm", name=name, signature=request, fn=fn,
            request_type=EventType.LLM_REQUEST.value, response_type=EventType.LLM_RESPONSE.value,
            actor=Actor(type=ActorType.LLM, name=self.model or name),
            input_payload=request,
        )

    def tool(self, name: str, args: dict[str, Any], fn: Callable[[], Any]) -> Any:
        self._tool_calls += 1
        return self.call_external(
            kind="tool", name=name, signature=args, fn=fn,
            request_type=EventType.TOOL_CALLED.value, response_type=EventType.TOOL_COMPLETED.value,
            actor=Actor(type=ActorType.TOOL, name=name),
            input_payload=args,
        )

    def decision(self, label: str, detail: dict[str, Any]) -> None:
        """Record an OBSERVABLE agent decision. Never private reasoning."""
        self._emit(
            EventType.AGENT_DECISION.value,
            actor=Actor(type=ActorType.AGENT, name=self.agent),
            name=label,
            output=detail,
        )

    def record_retry(self) -> None:
        self._retries += 1

    # ----- close -----
    def close(self, status: str = "completed", error: str | None = None) -> None:
        if self._closed:
            return
        self._closed = True
        terminal = EventType.RUN_FAILED.value if status == "failed" else EventType.RUN_COMPLETED.value
        self._emit(
            terminal,
            actor=Actor(type=ActorType.AGENT, name=self.agent),
            status=status,
            error=ErrorInfo(message=error) if error else None,
            output={"tool_calls": self._tool_calls, "retries": self._retries},
        )
        self.storage.insert_events(self.run_id, self._events)
        self.storage.finish_run(
            self.run_id,
            status,
            error=error,
            duration_ms=_now_ms() - self._start_ms,
            tool_calls=self._tool_calls,
            retries=self._retries,
        )

    def __enter__(self) -> RunContext:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc is not None:
            self.close(status="failed", error=f"{type(exc).__name__}: {exc}")
        else:
            self.close(status="completed")


class CrashTracer:
    """Entry point. Holds a Storage and a default Source."""

    def __init__(self, storage: Storage | None = None, *, integration: str = "generic-python",
                 framework: str | None = None, version: str | None = None,
                 db_path: str | None = None):
        self.storage = storage or Storage(db_path or DEFAULT_DB)
        self.source = Source(integration=integration, framework=framework, version=version)

    def run(self, agent: str, *, model: str | None = None, project: str = "default",
            metadata: dict[str, Any] | None = None) -> RunContext:
        pid = self.storage.get_or_create_default_project(project)
        rid = self.storage.create_run(pid, agent=agent, model=model, metadata=metadata)
        return RunContext(self.storage, rid, pid, agent=agent, model=model,
                          metadata=metadata or {}, source=self.source)