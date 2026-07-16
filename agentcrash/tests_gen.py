"""Regression test generation and execution.

Turns a failure into a behavioral test. A test is NOT "does the output text
match" — it asserts agent *invariants* over the trace: side-effecting actions
must be preceded by a verification step, forbidden actions must not occur,
required actions must occur, and the run must succeed (or fail safely) within
retry/latency budgets. Tests run the agent against the frozen fixture so they
are reproducible and offline.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agentcrash.interventions import Intervention
from agentcrash.replay import ReplayConfig, Replayer
from agentcrash.schema import AgentCrashEvent, EventType


@dataclass
class TestSpec:
    # Tell pytest this dataclass is not a test class (name starts with "Test").
    __test__ = False
    name: str
    input: Any
    source_run_id: str | None = None  # fixture source
    invariants: list[dict[str, Any]] = field(default_factory=list)
    forbidden_actions: list[str] = field(default_factory=list)
    required_actions: list[str] = field(default_factory=list)
    must_succeed: bool = True
    max_retries: int | None = None
    max_latency_ms: int | None = None
    interventions: list[Intervention] = field(default_factory=list)
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "input": self.input, "source_run_id": self.source_run_id,
            "invariants": self.invariants, "forbidden_actions": self.forbidden_actions,
            "required_actions": self.required_actions, "must_succeed": self.must_succeed,
            "max_retries": self.max_retries, "max_latency_ms": self.max_latency_ms,
            "interventions": [{"type": iv.type, "fixture_key": iv.fixture_key,
                               "kind": iv.kind, "name": iv.name, "spec": iv.spec} for iv in self.interventions],
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TestSpec:
        ivs = [Intervention(id=f"iv-{i}", type=iv["type"], fixture_key=iv.get("fixture_key"),
                            kind=iv.get("kind"), name=iv.get("name"), spec=iv.get("spec", {}))
               for i, iv in enumerate(d.get("interventions", []))]
        return cls(
            name=d["name"], input=d.get("input"), source_run_id=d.get("source_run_id"),
            invariants=d.get("invariants", []), forbidden_actions=d.get("forbidden_actions", []),
            required_actions=d.get("required_actions", []), must_succeed=d.get("must_succeed", True),
            max_retries=d.get("max_retries"), max_latency_ms=d.get("max_latency_ms"),
            interventions=ivs, description=d.get("description", ""),
        )


@dataclass
class TestResult:
    test_name: str
    passed: bool
    run_id: str | None = None
    status: str = "not_run"
    violations: list[str] = field(default_factory=list)
    trace: list[AgentCrashEvent] = field(default_factory=list)


def generate_test(report, run: dict[str, Any] | None, original_input: Any) -> TestSpec:
    """Build a TestSpec from a FailureReport. Captures the bug as invariants."""
    inv = report.suggested_invariant
    invariants: list[dict[str, Any]] = []
    forbidden: list[str] = []
    if inv:
        invariants.append(inv)
        # the invariant's action-without-preceding is the forbidden behavior
        forbidden.append(f"{inv.get('action')} without preceding {inv.get('preceding')}")
    # always assert the run must succeed (the fixed agent should complete)
    name = "regression_" + (report.run_id or "unknown")[:8]
    if inv:
        name = f"require_{inv.get('preceding')}_before_{inv.get('action')}"
    return TestSpec(
        name=name,
        input=original_input,
        source_run_id=report.run_id,
        invariants=invariants,
        forbidden_actions=forbidden,
        required_actions=[inv.get("preceding")] if inv else [],
        must_succeed=True,
        description=report.recommended_fix or report.root_cause or "",
    )


def run_test(spec: TestSpec, agent_fn: Callable[[Any, Any], Any],
             replayer: Replayer) -> TestResult:
    """Re-run the agent against the frozen fixture and evaluate invariants."""
    if not spec.source_run_id:
        return TestResult(test_name=spec.name, passed=False, violations=["test has no source_run_id fixture"])
    config = ReplayConfig(mode="selective", interventions=spec.interventions)
    try:
        result = replayer.replay(spec.source_run_id, agent_fn, spec.input, config)
    except Exception as exc:  # noqa: BLE001
        return TestResult(test_name=spec.name, passed=False, status="error",
                          violations=[f"replay failed to execute: {exc}"])

    trace = result.events
    violations = _evaluate(spec, trace, result.status)
    passed = not violations
    return TestResult(test_name=spec.name, passed=passed, run_id=result.new_run_id,
                      status=result.status, violations=violations, trace=trace)


def _action_names(trace: list[AgentCrashEvent]) -> list[tuple[str, int]]:
    """Ordered [(name, seq)] of tool calls + explicit decisions."""
    out = []
    for e in trace:
        if e.type in (EventType.TOOL_CALLED.value, EventType.AGENT_DECISION.value,
                      EventType.MCP_REQUEST.value):
            out.append((e.name or e.type, e.seq))
    return out


def _evaluate(spec: TestSpec, trace: list[AgentCrashEvent], status: str) -> list[str]:
    violations: list[str] = []
    actions = _action_names(trace)
    names_in_order = [a[0] for a in actions]

    for inv in spec.invariants:
        t = inv.get("type")
        if t == "action_requires_preceding":
            action = inv.get("action")
            preceding = inv.get("preceding")
            for name, seq in actions:
                if name == action:
                    has_preceding = any(n == preceding and s < seq for n, s in actions)
                    if not has_preceding:
                        violations.append(
                            f"{action} called (seq {seq}) without preceding {preceding} — {inv.get('reason','')}"
                        )
        elif t == "forbidden_action":
            if inv.get("action") in names_in_order:
                violations.append(f"forbidden action {inv.get('action')} was called")
        elif t == "required_action":
            if inv.get("action") not in names_in_order:
                violations.append(f"required action {inv.get('action')} was not called")

    for fb in spec.forbidden_actions:
        # forbidden entries may be composite ("X without preceding Y"); the invariant already covers those.
        if " without " not in fb and fb in names_in_order:
            violations.append(f"forbidden action {fb} was called")

    for req in spec.required_actions:
        if req and req not in names_in_order:
            violations.append(f"required action {req} was not called")

    if spec.must_succeed and status != "completed":
        violations.append(f"run did not succeed (status={status})")

    retries = sum(1 for e in trace if e.type == EventType.TOOL_CALLED.value and e.status == "started") - len(
        [e for e in trace if e.type == EventType.TOOL_COMPLETED.value]
    )
    if spec.max_retries is not None and retries > spec.max_retries:
        violations.append(f"retries {retries} exceeded max {spec.max_retries}")

    return violations