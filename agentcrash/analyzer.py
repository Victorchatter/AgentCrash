"""Causal failure analysis.

NOT "ask an LLM why this failed." The analyzer gathers evidence first:

1. Identify candidate causal events (replayable responses preceding the failure).
2. For each candidate, run controlled counterfactual replays (selective mode:
   freeze side-effecting tools, re-run the LLM decision live against a modified
   state) and observe whether the failure is averted.
3. Rank candidates by how decisively each intervention changes the outcome.
4. Emit a root-cause report with cited event IDs and a confidence score derived
   from the evidence, not from a model.

The disambiguation strategy is generic: when a candidate tool returned a list,
try replaying with each singleton subset. If exactly one subset averts the
failure, that identifies the pivotal record and the root cause is "ambiguous
selection." This is the technique that cracks the demo failure.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agentcrash.interventions import Intervention
from agentcrash.replay import Replayer
from agentcrash.schema import AgentCrashEvent, EventType


@dataclass
class Evidence:
    description: str
    event_id: str | None
    averted: bool  # did this intervention prevent the failure?
    replay_run_id: str | None = None


@dataclass
class Candidate:
    event_id: str
    event_type: str
    name: str
    score: float = 0.0
    evidence: list[Evidence] = field(default_factory=list)
    averted: bool = False
    # v2: structured per-singleton disambiguation results, for combination
    # probing. Each entry: {index, item, averted, fixture_key}.
    singleton_results: list[dict] = field(default_factory=list)


@dataclass
class FailureReport:
    run_id: str
    failed: bool
    root_cause: str | None = None
    confidence: float = 0.0
    candidates: list[Candidate] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    recommended_fix: str | None = None
    suggested_invariant: dict[str, Any] | None = None
    summary: list[str] = field(default_factory=list)
    # v2: a multi-root failure — no single candidate averts, but a combination
    # of interventions across >=2 candidates does.
    multi_root: bool = False
    root_events: list[str] = field(default_factory=list)


# ponytail: side-effecting tool names recognized for fix suggestions.
# Ceiling: a real system learns these per-project; here we hardcode the common
# financial/write actions and derive the rest from name stems (refund, charge,
# delete, write, send, update, create).
_SIDE_EFFECT_STEMS = ("refund", "charge", "delete", "write", "send", "update", "create", "submit", "transfer")


def _is_side_effect(name: str) -> bool:
    n = (name or "").lower()
    return any(stem in n for stem in _SIDE_EFFECT_STEMS)


class Analyzer:
    def __init__(self, storage: StorageLike, replayer: Replayer):
        self.storage = storage
        self.replayer = replayer

    def analyze(self, run_id: str, agent_fn: Callable[[Any, Any], Any],
                original_input: Any, max_candidates: int = 5) -> FailureReport:
        events = self.storage.get_events(run_id)
        run = self.storage.get_run(run_id)
        failed = bool(run is None or run.get("status") == "failed" or run.get("error"))
        report = FailureReport(run_id=run_id, failed=failed)

        if not failed:
            report.summary.append("Run did not fail; nothing to analyze.")
            return report

        failure_event = next((e for e in reversed(events) if e.is_error), None)
        if failure_event:
            report.evidence.append(Evidence(
                description=f"Run terminated with error: {failure_event.error.message if failure_event.error else failure_event.type}",
                event_id=failure_event.id, averted=False))

        # candidate replayable responses that preceded the failure
        candidates_events = [
            e for e in events
            if e.replay and e.replay.frozen and e.type in (
                EventType.TOOL_COMPLETED.value, EventType.LLM_RESPONSE.value,
                EventType.MCP_RESPONSE.value, EventType.RETRIEVAL_COMPLETED.value)
        ][:max_candidates]

        for ce in candidates_events:
            cand = Candidate(event_id=ce.id, event_type=ce.type, name=ce.name or ce.type)
            self._probe_disambiguation(cand, ce, run_id, agent_fn, original_input, report)
            self._probe_drop(cand, ce, run_id, agent_fn, original_input, report)
            if cand.averted:
                cand.score = sum(1 for ev in cand.evidence if ev.averted) / max(1, len(cand.evidence))
            report.candidates.append(cand)

        report.candidates.sort(key=lambda c: c.score, reverse=True)
        self._probe_combinations(report, run_id, agent_fn, original_input)
        self._synthesize(report, events)
        return report

    def _probe_disambiguation(self, cand: Candidate, ce: AgentCrashEvent, run_id: str,
                              agent_fn: Callable[[Any, Any], Any], original_input: Any,
                              report: FailureReport) -> None:
        out = ce.output
        if not isinstance(out, list) or len(out) < 2:
            return
        from agentcrash.replay import ReplayConfig

        target_key = ce.replay.fixture_key if ce.replay else None
        averted_any = False
        for i, item in enumerate(out):
            iv = Intervention(id=f"cf-{ce.id}-{i}", type="replace_tool_response",
                              fixture_key=target_key, spec={"response": [item]})
            try:
                result = self.replayer.replay(
                    run_id, agent_fn, original_input,
                    ReplayConfig(mode="selective", interventions=[iv]),
                )
            except Exception as exc:  # noqa: BLE001
                cand.evidence.append(Evidence(
                    description=f"Replay with element #{i} only failed to execute: {exc}",
                    event_id=ce.id, averted=False, replay_run_id=None))
                continue
            averted = result.status == "completed"
            cand.evidence.append(Evidence(
                description=f"Replay with only {ce.name}#{i} ({_short(item)}) -> {result.status}",
                event_id=ce.id, averted=averted, replay_run_id=result.new_run_id))
            cand.singleton_results.append(
                {"index": i, "item": item, "averted": averted, "fixture_key": target_key})
            if averted:
                averted_any = True
                cand.averted = True
        if averted_any:
            report.evidence.extend(cand.evidence)

    def _probe_drop(self, cand: Candidate, ce: AgentCrashEvent, run_id: str,
                    agent_fn: Callable[[Any, Any], Any], original_input: Any,
                    report: FailureReport) -> None:
        from agentcrash.replay import ReplayConfig

        target_key = ce.replay.fixture_key if ce.replay else None
        iv = Intervention(id=f"drop-{ce.id}", type="inject_failure",
                          fixture_key=target_key, spec={"message": "AgentCrash dropped this call"})
        try:
            result = self.replayer.replay(
                run_id, agent_fn, original_input,
                ReplayConfig(mode="selective", interventions=[iv]),
            )
        except Exception:  # noqa: BLE001
            return
        averted = result.status == "completed"
        cand.evidence.append(Evidence(
            description=f"Replay with {ce.name} forced to fail -> {result.status} (averted={averted})",
            event_id=ce.id, averted=averted, replay_run_id=result.new_run_id))

    def _probe_combinations(self, report: FailureReport, run_id: str,
                            agent_fn: Callable[[Any, Any], Any], original_input: Any) -> None:
        """v2: detect multi-root failures.

        If no single candidate averts the failure, try bounded combinations of
        disambiguation singletons across >=2 list-output candidates. If a
        combination averts where every single candidate did not, the failure is
        multi-root (fixing any one call alone is insufficient). Bounded: only
        when 2-4 list candidates exist and the Cartesian product is <= 16 replays.
        Skipped entirely when any single candidate already averted (single-root
        case, e.g. the demo) — so this changes nothing for the common path.
        """
        import itertools

        from agentcrash.replay import ReplayConfig

        if any(c.averted for c in report.candidates):
            return
        list_cands = [c for c in report.candidates if len(c.singleton_results) >= 2]
        if len(list_cands) < 2:
            return
        if len(list_cands) > 4:
            list_cands = list_cands[:4]
        product = 1
        for c in list_cands:
            product *= len(c.singleton_results)
        if product > 16:
            return  # ponytail: bounded; a real system samples or ranks here.
        for combo in itertools.product(*[c.singleton_results for c in list_cands]):
            interventions = [
                Intervention(id=f"cf-combo-{i}", type="replace_tool_response",
                             fixture_key=sr["fixture_key"], spec={"response": [sr["item"]]})
                for i, sr in enumerate(combo)
            ]
            try:
                result = self.replayer.replay(
                    run_id, agent_fn, original_input,
                    ReplayConfig(mode="selective", interventions=interventions),
                )
            except Exception:  # noqa: BLE001
                continue
            if result.status == "completed":
                report.multi_root = True
                report.root_events = [c.event_id for c in list_cands]
                names = " and ".join(c.name for c in list_cands)
                ev_ids = ", ".join(report.root_events)
                report.evidence.append(Evidence(
                    description=f"Replay fixing both {names} ({ev_ids}) -> completed",
                    event_id=None, averted=True, replay_run_id=result.new_run_id))
                # record the non-averting singletons as reproducing evidence
                for c in list_cands:
                    for sr in c.singleton_results:
                        if not sr["averted"]:
                            report.evidence.append(Evidence(
                                description=f"Replay fixing only {c.name}#{sr['index']} -> failed",
                                event_id=c.event_id, averted=False))
                return

    def _synthesize(self, report: FailureReport, events: list[AgentCrashEvent]) -> None:
        if not report.candidates:
            report.root_cause = "No replayable candidate causes identified; failure may be in orchestration or environment."
            report.confidence = 0.2
            report.summary.append(report.root_cause)
            return

        if report.multi_root:
            names = ", ".join(
                next((e.name for e in events if e.id == eid), eid) for eid in report.root_events
            )
            report.root_cause = (
                f"Multi-root failure: the failure requires disambiguating more than one call "
                f"({names}; events {', '.join(report.root_events)}). Fixing any single call "
                f"alone does not avert it; fixing all of them does."
            )
            report.confidence = 0.82
            side_effects = [e.name for e in events if e.type == EventType.TOOL_CALLED.value
                            and _is_side_effect(e.name or "")]
            target = side_effects[0] if side_effects else "the side-effecting action"
            report.recommended_fix = (
                f"Require explicit identity/record resolution for each ambiguous call "
                f"({names}) before performing {target}."
            )
            report.suggested_invariant = {
                "type": "action_requires_preceding_multi",
                "action": target,
                "preceding": report.root_events,
                "reason": "multi-root ambiguous selection caused the failure",
            }
            report.summary = [
                f"ROOT CAUSE ANALYSIS — run {report.run_id}",
                "",
                f"Most likely cause: {report.root_cause}",
                f"Confidence: {int(report.confidence * 100)}%",
                "",
                "Evidence:",
            ]
            for ev in report.evidence[:8]:
                mark = "✅ averts" if ev.averted else "❌ reproduces"
                report.summary.append(f"  - [{mark}] {ev.description}")
            report.summary += ["", f"Recommended fix: {report.recommended_fix}"]
            return

        top = report.candidates[0]
        # A candidate "decisively" caused the failure if a disambiguation averted it.
        decisive = [c for c in report.candidates if c.averted]

        if decisive:
            cause = decisive[0]
            ce = next((e for e in events if e.id == cause.event_id), None)
            n = len(ce.output) if ce and isinstance(ce.output, list) else 0
            report.root_cause = (
                f"The agent selected the wrong record after an ambiguous {cause.name} result "
                f"(event {cause.event_id}, {n} candidates returned)."
            )
            # confidence: high if exactly one disambiguation averted it
            averters = sum(1 for ev in cause.evidence if ev.averted)
            report.confidence = min(0.97, 0.6 + 0.3 * (1 if averters == 1 else 0.5))
            # recommended fix + invariant: require identity resolution before the side effect
            side_effect = next((e for e in events if e.type == EventType.TOOL_CALLED.value
                                and _is_side_effect(e.name or "")), None)
            if side_effect:
                preceding = _suggest_verification(cause.name or "record")
                report.recommended_fix = (
                    f"Require explicit identity/record resolution (e.g. {preceding}) before "
                    f"performing {side_effect.name}."
                )
                report.suggested_invariant = {
                    "type": "action_requires_preceding",
                    "action": side_effect.name,
                    "preceding": preceding,
                    "reason": "ambiguous selection caused the failure",
                }
            else:
                report.recommended_fix = f"Disambiguate {cause.name} results before proceeding."
        else:
            report.root_cause = (
                f"Most likely cause involves {top.name} (event {top.event_id}); "
                f"counterfactual replays did not cleanly avert the failure."
            )
            report.confidence = 0.4

        report.summary = [
            f"ROOT CAUSE ANALYSIS — run {report.run_id}",
            "",
            f"Most likely cause: {report.root_cause}",
            f"Confidence: {int(report.confidence * 100)}%",
            "",
            "Evidence:",
        ]
        for ev in (cause.evidence if decisive else top.evidence)[:6]:
            mark = "✅ averts" if ev.averted else "❌ reproduces"
            report.summary.append(f"  - [{mark}] {ev.description}")
        if report.recommended_fix:
            report.summary.append("")
            report.summary.append(f"Recommended fix: {report.recommended_fix}")


def _short(item: Any) -> str:
    if isinstance(item, dict):
        return ", ".join(f"{k}={v}" for k, v in list(item.items())[:3])
    return str(item)[:60]


def _suggest_verification(search_name: str) -> str:
    # ponytail: heuristic name. "search_customer" -> "verify_customer"; default verify_identity.
    stem = (search_name or "").replace("search_", "").strip()
    return f"verify_{stem or 'identity'}"


# Minimal structural type to avoid importing Storage just for typing.
from typing import Protocol  # noqa: E402


class StorageLike(Protocol):  # pragma: no cover
    def get_events(self, run_id: str) -> list[AgentCrashEvent]: ...
    def get_run(self, run_id: str) -> dict[str, Any] | None: ...