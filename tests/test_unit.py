"""Unit tests: schema, redaction, storage, interventions, behavioral diff."""
from __future__ import annotations

import pytest

from agentcrash.diff import diff_runs
from agentcrash.interventions import (
    LIVE_ONLY,
    FixtureEntry,
    Intervention,
    apply_interventions,
    find_target_key,
)
from agentcrash.redaction import redact_event
from agentcrash.schema import (
    SCHEMA_VERSION,
    AgentCrashEvent,
    EventStatus,
    EventType,
)
from agentcrash.sdk import _fixture_key


# ---------- schema ----------
def _ev(**kw):
    base = dict(id="e1", trace_id="t1", timestamp=1700000000000, type=EventType.TOOL_COMPLETED.value)
    base.update(kw)
    return AgentCrashEvent(**base)


def test_schema_version_is_v1():
    assert SCHEMA_VERSION == "agentcrash.schema.v1"
    assert AgentCrashEvent(id="x", trace_id="t", timestamp=1, type="run.started").schema_version == SCHEMA_VERSION


def test_event_defaults_completed_and_not_error():
    e = _ev()
    assert e.status == EventStatus.COMPLETED.value
    assert e.is_error is False


def test_is_error_for_failed_status_and_for_error_info():
    assert _ev(status="failed").is_error is True
    e = _ev(error={"type": "X", "message": "boom"})
    assert e.is_error is True


def test_replayable_types_are_a_frozen_set():
    from agentcrash.schema import REPLAYABLE_TYPES

    assert EventType.LLM_RESPONSE.value in REPLAYABLE_TYPES
    assert isinstance(REPLAYABLE_TYPES, frozenset)


def test_fixture_key_is_stable_and_signature_sensitive():
    a = _fixture_key("tool", "search", {"name": "John"})
    b = _fixture_key("tool", "search", {"name": "John"})
    c = _fixture_key("tool", "search", {"name": "Jane"})
    assert a == b
    assert a != c
    assert a.startswith("tool:search:")


# ---------- redaction ----------
@pytest.mark.parametrize("secret,tag", [
    ("sk-ant-" + "x" * 24, "anthropic_api_key"),
    ("sk-" + "a" * 24, "openai_api_key"),
    ("AKIA" + "A" * 16, "aws_access_key_id"),
    ("ghp_" + "a" * 36, "github_token"),
    ("xoxb-" + "a" * 12, "slack_token"),
])
def test_redaction_patterns(secret, tag):
    e = _ev(output=f"token={secret}")
    redact_event(e)
    assert secret not in str(e.output)
    assert e.privacy.redacted
    assert tag in e.privacy.redaction_types


def test_bearer_authorization_header_redacted():
    e = _ev(output="Authorization: Bearer abc-def-1234567890")
    redact_event(e)
    assert "Bearer abc-def-1234567890" not in str(e.output)
    assert e.privacy.redacted


def test_env_secret_assignment_redacted():
    e = _ev(output="API_KEY=supersecretvalue123")
    redact_event(e)
    assert "supersecretvalue123" not in str(e.output)
    assert e.privacy.redacted


def test_prose_is_not_redacted_as_high_entropy():
    # A normal sentence with spaces must not trip the entropy sweep.
    e = _ev(output="The quick brown fox jumps over the lazy dog near the riverbank today.")
    redact_event(e)
    assert e.privacy.redacted is False


def test_high_entropy_blob_redacted():
    blob = "Z9fK2qP7mX1nR4vT8wY3bL5cH6dJ0g" + "A1b2C3d4E5f6"
    assert " " not in blob
    e = _ev(output=blob)
    redact_event(e)
    assert blob not in str(e.output)
    assert e.privacy.redacted


def test_redaction_walks_nested_structures():
    e = _ev(output={"headers": {"Authorization": "Bearer secret-token-xyz123"},
                    "items": [{"key": "sk-ant-" + "z" * 24}]})
    redact_event(e)
    blob = str(e.output)
    assert "sk-ant-" + "z" * 24 not in blob
    assert "secret-token-xyz123" not in blob
    assert e.privacy.redacted


def test_redaction_scrubs_error_stack():
    e = _ev(status="failed",
            error={"type": "RuntimeError", "message": "failed with key sk-" + "b" * 24,
                   "stack": "trace... sk-" + "b" * 24})
    redact_event(e)
    assert "sk-" + "b" * 24 not in (e.error.message or "")
    assert "sk-" + "b" * 24 not in (e.error.stack or "")
    assert e.privacy.redacted


def test_unredacted_event_leaves_privacy_clean():
    e = _ev(output={"ok": "normal value"})
    redact_event(e)
    assert e.privacy.redacted is False
    assert e.privacy.redaction_types == []


