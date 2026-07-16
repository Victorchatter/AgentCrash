"""Chaos engine — controlled fault injection for agent reliability.

A chaos test injects a single fault into a frozen replay and checks the agent's
steady-state invariants: did it recover (complete), retry, or avoid duplicate
side effects? Faults are reproducible because they run against the frozen
fixture. Full reliability scoring across fault classes is on the roadmap; this
implements the core: inject, replay, observe.

Chaos test spec (dict / YAML-shaped)::

    name: tool_timeout_recovery
    target: { kind: tool, name: refund_order }
    fault: { type: inject_timeout, ms: 30000 }
    expected:
      must_recover: true        # agent run still completes
      max_retries: 3
      avoid_duplicate_side_effects: true
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agentcrash.interventions import Intervention
from agentcrash.replay import ReplayConfig, Replayer
from agentcrash.schema import EventType


@dataclass
class ChaosResult:
    name: str
    fault: str
    passed: bool
    status: str
    run_id: str | None = None
    observations: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)


def run_chaos(spec: dict[str, Any], agent_fn: Callable[[Any, Any], Any],
              original_input: Any, source_run_id: str, replayer: Replayer) -> ChaosResult:
    name = spec.get("name", "unnamed_chaos")
    target = spec.get("target", {})
    fault = spec.get("fault", {})
    expected = spec.get("expected", {})

    iv = Intervention(
        id=f"chaos-{name}",
        type=fault.get("type", "inject_failure"),
        kind=target.get("kind"),
        name=target.get("name"),
        spec=fault,
    )
    config = ReplayConfig(mode="selective", interventions=[iv])
    try:
        result = replayer.replay(source_run_id, agent_fn, original_input, config)
    except Exception as exc:  # noqa: BLE001
        return ChaosResult(name=name, fault=iv.type, passed=False, status="error",
                           violations=[f"replay failed: {exc}"])

    trace = result.events
    observations: list[str] = []
    violations: list[str] = []

    tool_calls = [e for e in trace if e.type == EventType.TOOL_CALLED.value]
    completed = [e for e in trace if e.type == EventType.TOOL_COMPLETED.value]
    side_effect_names = {e.name for e in tool_calls if e.name and _is_side_effect(e.name)}
    # duplicate side effects = same side-effecting tool called >1 time with identical args
    seen: dict[tuple[str, str], int] = {}
    duplicates: list[str] = []
    for e in tool_calls:
        if e.name in side_effect_names:
            key = (e.name, str(e.input))
            seen[key] = seen.get(key, 0) + 1
            if seen[key] == 2:
                duplicates.append(e.name)
    if duplicates:
        observations.append(f"Duplicate side-effect calls: {duplicates}")

    recovered = result.status == "completed"
    observations.append(f"Recovered (run completed): {recovered}")

    if expected.get("must_recover") and not recovered:
        violations.append("agent did not recover from the fault")
    if expected.get("avoid_duplicate_side_effects") and duplicates:
        violations.append("agent performed duplicate side-effecting calls under fault")
    max_retries = expected.get("max_retries")
    if max_retries is not None:
        retries = max(0, len(tool_calls) - len(completed))
        if retries > max_retries:
            violations.append(f"retries {retries} exceeded expected max {max_retries}")

    return ChaosResult(
        name=name, fault=iv.type, passed=not violations, status=result.status,
        run_id=result.new_run_id, observations=observations, violations=violations,
    )


_SIDE_EFFECT_STEMS = ("refund", "charge", "delete", "write", "send", "update", "create", "submit", "transfer")


def _is_side_effect(name: str) -> bool:
    n = (name or "").lower()
    return any(stem in n for stem in _SIDE_EFFECT_STEMS)