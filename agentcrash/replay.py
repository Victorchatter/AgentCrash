"""The replay engine.

Re-runs an agent against a frozen fixture of its original external responses.
Three modes:

* ``exact`` — every external response frozen. Deterministic reproduction.
* ``selective`` — chosen *pure* kinds (llm, retrieval, fs.read) run live; all
  side-effecting kinds stay frozen. This is how counterfactuals work offline:
  freeze the tool responses (or apply an intervention to them), re-run the LLM
  decision live against the modified state, observe the new outcome.
* ``live`` — everything runs for real against the environment. **Dangerous**,
  requires explicit consent; side-effecting calls may fire.

The agent is re-invoked as ``agent_fn(original_input, ctx)`` where ``ctx``
mirrors :class:`agentcrash.sdk.RunContext`. An agent authored against the SDK
is therefore replayable as-is.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agentcrash.diff import diff_runs
from agentcrash.interventions import FixtureEntry, Intervention, apply_interventions
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
from agentcrash.sdk import _fixture_key, _now_ms
from agentcrash.storage import Storage

# Kinds whose fns are safe to re-run live in *selective* (simulated) replay when
# a call is not in the frozen fixture (i.e. the agent diverged into a new branch).
# These represent a simulated/mocked environment, not real external services.
# Real side-effecting kinds (http, shell, filesystem.write) are excluded — a
# missing fixture entry for them in selective mode is an error, not a live call.
# ``live`` mode (real environment, explicit consent) is the only path that runs
# them live.
SIMULATED_LIVE_KINDS = {"llm", "tool", "retrieval", "filesystem.read", "mcp"}


class MissingFixtureError(RuntimeError):
    """The replayed agent made a call not present in the frozen fixture.

    In exact replay this means the agent diverged from the recording (e.g. an
    intervention changed control flow into a new branch). Re-running it live
    would be unsafe for side-effecting calls, so we stop.
    """


class ReplayFrozenError(RuntimeError):
    """Raised to mimic a frozen failure during replay."""

    def __init__(self, error: dict[str, Any] | None):
        self.error = error or {}
        super().__init__(error.get("message", "frozen failure") if error else "frozen failure")


@dataclass
class ReplayConfig:
    mode: str = "exact"  # exact | selective | live
    live_kinds: set[str] = field(default_factory=set)
    interventions: list[Intervention] = field(default_factory=list)
    consent_live: bool = False  # must be True for mode=="live"


@dataclass
class ReplayResult:
    replay_id: str
    new_run_id: str
    original_run_id: str
    status: str
    agent_output: Any = None
    error: str | None = None
    diff_lines: list[str] = field(default_factory=list)
    diff: Any = None
    events: list[AgentCrashEvent] = field(default_factory=list)


def build_fixture(events: list[AgentCrashEvent]) -> dict[str, FixtureEntry]:
    """Build {fixture_key: FixtureEntry} from a run's frozen response events."""
    fixture: dict[str, FixtureEntry] = {}
    for e in events:
        if not (e.replay and e.replay.frozen and e.replay.fixture_key):
            continue
        kind = _kind_for_type(e.type)
        if kind is None:
            continue
        fixture[e.replay.fixture_key] = FixtureEntry(
            fixture_key=e.replay.fixture_key,
            kind=kind,
            name=e.name or "",
            type=e.type,
            output=e.output,
            status=e.status,
            error=e.error.model_dump() if e.error else None,
            call_signature=e.replay.call_signature or {},
        )
    return fixture


def _kind_for_type(type_: str) -> str | None:
    mapping = {
        EventType.LLM_RESPONSE.value: "llm",
        EventType.TOOL_COMPLETED.value: "tool",
        EventType.MCP_RESPONSE.value: "mcp",
        EventType.HTTP_RESPONSE.value: "http",
        EventType.RETRIEVAL_COMPLETED.value: "retrieval",
        EventType.SHELL_COMMAND.value: "shell",
        EventType.FILESYSTEM_READ.value: "filesystem.read",
    }
    return mapping.get(type_)


