"""Counterfactual interventions for replay.

An :class:`Intervention` modifies a frozen fixture entry before the replayed
agent sees it. This is the mechanism for causal debugging: "what if the search
had returned a different record?" The engine is extensible — new intervention
types are registered in :data:`REGISTRY`.

Interventions target a frozen call by its ``fixture_key`` or by ``(kind,
name)``. ``replace_model`` and ``modify_prompt`` require a *live* LLM call
(see :mod:`agentcrash.replay`) and are marked accordingly.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

# Intervention types that need a live LLM provider, not just fixture edits.
LIVE_ONLY = {"replace_model", "modify_prompt"}


@dataclass
class FixtureEntry:
    """One frozen external response, keyed by fixture_key."""

    fixture_key: str
    kind: str
    name: str
    type: str
    output: Any = None
    status: str = "completed"
    error: dict[str, Any] | None = None
    call_signature: dict[str, Any] = field(default_factory=dict)


@dataclass
class Intervention:
    id: str
    type: str
    # target: fixture_key, or (kind, name) resolved at apply time
    fixture_key: str | None = None
    kind: str | None = None
    name: str | None = None
    spec: dict[str, Any] = field(default_factory=dict)

    def matches(self, entry: FixtureEntry) -> bool:
        if self.fixture_key:
            return entry.fixture_key == self.fixture_key
        if self.kind and self.name:
            return entry.kind == self.kind and entry.name == self.name
        if self.name:
            return entry.name == self.name
        return False

    def is_live_only(self) -> bool:
        return self.type in LIVE_ONLY


def _apply(entry: FixtureEntry, intervention: Intervention) -> FixtureEntry:
    e = copy.deepcopy(entry)
    t = intervention.type
    spec = intervention.spec
    if t in ("replace_tool_response", "replace_llm_output", "replace_response"):
        e.output = spec.get("response")
        e.status = "completed"
        e.error = None
    elif t in ("modify_tool_response", "modify_response"):
        if isinstance(e.output, dict) and isinstance(spec.get("merge"), dict):
            e.output = {**e.output, **spec["merge"]}
        elif "response" in spec:
            e.output = spec["response"]
    elif t == "inject_failure":
        e.status = "failed"
        e.error = {
            "type": spec.get("error_type", "InjectedFault"),
            "message": spec.get("message", "AgentCrash injected failure"),
        }
        if spec.get("clear_output", True):
            e.output = None
    elif t == "inject_timeout":
        e.status = "failed"
        e.error = {"type": "Timeout", "message": f"Timed out after {spec.get('ms', 30000)}ms (injected)"}
        e.output = None
    elif t == "remove_tool":
        e.status = "failed"
        e.error = {"type": "ToolMissing", "message": f"Tool '{entry.name}' removed by intervention"}
        e.output = None
    elif t in ("replace_model", "modify_prompt"):
        # Handled in replay when the LLM call is made live.
        pass
    else:
        raise ValueError(f"unknown intervention type: {t}")
    return e


def apply_interventions(entries: dict[str, FixtureEntry],
                        interventions: list[Intervention]) -> dict[str, FixtureEntry]:
    """Return a new fixture map with interventions applied to matching entries."""
    out = {k: copy.deepcopy(v) for k, v in entries.items()}
    for iv in interventions:
        if iv.is_live_only():
            continue
        for key, entry in out.items():
            if iv.matches(entry):
                out[key] = _apply(entry, iv)
    return out


def find_target_key(entries: dict[str, FixtureEntry], intervention: Intervention) -> str | None:
    for key, entry in entries.items():
        if intervention.matches(entry):
            return key
    return None