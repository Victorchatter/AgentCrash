"""Replayable LLM client wrapper tests — dependency-free.

Drives ``RecordingChatCompletions`` / ``wrap`` with a FAKE OpenAI-compatible
client (duck-typed ``.chat.completions.create``). No ``openai`` package needed.
Asserts the full record -> exact replay -> counterfactual loop, redaction,
and that exact replay never calls the real client.
"""

from __future__ import annotations

from agentcrash.integrations.openai_client import RecordingChatCompletions, wrap
from agentcrash.interventions import Intervention
from agentcrash.replay import ReplayConfig, Replayer
from agentcrash.sdk import CrashTracer, _fixture_key

SECRET = "sk-ant-" + "q" * 24


class _FakeCompletions:
    def __init__(self, response):
        self._response = response
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        return self._response


class _FakeChat:
    def __init__(self, completions):
        self.completions = completions


class _FakeClient:
    def __init__(self, completions):
        self.chat = _FakeChat(completions)


RESPONSE = {"choices": [{"message": {"content": "hello"}}]}


def _record(tmp_storage, kwargs=None):
    kwargs = kwargs or {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
    tracer = CrashTracer(tmp_storage, integration="openai", framework="openai")
    completions = _FakeCompletions(RESPONSE)
    client = _FakeClient(completions)
    with tracer.run("agent", model="gpt-4o", project="p") as run:
        wrapped = wrap(run, client)
        out = wrapped.chat.completions.create(**kwargs)
    return run, out, completions, kwargs


def test_record_and_exact_replay_returns_frozen_without_calling_client(tmp_storage):
    run, out, completions, _ = _record(tmp_storage)
    assert out == RESPONSE
    assert completions.calls == 1  # called once during record

    # exact replay: must return the frozen response WITHOUT calling the client.
    completions.calls = 0  # reset; create would raise if touched during replay
    completions.create = lambda **kw: (_ for _ in ()).throw(AssertionError("client must not be called in exact replay"))
    replayer = Replayer(tmp_storage)

    def agent(request, ctx):
        c = RecordingChatCompletions(ctx, completions)
        return c.create(model="gpt-4o", messages=[{"role": "user", "content": "hi"}])

    result = replayer.replay(run.run_id, agent, {}, ReplayConfig(mode="exact"))
    assert result.status == "completed"
    assert result.agent_output == RESPONSE
    assert completions.calls == 0


def test_counterfactual_replace_llm_output(tmp_storage):
    run, _, completions, kwargs = _record(tmp_storage)
    replayer = Replayer(tmp_storage)
    replaced = {"choices": [{"message": {"content": "fixed-counterfactual"}}]}

    def agent(request, ctx):
        c = RecordingChatCompletions(ctx, completions)
        return c.create(**kwargs)

    fkey = _fixture_key("llm", "llm", kwargs)
    iv = Intervention(id="cf", type="replace_llm_output", fixture_key=fkey, spec={"response": replaced})
    result = replayer.replay(run.run_id, agent, {}, ReplayConfig(mode="selective", interventions=[iv]))
    assert result.agent_output == replaced


def test_bearer_token_in_messages_redacted(tmp_storage):
    kwargs = {"model": "gpt-4o",
              "messages": [{"role": "user", "content": f"Authorization: Bearer {SECRET}\ndo thing"}]}
    run, _, _, _ = _record(tmp_storage, kwargs)
    events = tmp_storage.get_events(run.run_id)
    blob = str([e.model_dump() for e in events])
    assert SECRET not in blob
    llm = next(e for e in events if e.type == "llm.response")
    assert llm.privacy.redacted is True
    assert llm.output == RESPONSE  # response preserved