class ReplayContext:
    """Mirrors RunContext's external-call surface for replay."""

    def __init__(self, storage: Storage, run_id: str, project_id: str, *,
                 agent: str, model: str | None, source: Source,
                 fixture: dict[str, FixtureEntry], config: ReplayConfig,
                 original_input: Any):
        self.storage = storage
        self.run_id = run_id
        self.project_id = project_id
        self.agent = agent
        self.model = model
        self.source = source
        self.fixture = fixture
        self.config = config
        self.original_input = original_input
        self._events: list[AgentCrashEvent] = []
        self._seq = 0
        self._stack: list[str] = []
        self._tool_calls = 0
        self._retries = 0
        self._start_ms = _now_ms()
        self._closed = False
        self._emit(
            EventType.RUN_STARTED.value,
            actor=Actor(type=ActorType.AGENT, name=agent),
            status="started",
            input={"original_input": original_input, "replay_of": None},
        )

    def _emit(self, type_: str, *, actor: Actor | None = None, input: Any = None,
              output: Any = None, status: str = "completed", duration_ms: int | None = None,
              error: ErrorInfo | None = None, name: str | None = None,
              replay: ReplayMeta | None = None, metadata: dict[str, Any] | None = None) -> AgentCrashEvent:
        self._seq += 1
        eid = uuid.uuid4().hex
        parent_id = self._stack[-1] if self._stack else None
        event = AgentCrashEvent(
            id=eid, trace_id=self.run_id, parent_id=parent_id, seq=self._seq,
            timestamp=_now_ms(), type=type_, name=name, source=self.source, actor=actor,
            input=input, output=output, status=status, duration_ms=duration_ms,
            error=error, replay=replay, metadata=metadata or {},
        )
        redact_event(event)
        self._events.append(event)
        return event

    def decision(self, label: str, detail: dict[str, Any]) -> None:
        self._emit(EventType.AGENT_DECISION.value, actor=Actor(type=ActorType.AGENT, name=self.agent),
                   name=label, output=detail)

    def record_retry(self) -> None:
        self._retries += 1

    def event(self, type_: str, **kwargs: Any) -> AgentCrashEvent:
        return self._emit(type_, **kwargs)

    def _is_live(self, kind: str) -> bool:
        if self.config.mode == "live":
            return True
        if self.config.mode == "exact":
            return False
        # selective: caller may restrict; default = the simulated-safe set
        allowed = self.config.live_kinds or SIMULATED_LIVE_KINDS
        return kind in allowed

    def call_external(self, *, kind: str, name: str, signature: dict[str, Any],
                      fn: Callable[[], Any], request_type: str, response_type: str,
                      actor: Actor | None = None, input_payload: Any = None) -> Any:
        fkey = _fixture_key(kind, name, signature)
        req = self._emit(
            request_type, actor=actor, name=name, input=input_payload or signature, status="started",
            replay=ReplayMeta(replayable=True, frozen=False, fixture_key=fkey, call_signature=signature),
        )
        self._stack.append(req.id)
        t0 = _now_ms()
        # A live-only intervention (replace_model/modify_prompt) forces this call
        # to re-run live even if a frozen response exists.
        probe = FixtureEntry(fixture_key=fkey, kind=kind, name=name or "", type=response_type)
        live_override = any(iv.is_live_only() and iv.matches(probe) for iv in self.config.interventions)
        entry = self.fixture.get(fkey)
        try:
            if entry is not None and not live_override:
                # frozen path — use the captured (possibly intervened) response
                if entry.status == "failed":
                    self._emit(response_type, actor=actor, name=name, status="failed",
                               duration_ms=_now_ms() - t0,
                               error=ErrorInfo(**(entry.error or {"message": "frozen failure"})),
                               replay=ReplayMeta(replayable=True, frozen=True, fixture_key=fkey,
                                                 call_signature=signature))
                    raise ReplayFrozenError(entry.error)
                self._emit(response_type, actor=actor, name=name, output=entry.output, status="completed",
                           duration_ms=_now_ms() - t0,
                           replay=ReplayMeta(replayable=True, frozen=True, fixture_key=fkey,
                                             call_signature=signature))
                return entry.output
            # live path — either a new (divergent) call, or a live_only intervention
            if entry is None and not live_override and not self._is_live(kind):
                raise MissingFixtureError(
                    f"replay diverged: no frozen response for {kind}:{name} (key={fkey}) "
                    f"and kind not live-allowed in {self.config.mode} mode"
                )
            result = fn()
            self._emit(response_type, actor=actor, name=name, output=result, status="completed",
                       duration_ms=_now_ms() - t0,
                       replay=ReplayMeta(replayable=True, frozen=False, fixture_key=fkey,
                                         call_signature=signature))
            return result
        except ReplayFrozenError:
            raise
        except MissingFixtureError:
            raise
        except Exception as exc:
            self._emit(response_type, actor=actor, name=name, status="failed", duration_ms=_now_ms() - t0,
                       error=ErrorInfo(type=type(exc).__name__, message=str(exc)),
                       replay=ReplayMeta(replayable=True, frozen=False, fixture_key=fkey,
                                         call_signature=signature))
            raise
        finally:
            self._stack.pop()

    def llm(self, request: dict[str, Any], fn: Callable[[], Any], *, name: str = "llm") -> Any:
        return self.call_external(
            kind="llm", name=name, signature=request, fn=fn,
            request_type=EventType.LLM_REQUEST.value, response_type=EventType.LLM_RESPONSE.value,
            actor=Actor(type=ActorType.LLM, name=self.model or name), input_payload=request,
        )

    def tool(self, name: str, args: dict[str, Any], fn: Callable[[], Any]) -> Any:
        self._tool_calls += 1
        return self.call_external(
            kind="tool", name=name, signature=args, fn=fn,
            request_type=EventType.TOOL_CALLED.value, response_type=EventType.TOOL_COMPLETED.value,
            actor=Actor(type=ActorType.TOOL, name=name), input_payload=args,
        )

    def close(self, status: str = "completed", error: str | None = None) -> None:
        if self._closed:
            return
        self._closed = True
        terminal = EventType.RUN_FAILED.value if status == "failed" else EventType.RUN_COMPLETED.value
        self._emit(terminal, actor=Actor(type=ActorType.AGENT, name=self.agent), status=status,
                   error=ErrorInfo(message=error) if error else None,
                   output={"tool_calls": self._tool_calls, "retries": self._retries})
        self.storage.insert_events(self.run_id, self._events)
        self.storage.finish_run(self.run_id, status, error=error, duration_ms=_now_ms() - self._start_ms,
                                tool_calls=self._tool_calls, retries=self._retries)


