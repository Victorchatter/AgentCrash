"""OTel GenAI span import — dependency-free OTLP/JSON -> agentcrash.schema.v1.

Synthesizes an OTLP/JSON bundle (chat + execute_tool, with a failing tool and a
bearer-token prompt) and asserts the import lane: run lifecycle, event types,
status, tokens, searchability, and redaction-at-ingest. No ``opentelemetry``
dependency; spans are plain dicts in the OTLP/JSON shape the Collector ``file``
exporter writes.
"""

from __future__ import annotations

import json

from agentcrash.integrations.otel import import_bundle, import_spans
from agentcrash.schema import EventType
from agentcrash.storage import Storage

TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
PARENT_SPAN = "bbb1234567abcdef"
CHAT_SPAN = "fff1234567abcdef"
TOOL_SPAN = "ccc1234567abcdef"


def _attr(key: str, value: object) -> dict:
    if isinstance(value, bool):
        v = {"boolValue": value}
    elif isinstance(value, int):
        v = {"intValue": str(value)}
    elif isinstance(value, float):
        v = {"doubleValue": value}
    else:
        v = {"stringValue": str(value)}
    return {"key": key, "value": v}


def _bundle() -> dict:
    """One trace: a chat span (child of root) then a failing execute_tool span.

    The chat prompt carries a bearer token; the tool fails with a timeout.
    """
    chat = {
        "traceId": TRACE_ID,
        "spanId": CHAT_SPAN,
        "parentSpanId": PARENT_SPAN,
        "name": "chat gpt-4o",
        "startTimeUnixNano": 1_700_000_000_000_000_000,
        "endTimeUnixNano": 1_700_000_000_500_000_000,
        "attributes": [
            _attr("gen_ai.operation.name", "chat"),
            _attr("gen_ai.request.model", "gpt-4o"),
            _attr("gen_ai.response.model", "gpt-4o-2024-08-06"),
            _attr("gen_ai.usage.input_tokens", 42),
            _attr("gen_ai.usage.output_tokens", 7),
            _attr("gen_ai.prompt", "Authorization: Bearer sk-ant-test1234567890abcdefghijkl\nsummarize"),
            _attr("gen_ai.completion", "done"),
        ],
    }
    tool = {
        "traceId": TRACE_ID,
        "spanId": TOOL_SPAN,
        "parentSpanId": CHAT_SPAN,
        "name": "refund_order",
        "startTimeUnixNano": 1_700_000_000_600_000_000,
        "endTimeUnixNano": 1_700_000_001_600_000_000,
        "status": {"code": "STATUS_CODE_ERROR", "message": "connection timeout"},
        "attributes": [
            _attr("gen_ai.operation.name", "execute_tool"),
            _attr("gen_ai.tool.name", "refund_order"),
            _attr("gen_ai.tool.input", '{"order_id": "CUST-001"}'),
            _attr("gen_ai.tool.output", "Error: timeout contacting payment gateway"),
            _attr("error.type", "TimeoutError"),
        ],
    }
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": [_attr("service.name", "billing-svc")]},
                "scopeSpans": [{"spans": [chat, tool]}],
            }
        ]
    }


def _fresh_storage(tmp_path) -> Storage:
    return Storage(str(tmp_path / "otel.db"))


def test_import_bundle_creates_run_with_lifecycle(tmp_path):
    storage = _fresh_storage(tmp_path)
    run_ids = import_bundle(storage, _bundle())
    assert run_ids == [TRACE_ID]
    run = storage.get_run(TRACE_ID)
    assert run is not None
    assert run["status"] == "failed"  # tool failed -> run failed
    assert run["agent"] == "billing-svc"
    assert run["model"] == "gpt-4o"
    events = storage.get_events(TRACE_ID)
    types = [e.type for e in events]
    assert EventType.RUN_STARTED.value in types
    assert EventType.RUN_FAILED.value in types
    assert EventType.LLM_RESPONSE.value in types
    assert EventType.TOOL_FAILED.value in types  # execute_tool + status error


def test_import_maps_tokens_and_payloads(tmp_path):
    storage = _fresh_storage(tmp_path)
    import_bundle(storage, _bundle())
    events = storage.get_events(TRACE_ID)
    chat = next(e for e in events if e.type == EventType.LLM_RESPONSE.value)
    assert chat.metadata["usage"] == {"input": 42, "output": 7}
    assert chat.metadata["model"] == "gpt-4o-2024-08-06"
    assert chat.output == "done"
    tool = next(e for e in events if e.type == EventType.TOOL_FAILED.value)
    assert tool.error is not None
    assert tool.error.type == "TimeoutError"
    assert tool.status == "failed"
    # parent/child links carried through
    assert tool.parent_id == CHAT_SPAN


def test_imported_events_are_not_replayable(tmp_path):
    """Imported spans are observational — no replay fixture (research §6)."""
    storage = _fresh_storage(tmp_path)
    import_bundle(storage, _bundle())
    for e in storage.get_events(TRACE_ID):
        assert e.replay is None


def test_trace_search_finds_imported_content(tmp_path):
    storage = _fresh_storage(tmp_path)
    import_bundle(storage, _bundle())
    # FTS indexes input/output/metadata (not the event name), so search for a
    # payload term that survives in the tool's recorded output.
    hits = storage.search_events(TRACE_ID, "timeout")
    assert hits, "FTS should index imported event payloads"
    assert any("timeout" in str(h.output).lower() for h in hits)


def test_bearer_token_in_prompt_is_redacted_at_ingest(tmp_path):
    """Security: a bearer token in a gen_ai.prompt must be scrubbed before
    storage. insert_events does not redact — import_bundle must."""
    storage = _fresh_storage(tmp_path)
    import_bundle(storage, _bundle())
    chat = next(e for e in storage.get_events(TRACE_ID) if e.type == EventType.LLM_RESPONSE.value)
    assert chat.privacy.redacted is True
    assert "sk-ant-test1234567890abcdefghijkl" not in json.dumps(chat.input, default=str)
    # the sk-ant- pattern fires before the bearer pattern, so the token is
    # classified as anthropic_api_key; what matters is that it is gone.
    assert "anthropic_api_key" in chat.privacy.redaction_types
    # also verify it is gone from the stored payload row directly
    row = storage._conn.execute("SELECT payload FROM events WHERE id=?", (CHAT_SPAN,)).fetchone()
    assert "sk-ant-test1234567890abcdefghijkl" not in row["payload"]


def test_import_spans_single_trace_and_json_string(tmp_path):
    storage = _fresh_storage(tmp_path)
    bundle = _bundle()
    spans = bundle["resourceSpans"][0]["scopeSpans"][0]["spans"]
    rid = import_spans(storage, spans)
    assert rid == TRACE_ID
    assert storage.get_run(rid)["status"] == "failed"
    # import_bundle accepts a JSON string too
    storage2 = _fresh_storage(tmp_path.with_name("otel2.db"))
    rids = import_bundle(storage2, json.dumps(_bundle()))
    assert rids == [TRACE_ID]