# ---------- storage ----------
def test_storage_run_and_events_roundtrip(tmp_storage, demo):
    from agentcrash.sdk import CrashTracer

    tracer = CrashTracer(tmp_storage, integration="test", framework="t")
    with tracer.run("support", model="stub", project="demo") as run:
        run.tool("search_customer", {"name": "John Smith"}, lambda: demo.search_customer("John Smith"))
    events = tmp_storage.get_events(run.run_id)
    assert len(events) >= 3  # run.started + tool.called + tool.completed
    got = tmp_storage.get_run(run.run_id)
    assert got["status"] == "completed"
    assert got["tool_calls"] == 1


def test_storage_fts_search(tmp_storage, demo):
    from agentcrash.sdk import CrashTracer

    tracer = CrashTracer(tmp_storage, integration="test", framework="t")
    try:
        with tracer.run("support", model="stub", project="demo",
                        metadata={"request": demo.DEMO_REQUEST}) as run:
            demo.buggy_agent(demo.DEMO_REQUEST, run)
    except Exception:
        pass  # the buggy agent is expected to raise WrongCustomerError
    # FTS indexes output text; "CUST-001" appears in the search results.
    hits = tmp_storage.search_events(run.run_id, "CUST-001")
    assert hits, "FTS should find the search_customer result containing CUST-001"


def test_storage_migrates_stale_fts_without_event_id(tmp_storage, demo):
    """An older DB whose events_fts predates the event_id column must be
    migrated on open (FTS5 can't be ALTERed) and search must still work."""
    from agentcrash.sdk import CrashTracer

    tracer = CrashTracer(tmp_storage, integration="test", framework="t")
    with tracer.run("support", model="stub", project="demo") as run:
        run.tool("search_customer", {"name": "John Smith"}, lambda: demo.search_customer("John Smith"))
    rid = run.run_id
    tmp_storage.close()

    # Downgrade FTS to the pre-event_id shape, simulating a stale older DB.
    import sqlite3

    conn = sqlite3.connect(str(tmp_storage.path))
    conn.execute("DROP TABLE events_fts")
    conn.execute("CREATE VIRTUAL TABLE events_fts USING fts5(run_id UNINDEXED, content)")
    conn.execute("INSERT INTO events_fts(rowid, run_id, content) VALUES (NULL, ?, 'CUST-001')", (rid,))
    conn.commit()
    conn.close()

    # Reopen -> migration should recreate event_id and rebuild the index.
    from agentcrash.storage import Storage

    reopened = Storage(str(tmp_storage.path))
    try:
        cols = {r[1] for r in reopened._conn.execute("PRAGMA table_info(events_fts)").fetchall()}
        assert "event_id" in cols
        assert reopened.search_events(rid, "CUST-001"), "search must work after FTS migration"
    finally:
        reopened.close()


def test_storage_export_import_roundtrip(tmp_storage, demo):
    from agentcrash.sdk import CrashTracer

    tracer = CrashTracer(tmp_storage, integration="test", framework="t")
    try:
        with tracer.run("support", model="stub", project="demo") as run:
            demo.buggy_agent(demo.DEMO_REQUEST, run)
    except Exception:
        pass
    rid = run.run_id
    bundle = tmp_storage.export_run(rid)
    assert bundle["run"]["id"] == rid
    assert len(bundle["events"]) == len(tmp_storage.get_events(rid))
    imported = tmp_storage.import_run(bundle)
    assert imported == rid
    assert len(tmp_storage.get_events(rid)) == len(bundle["events"])


def test_artifact_stored_and_retrievable(tmp_storage):
    from agentcrash.sdk import CrashTracer

    tracer = CrashTracer(tmp_storage, integration="test", framework="t")
    with tracer.run("a", project="p") as run:
        run.event(EventType.LLM_RESPONSE.value, name="llm", output="ok")
    aid = tmp_storage.store_artifact(run.run_id, None, "llm.response.body", b"payload-bytes")
    path = tmp_storage.get_artifact_path(aid)
    assert path is not None and path.read_bytes() == b"payload-bytes"


# ---------- interventions ----------
def _entry(kind="tool", name="search_customer", output=None, status="completed"):
    return FixtureEntry(fixture_key=_fixture_key(kind, name, {"name": "John"}),
                        kind=kind, name=name, type=EventType.TOOL_COMPLETED.value,
                        output=output, status=status)


def test_replace_tool_response_intervention():
    e = _entry(output=[{"id": "OLD"}])
    iv = Intervention(id="iv", type="replace_tool_response", fixture_key=e.fixture_key,
                      spec={"response": [{"id": "NEW"}]})
    out = apply_interventions({e.fixture_key: e}, [iv])
    assert out[e.fixture_key].output == [{"id": "NEW"}]
    assert out[e.fixture_key].status == "completed"


def test_inject_failure_intervention():
    e = _entry(output=[{"id": "X"}])
    iv = Intervention(id="iv", type="inject_failure", fixture_key=e.fixture_key,
                      spec={"message": "boom", "error_type": "Fault"})
    out = apply_interventions({e.fixture_key: e}, [iv])
    assert out[e.fixture_key].status == "failed"
    assert out[e.fixture_key].error["type"] == "Fault"
    assert out[e.fixture_key].output is None


