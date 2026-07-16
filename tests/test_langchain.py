"""LangChain/LangGraph callback adapter tests — dependency-free.

Drives ``AgentCrashCallbackHandler`` by calling its callback methods directly
with plain objects mimicking langchain's callback arguments (serialized dicts,
message objects, an LLMResult). No ``langchain``/``langgraph`` package required;
the handler is duck-typed. Asserts the canonical event mapping, lifecycle,
payload coercion, redaction, and searchability.
"""

from __future__ import annotations

from agentcrash.integrations.langchain import AgentCrashCallbackHandler
from agentcrash.schema import EventType
from agentcrash.sdk import CrashTracer

SECRET = "sk-ant-" + "q" * 24


# ---- langchain-shaped fakes (duck-typed; no langchain_core needed) ----
class _Msg:
    def __init__(self, content, type_="human"):
        self.content = content
        self.type = type_


class _Gen:
    def __init__(self, message=None, text=None):
        self.message = message
        self.text = text


class _LLMResult:
    def __init__(self, generations):
        self.generations = generations


def _run(tmp_storage, fn, *, agent="agent", model="gpt-4o"):
    tracer = CrashTracer(tmp_storage, integration="langchain", framework="langchain")
    with tracer.run(agent, model=model, project="p") as run:
        fn(AgentCrashCallbackHandler(run))
    return run


def test_records_llm_tool_chain_lifecycle(tmp_storage):
    def go(h):
        h.on_chain_start({"name": "AgentExecutor"}, {"input": "hi"}, run_id="r1")
        h.on_chat_model_start(
            {"name": "ChatOpenAI", "kwargs": {"model": "gpt-4o"}},
            [[_Msg("hello")]],
            run_id="r2", parent_run_id="r1",
        )
        h.on_llm_end(_LLMResult([[_Gen(message=_Msg("refund approved", "ai"))]]), run_id="r2")
        h.on_tool_start({"name": "refund_order"}, '{"order_id":"CUST-001"}', run_id="r3", parent_run_id="r1")
        h.on_tool_end({"order_id": "CUST-001", "refunded": True}, run_id="r3")
        h.on_chain_end({"output": "done"}, run_id="r1")

    run = _run(tmp_storage, go)
    events = tmp_storage.get_events(run.run_id)
    types = [e.type for e in events]
    assert EventType.RUN_STARTED.value in types
    assert EventType.RUN_COMPLETED.value in types
    assert EventType.LLM_RESPONSE.value in types
    assert EventType.TOOL_COMPLETED.value in types
    assert EventType.AGENT_DECISION.value in types

    llm = next(e for e in events if e.type == EventType.LLM_RESPONSE.value)
    assert llm.metadata["model"] == "gpt-4o"
    assert llm.output == [{"type": "ai", "content": "refund approved"}]
    assert llm.duration_ms is not None
    tool = next(e for e in events if e.type == EventType.TOOL_COMPLETED.value)
    assert tool.name == "refund_order"
    assert tool.output == {"order_id": "CUST-001", "refunded": True}


def test_tool_error_records_failed_event(tmp_storage):
    def go(h):
        h.on_tool_start({"name": "refund_order"}, "{}", run_id="t1")
        h.on_tool_error(TimeoutError("gateway down"), run_id="t1")

    run = _run(tmp_storage, go)
    events = tmp_storage.get_events(run.run_id)
    tf = next(e for e in events if e.type == EventType.TOOL_FAILED.value)
    assert tf.status == "failed"
    assert tf.error.type == "TimeoutError"
    assert tf.error.message == "gateway down"


def test_secret_in_prompt_redacted(tmp_storage):
    def go(h):
        h.on_chat_model_start(
            {"name": "ChatOpenAI", "kwargs": {"model": "gpt-4o"}},
            [[_Msg(f"Authorization: Bearer {SECRET}\nsummarize")]],
            run_id="r2",
        )
        h.on_llm_end(_LLMResult([[_Gen(text="ok")]]), run_id="r2")

    run = _run(tmp_storage, go)
    events = tmp_storage.get_events(run.run_id)
    llm = next(e for e in events if e.type == EventType.LLM_RESPONSE.value)
    assert llm.privacy.redacted is True
    blob = str([e.model_dump() for e in events])
    assert SECRET not in blob
    assert "Bearer" in blob or "REDACTED" in blob  # prompt preserved minus the secret


def test_retriever_recorded(tmp_storage):
    def go(h):
        h.on_retriever_start({"name": "vector_store"}, "what is agentcrash", run_id="r1")
        h.on_retriever_end([{"content": "a debugger for agents"}], run_id="r1")

    run = _run(tmp_storage, go)
    events = tmp_storage.get_events(run.run_id)
    ret = next(e for e in events if e.type == EventType.RETRIEVAL_COMPLETED.value)
    assert ret.output == [{"content": "a debugger for agents"}]


def test_trace_search_finds_callback_content(tmp_storage):
    def go(h):
        h.on_chat_model_start({"name": "ChatOpenAI", "kwargs": {"model": "gpt-4o"}},
                              [[_Msg("plan")]], run_id="r2")
        h.on_llm_end(_LLMResult([[_Gen(text="approved the refund")]]), run_id="r2")

    run = _run(tmp_storage, go)
    hits = tmp_storage.search_events(run.run_id, "approved")
    assert hits
    assert any("approved" in str(h.output).lower() for h in hits)


def test_import_does_not_require_langchain():
    """The adapter must import cleanly with no langchain installed."""
    import importlib

    mod = importlib.import_module("agentcrash.integrations.langchain")
    assert mod.AgentCrashCallbackHandler is AgentCrashCallbackHandler