class Replayer:
    def __init__(self, storage: Storage):
        self.storage = storage

    def replay(self, original_run_id: str, agent_fn: Callable[[Any, Any], Any],
               original_input: Any, config: ReplayConfig | None = None,
               *, agent: str | None = None, model: str | None = None) -> ReplayResult:
        if config is None:
            config = ReplayConfig()
        if config.mode == "live" and not config.consent_live:
            raise PermissionError("live replay requires explicit consent (config.consent_live=True)")

        original_events = self.storage.get_events(original_run_id)
        if not original_events:
            raise KeyError(f"no events for run {original_run_id}")
        run = self.storage.get_run(original_run_id)
        agent_name = agent or (run.get("agent") if run else "agent")
        model_name = model or (run.get("model") if run else None)

        base_fixture = build_fixture(original_events)
        fixture = apply_interventions(base_fixture, config.interventions)

        project_id = run.get("project_id") if run else self.storage.get_or_create_default_project()
        new_run_id = self.storage.create_run(
            project_id, agent=agent_name, model=model_name,
            metadata={"replay_of": original_run_id, "mode": config.mode,
                      "interventions": [iv.type for iv in config.interventions]},
        )
        replay_id = self.storage.create_replay(
            original_run_id, config.mode,
            {"live_kinds": sorted(config.live_kinds),
             "interventions": [{"type": iv.type, "spec": iv.spec} for iv in config.interventions]},
        )
        for iv in config.interventions:
            self.storage.add_intervention(replay_id, iv.type, iv.fixture_key, iv.spec)

        source = Source(integration="agentcrash.replay")
        ctx = ReplayContext(self.storage, new_run_id, project_id, agent=agent_name, model=model_name,
                            source=source, fixture=fixture, config=config, original_input=original_input)

        agent_output: Any = None
        status = "completed"
        error: str | None = None
        try:
            agent_output = agent_fn(original_input, ctx)
        except ReplayFrozenError as exc:
            # Faithful reproduction: surface the ORIGINAL exception type/message,
            # not the internal ReplayFrozenError wrapper.
            status = "failed"
            etype = (exc.error or {}).get("type", "Failed")
            emsg = (exc.error or {}).get("message", str(exc))
            error = f"{etype}: {emsg}"
        except Exception as exc:
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"
        ctx.close(status=status, error=error)

        new_events = self.storage.get_events(new_run_id)
        d = diff_runs(original_events, new_events)
        self.storage.finish_replay(replay_id, status, {
            "agent_output": agent_output, "error": error,
            "diff_lines": d.lines, "result_changed": d.result_changed,
        })
        return ReplayResult(
            replay_id=replay_id, new_run_id=new_run_id, original_run_id=original_run_id,
            status=status, agent_output=agent_output, error=error, diff_lines=d.lines, diff=d,
            events=new_events,
        )