def test_inject_timeout_intervention():
    e = _entry()
    iv = Intervention(id="iv", type="inject_timeout", fixture_key=e.fixture_key, spec={"ms": 5000})
    out = apply_interventions({e.fixture_key: e}, [iv])
    assert out[e.fixture_key].status == "failed"
    assert "5000ms" in out[e.fixture_key].error["message"]


def test_live_only_interventions_skipped_in_apply():
    e = _entry(output=[{"id": "X"}])
    iv = Intervention(id="iv", type="replace_model", fixture_key=e.fixture_key, spec={})
    assert iv.is_live_only() is True
    out = apply_interventions({e.fixture_key: e}, [iv])
    # output untouched: apply_interventions must not edit live-only interventions
    assert out[e.fixture_key].output == [{"id": "X"}]


def test_intervention_matches_by_kind_and_name():
    e = _entry(kind="tool", name="refund_order")
    iv = Intervention(id="iv", type="inject_failure", kind="tool", name="refund_order", spec={})
    assert iv.matches(e) is True
    other = _entry(kind="tool", name="search_customer")
    assert iv.matches(other) is False
    assert find_target_key({e.fixture_key: e, other.fixture_key: other}, iv) == e.fixture_key


def test_unknown_intervention_type_raises():
    e = _entry()
    iv = Intervention(id="iv", type="nope", fixture_key=e.fixture_key, spec={})
    with pytest.raises(ValueError):
        apply_interventions({e.fixture_key: e}, [iv])


def test_live_only_set_contains_replace_model_and_modify_prompt():
    assert "replace_model" in LIVE_ONLY
    assert "modify_prompt" in LIVE_ONLY


# ---------- diff ----------
def _mk(types_statuses):
    evs = []
    for i, (t, name, status) in enumerate(types_statuses, 1):
        evs.append(AgentCrashEvent(id=f"e{i}", trace_id="t", seq=i, timestamp=i,
                                   type=t, name=name, status=status,
                                   input={} if t == EventType.TOOL_CALLED.value else None))
    return evs


def test_diff_detects_added_call():
    a = _mk([(EventType.TOOL_CALLED.value, "search", "started")])
    b = _mk([(EventType.TOOL_CALLED.value, "search", "started"),
             (EventType.TOOL_CALLED.value, "refund", "started")])
    d = diff_runs(a, b)
    assert "refund" in d.added
    assert d.is_different is True


def test_diff_detects_reorder():
    a = _mk([(EventType.TOOL_CALLED.value, "search", "started"),
             (EventType.TOOL_CALLED.value, "refund", "started")])
    b = _mk([(EventType.TOOL_CALLED.value, "refund", "started"),
             (EventType.TOOL_CALLED.value, "search", "started")])
    d = diff_runs(a, b)
    assert d.reordered is True


def test_diff_detects_arg_change():
    a = [AgentCrashEvent(id="e1", trace_id="t", seq=1, timestamp=1,
                         type=EventType.TOOL_CALLED.value, name="search", status="started",
                         input={"name": "John"})]
    b = [AgentCrashEvent(id="e1", trace_id="t", seq=1, timestamp=1,
                         type=EventType.TOOL_CALLED.value, name="search", status="started",
                         input={"name": "Jane"})]
    d = diff_runs(a, b)
    assert d.arg_changes


def test_diff_identical_failures_not_flagged_different():
    # Same failing tool sequence, different exception wrapper text only.
    a = [AgentCrashEvent(id="e1", trace_id="t", seq=1, timestamp=1,
                         type=EventType.TOOL_CALLED.value, name="refund", status="started"),
         AgentCrashEvent(id="e2", trace_id="t", seq=2, timestamp=2,
                         type=EventType.RUN_FAILED.value, status="failed",
                         error={"type": "A", "message": "wrapper A"})]
    b = [AgentCrashEvent(id="e1", trace_id="t", seq=1, timestamp=1,
                         type=EventType.TOOL_CALLED.value, name="refund", status="started"),
         AgentCrashEvent(id="e2", trace_id="t", seq=2, timestamp=2,
                         type=EventType.RUN_FAILED.value, status="failed",
                         error={"type": "B", "message": "wrapper B"})]
    d = diff_runs(a, b)
    assert d.is_different is False


def test_diff_result_changed_failed_to_completed():
    a = [AgentCrashEvent(id="e1", trace_id="t", seq=1, timestamp=1,
                         type=EventType.RUN_FAILED.value, status="failed")]
    b = [AgentCrashEvent(id="e1", trace_id="t", seq=1, timestamp=1,
                         type=EventType.RUN_COMPLETED.value, status="completed")]
    d = diff_runs(a, b)
    assert d.result_changed is True
    assert d.is_different is True


def test_diff_flags_new_side_effect_as_regression():
    a = _mk([(EventType.TOOL_CALLED.value, "search", "started")])
    b = _mk([(EventType.TOOL_CALLED.value, "search", "started"),
             (EventType.TOOL_CALLED.value, "refund_order", "started")])
    d = diff_runs(a, b)
    assert any("refund_order" in r for r in d.regressions)