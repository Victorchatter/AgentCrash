"""Replayable wrapper for OpenAI-compatible chat clients.

The differentiating AgentCrash value is *replay* — and the SDK's
``call_external(kind="llm", fn=...)`` rail already makes any LLM call
replayable. This adapter auto-instruments the most common shape, the
``client.chat.completions.create(...)`` call, by swapping in a recording
``completions`` object — no subclassing, no dependency on the ``openai``
package (the client is duck-typed: anything with a ``create(**kwargs)``
method works).::

    from agentcrash.sdk import CrashTracer
    from agentcrash.integrations.openai_client import wrap

    tracer = CrashTracer(integration="my-agent", framework="openai")
    with tracer.run("agent", model="gpt-4o") as run:
        client = wrap(run, openai.OpenAI())          # records on the rail
        resp = client.chat.completions.create(model="gpt-4o", messages=[...])

Recorded calls are **replayable as-is**: exact replay returns the frozen
response without calling the real client; a counterfactual
``replace_llm_output`` intervention swaps it. Args (including message content
and any ``extra_headers``) are redacted before storage via ``call_external``.

This closes the replay gap the observational adapters (OTel importer, LangChain
callback handler) flag — those record what happened; this makes the LLM call
itself replayable and counterfactual-ready.
"""

from __future__ import annotations

from typing import Any

from agentcrash.schema import Actor, ActorType, EventType

__all__ = ["RecordingChatCompletions", "wrap"]


class RecordingChatCompletions:
    """A recording stand-in for a ``chat.completions`` object.

    ``ctx`` is a :class:`~agentcrash.sdk.RunContext` (record) or the matching
    replay context; both expose ``call_external`` with the same signature, so
    the same wrapper works in record and replay. ``completions`` is the real
    ``client.chat.completions`` (duck-typed: must have ``create(**kwargs)``).
    """

    def __init__(self, ctx: Any, completions: Any, *, name: str = "llm"):
        self.ctx = ctx
        self._completions = completions
        self.name = name

    def create(self, **kwargs: Any) -> Any:
        return self.ctx.call_external(
            kind="llm",
            name=self.name,
            signature=kwargs,
            fn=lambda: self._completions.create(**kwargs),
            request_type=EventType.LLM_REQUEST.value,
            response_type=EventType.LLM_RESPONSE.value,
            actor=Actor(type=ActorType.LLM, name=kwargs.get("model") or self.name),
            input_payload=kwargs,
        )


def wrap(ctx: Any, client: Any, *, name: str = "llm") -> Any:
    """Swap ``client.chat.completions`` for a recording wrapper, return client.

    ``client`` must expose ``client.chat.completions`` with a ``create`` method
    (the standard OpenAI SDK shape). The agent's existing
    ``client.chat.completions.create(...)`` calls are then recorded on the
    replay rail with no code changes at the call sites.
    """
    client.chat.completions = RecordingChatCompletions(ctx, client.chat.completions, name=name)
    return client