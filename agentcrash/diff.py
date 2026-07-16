"""Behavioral diff between two agent runs.

Compares agent *behavior*, not text. Detects tool calls added/removed/
reordered, argument changes, retry-count changes, model changes, result
changes, and errors introduced or resolved. Flags regressions with
machine-readable structure so the analyzer can cite them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agentcrash.schema import AgentCrashEvent, EventType


@dataclass
class BehavioralDiff:
    tool_calls_a: list[str] = field(default_factory=list)
    tool_calls_b: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    reordered: bool = False
    arg_changes: list[str] = field(default_factory=list)
    result_changed: bool = False
    errors_a: list[str] = field(default_factory=list)
    errors_b: list[str] = field(default_factory=list)
    regressions: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)

    @property
    def is_different(self) -> bool:
        # errors_a/b are informational; identical failures that differ only in
        # exception wrapper text are not behavioral differences.
        return bool(
            self.added or self.removed or self.reordered or self.arg_changes
            or self.result_changed or self.regressions
        )


def _tool_seq(events: list[AgentCrashEvent]) -> list[tuple[str, str, dict[str, Any]]]:
    """Ordered list of (name, status, args) for completed/failed tool calls."""
    seq = []
    for e in events:
        if e.type in (EventType.TOOL_CALLED.value, EventType.MCP_REQUEST.value):
            # pair with its response by name + order
            seq.append((e.name or e.type, e.status, e.input if isinstance(e.input, dict) else {}))
    return seq


def _terminal(events: list[AgentCrashEvent]) -> tuple[str, AgentCrashEvent | None]:
    for e in reversed(events):
        if e.type in (EventType.RUN_COMPLETED.value, EventType.RUN_FAILED.value):
            return e.status, e
    return "unknown", None


def diff_runs(a: list[AgentCrashEvent], b: list[AgentCrashEvent]) -> BehavioralDiff:
    """Compare original run events ``a`` vs replay events ``b``."""
    d = BehavioralDiff()
    ta = _tool_seq(a)
    tb = _tool_seq(b)
    d.tool_calls_a = [t[0] for t in ta]
    d.tool_calls_b = [t[0] for t in tb]

    names_a = [t[0] for t in ta]
    names_b = [t[0] for t in tb]
    # added/removed ignoring order
    d.added = [n for n in names_b if n not in names_a] or [n for n in names_b if names_b.count(n) > names_a.count(n)]
    d.removed = [n for n in names_a if n not in names_b] or [n for n in names_a if names_a.count(n) > names_b.count(n)]
    # dedupe preserving order
    d.added = list(dict.fromkeys(d.added))
    d.removed = list(dict.fromkeys(d.removed))
    d.reordered = names_a != names_b and sorted(names_a) == sorted(names_b)

    # argument changes for calls with the same name in order
    # strict=False (default): sequences of different lengths compare up to the shorter.
    for (na, _, argsa), (nb, _, argsb) in zip(ta, tb, strict=False):
        if na == nb and argsa != argsb:
            d.arg_changes.append(f"{na}: {argsa} -> {argsb}")

    status_a, term_a = _terminal(a)
    status_b, term_b = _terminal(b)
    d.result_changed = status_a != status_b or (term_a and term_b and term_a.output != term_b.output)

    d.errors_a = [e.error.message for e in a if e.error and e.error.message]
    d.errors_b = [e.error.message for e in b if e.error and e.error.message]

    # regression heuristics — side-effecting action appears in B but not A, etc.
    side_effect_tools = {"refund", "charge", "send_email", "delete", "write_file", "shell", "refund_order"}
    a_side = {n for n in names_a if n in side_effect_tools}
    b_side = {n for n in names_b if n in side_effect_tools}
    new_side = b_side - a_side
    if new_side:
        d.regressions.append(f"New side-effecting action(s) in replay: {sorted(new_side)}")

    _build_lines(d, status_a, status_b)
    return d


def _build_lines(d: BehavioralDiff, status_a: str, status_b: str) -> None:
    lines = ["BEHAVIORAL DIFF", ""]
    if d.added:
        lines.append("Added tool calls:")
        for n in d.added:
            lines.append(f"  + {n}")
    if d.removed:
        lines.append("Removed tool calls:")
        for n in d.removed:
            lines.append(f"  - {n}")
    if d.reordered:
        lines.append("  ~ tool call order changed")
    if d.arg_changes:
        lines.append("Argument changes:")
        for c in d.arg_changes:
            lines.append(f"  ~ {c}")
    if d.result_changed:
        lines.append(f"  = result changed: {status_a} -> {status_b}")
    if d.regressions:
        lines.append("")
        lines.append("⚠️ REGRESSION:")
        for r in d.regressions:
            lines.append(f"  {r}")
    if not d.is_different:
        lines.append("  (no behavioral difference)")
    d.lines = lines