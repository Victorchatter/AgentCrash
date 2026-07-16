"""AgentCrash — the open-source crash debugger and reliability lab for AI agents.

Record → Replay → Analyze → Intervene → Fix.

This package exposes the canonical event model, an in-process tracer (SDK),
local SQLite storage, a replay engine with counterfactual interventions,
behavioral diffing, causal failure analysis, chaos/fault injection, and a
FastAPI server + CLI. It is framework-agnostic: integrations map foreign
agent events into the canonical :mod:`agentcrash.schema` model.
"""

from agentcrash.schema import (
    SCHEMA_VERSION,
    Actor,
    ActorType,
    AgentCrashEvent,
    Artifact,
    ErrorInfo,
    EventStatus,
    EventType,
    Privacy,
    ReplayMeta,
    Source,
)

__version__ = "0.1.0"

__all__ = [
    "SCHEMA_VERSION",
    "Actor",
    "ActorType",
    "AgentCrashEvent",
    "Artifact",
    "ErrorInfo",
    "EventStatus",
    "EventType",
    "Privacy",
    "ReplayMeta",
    "Source",
    "__version__",
]