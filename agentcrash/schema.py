"""Canonical AgentCrash event model — ``agentcrash.schema.v1``.

Every integration maps foreign agent activity into :class:`AgentCrashEvent`.
The schema is the single point of normalization: frameworks may come and go,
this shape does not.

Design rules:

* Observable behavior only. **Never** record private chain-of-thought.
* Parent/child spans via ``parent_id``; temporal order via ``timestamp`` + ``seq``.
* Large payloads are referenced as :class:`Artifact` rows, not inlined.
* Replayability is explicit: ``replay.frozen`` marks events whose external
  response is captured and can be replayed deterministically.
* The schema is versioned (``schema_version``) and additive: new optional
  fields are allowed; existing field semantics never change.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "agentcrash.schema.v1"


class ActorType(str, Enum):
    AGENT = "agent"
    LLM = "llm"
    TOOL = "tool"
    USER = "user"
    SYSTEM = "system"


class EventStatus(str, Enum):
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"


class EventType(str, Enum):
    # Run lifecycle
    RUN_STARTED = "run.started"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"

    # Model
    LLM_REQUEST = "llm.request"
    LLM_RESPONSE = "llm.response"

    # Agent observable decisions (NOT hidden reasoning)
    AGENT_DECISION = "agent.decision"

    # Tools
    TOOL_CALLED = "tool.called"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"

    # MCP
    MCP_REQUEST = "mcp.request"
    MCP_RESPONSE = "mcp.response"
    MCP_ERROR = "mcp.error"

    # Environment / side effects
    FILESYSTEM_READ = "filesystem.read"
    FILESYSTEM_WRITE = "filesystem.write"
    SHELL_COMMAND = "shell.command"
    SHELL_STDOUT = "shell.stdout"
    SHELL_STDERR = "shell.stderr"
    BROWSER_NAVIGATION = "browser.navigation"
    BROWSER_CLICK = "browser.click"
    BROWSER_TYPE = "browser.type"
    HTTP_REQUEST = "http.request"
    HTTP_RESPONSE = "http.response"

    # State
    MEMORY_READ = "memory.read"
    MEMORY_WRITE = "memory.write"
    RETRIEVAL_STARTED = "retrieval.started"
    RETRIEVAL_COMPLETED = "retrieval.completed"

    # Human in the loop
    HUMAN_APPROVAL_REQUESTED = "human.approval.requested"
    HUMAN_APPROVAL_GRANTED = "human.approval.granted"
    HUMAN_APPROVAL_DENIED = "human.approval.denied"

    # Catch-all
    ERROR_RAISED = "error.raised"


class Source(BaseModel):
    """Where this event came from — the integration that produced it."""

    model_config = ConfigDict(frozen=True)

    integration: str = "generic-python"
    framework: str | None = None
    version: str | None = None


class Actor(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: ActorType
    name: str | None = None


class ErrorInfo(BaseModel):
    type: str | None = None
    message: str | None = None
    stack: str | None = None


class Privacy(BaseModel):
    """Redaction metadata. ``redacted=True`` means sensitive data was scrubbed."""

    redacted: bool = False
    redaction_types: list[str] = Field(default_factory=list)


class Artifact(BaseModel):
    """A referenced payload too large or binary to inline in the event."""

    id: str
    kind: str  # e.g. "llm.response.body", "shell.stdout", "file.content"
    mime: str = "application/json"
    size: int = 0
    sha256: str | None = None
    # Storage-relative path; resolved by the storage layer.
    path: str | None = None


class ReplayMeta(BaseModel):
    """Marks an event as replayable and how its external response is frozen."""

    replayable: bool = False
    frozen: bool = False  # response captured verbatim and replayed as-is
    fixture_key: str | None = None  # signature used to look up the frozen response
    call_signature: dict[str, Any] | None = None


class AgentCrashEvent(BaseModel):
    """A single normalized, replayable agent event. The unit of everything."""

    model_config = ConfigDict(use_enum_values=True)

    schema_version: str = SCHEMA_VERSION
    id: str
    trace_id: str
    parent_id: str | None = None
    seq: int = 0
    timestamp: int  # epoch milliseconds
    duration_ms: int | None = None

    type: str  # EventType value, kept as str for forward-compat
    name: str | None = None

    source: Source = Field(default_factory=Source)
    actor: Actor | None = None

    input: Any | None = None
    output: Any | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    status: str = EventStatus.COMPLETED.value
    error: ErrorInfo | None = None

    privacy: Privacy = Field(default_factory=Privacy)
    artifacts: list[Artifact] = Field(default_factory=list)
    replay: ReplayMeta | None = None

    @property
    def is_error(self) -> bool:
        return self.status == EventStatus.FAILED.value or self.error is not None


# Event types that represent external calls whose response can be frozen and
# replayed deterministically. Used by the replay engine + fixture builder.
REPLAYABLE_TYPES: frozenset[str] = frozenset(
    {
        EventType.LLM_RESPONSE.value,
        EventType.TOOL_COMPLETED.value,
        EventType.MCP_RESPONSE.value,
        EventType.HTTP_RESPONSE.value,
        EventType.RETRIEVAL_COMPLETED.value,
        EventType.SHELL_COMMAND.value,  # stdout/stderr frozen alongside
        EventType.FILESYSTEM_READ.value,
    }
)

# Event types whose payload may contain secrets and must pass redaction.
REDACTABLE_TYPES: frozenset[str] = frozenset(
    {
        EventType.LLM_REQUEST.value,
        EventType.LLM_RESPONSE.value,
        EventType.TOOL_CALLED.value,
        EventType.TOOL_COMPLETED.value,
        EventType.MCP_REQUEST.value,
        EventType.MCP_RESPONSE.value,
        EventType.HTTP_REQUEST.value,
        EventType.HTTP_RESPONSE.value,
        EventType.SHELL_COMMAND.value,
        EventType.SHELL_STDOUT.value,
        EventType.SHELL_STDERR.value,
        EventType.FILESYSTEM_READ.value,
        EventType.FILESYSTEM_WRITE.value,
        EventType.MEMORY_READ.value,
        EventType.MEMORY_WRITE.value,
    }
)