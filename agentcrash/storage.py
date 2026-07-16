"""Local-first SQLite storage for AgentCrash.

Single-file database (WAL mode) + an on-disk artifact directory for large or
binary payloads. No server, no network. Designed to scale to tens of thousands
of events per run with cheap filtering and full-text search.

Schema is additive and versioned via the ``schema_meta`` table.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from agentcrash.schema import AgentCrashEvent

SCHEMA_VERSION_DB = 1


def _now_ms() -> int:
    return int(time.time() * 1000)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    status TEXT NOT NULL,
    agent TEXT,
    model TEXT,
    started_at INTEGER NOT NULL,
    ended_at INTEGER,
    duration_ms INTEGER,
    tool_calls INTEGER DEFAULT 0,
    retries INTEGER DEFAULT 0,
    cost_usd REAL,
    error TEXT,
    root_cause TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (project_id) REFERENCES projects(id)
);
CREATE INDEX IF NOT EXISTS idx_runs_project ON runs(project_id, started_at DESC);
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    parent_id TEXT,
    seq INTEGER NOT NULL,
    timestamp INTEGER NOT NULL,
    type TEXT NOT NULL,
    actor_type TEXT,
    actor_name TEXT,
    name TEXT,
    status TEXT NOT NULL,
    duration_ms INTEGER,
    source_integration TEXT,
    payload TEXT NOT NULL,
    error TEXT,
    privacy TEXT NOT NULL DEFAULT '{"redacted": false}',
    FOREIGN KEY (run_id) REFERENCES runs(id)
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(run_id, type);
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    event_id TEXT,
    kind TEXT NOT NULL,
    mime TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT,
    path TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(id)
);
CREATE TABLE IF NOT EXISTS replays (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    config TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at INTEGER NOT NULL,
    finished_at INTEGER,
    outcome TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(id)
);
CREATE TABLE IF NOT EXISTS interventions (
    id TEXT PRIMARY KEY,
    replay_id TEXT NOT NULL,
    type TEXT NOT NULL,
    target_event_id TEXT,
    spec TEXT NOT NULL,
    FOREIGN KEY (replay_id) REFERENCES replays(id)
);
CREATE TABLE IF NOT EXISTS tests (
    id TEXT PRIMARY KEY,
    run_id TEXT,
    name TEXT NOT NULL,
    spec TEXT NOT NULL,
    last_result TEXT,
    last_run_at INTEGER,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS chaos_runs (
    id TEXT PRIMARY KEY,
    run_id TEXT,
    spec TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at INTEGER NOT NULL,
    finished_at INTEGER,
    result TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
    run_id UNINDEXED, event_id UNINDEXED, content
);
"""


class Storage:
    """SQLite-backed store. Thread-safe-ish: one connection per instance,
    check_same_thread=False so the FastAPI server can share it. For the MVP we
    serialize writes with a global lock; per-run locks if throughput matters."""

    def __init__(self, db_path: str | os.PathLike[str], artifacts_dir: str | os.PathLike[str] | None = None):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir = Path(artifacts_dir) if artifacts_dir else self.path.parent / "artifacts"
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._migrate_fts()
        self._conn.execute(
            "INSERT OR IGNORE INTO schema_meta(key, value) VALUES (?, ?)",
            ("schema_version", str(SCHEMA_VERSION_DB)),
        )
        self._conn.commit()

    def _migrate_fts(self) -> None:
        """Reconcile additive schema changes that CREATE IF NOT EXISTS can't.

        FTS5 tables cannot be ALTERed, so if an existing events_fts predates the
        event_id column (older DBs created before the search-by-id query), drop
        and rebuild it from the events table. No-op on fresh/current DBs."""
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(events_fts)").fetchall()}
        if not cols or "event_id" in cols:
            return
        self._conn.execute("DROP TABLE events_fts")
        self._conn.execute(
            "CREATE VIRTUAL TABLE events_fts USING fts5(run_id UNINDEXED, event_id UNINDEXED, content)"
        )
        rows = self._conn.execute("SELECT run_id, id, payload FROM events").fetchall()
        fts = [(r["run_id"], r["id"], _fts_text(json.loads(r["payload"]))) for r in rows]
        if fts:
            self._conn.executemany(
                "INSERT INTO events_fts(rowid, run_id, event_id, content) VALUES (NULL, ?, ?, ?)",
                fts,
            )
        self._conn.commit()

    # ----- projects -----
    def create_project(self, name: str, metadata: dict[str, Any] | None = None) -> str:
        import uuid

        pid = uuid.uuid4().hex
        self._conn.execute(
            "INSERT INTO projects(id, name, created_at, metadata) VALUES (?,?,?,?)",
            (pid, name, _now_ms(), json.dumps(metadata or {})),
        )
        self._conn.commit()
        return pid

    def list_projects(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

    def get_or_create_default_project(self, name: str = "default") -> str:
        row = self._conn.execute("SELECT id FROM projects WHERE name=? LIMIT 1", (name,)).fetchone()
        if row:
            return row["id"]
        return self.create_project(name)

    # ----- runs -----
    def create_run(self, project_id: str, *, agent: str | None = None, model: str | None = None,
                   metadata: dict[str, Any] | None = None, run_id: str | None = None) -> str:
        import uuid

        rid = run_id or uuid.uuid4().hex
        self._conn.execute(
            "INSERT INTO runs(id, project_id, status, agent, model, started_at, metadata) "
            "VALUES (?,?,?,?,?,?,?)",
            (rid, project_id, "started", agent, model, _now_ms(), json.dumps(metadata or {})),
        )
        self._conn.commit()
        return rid

    def finish_run(self, run_id: str, status: str, *, error: str | None = None,
                   root_cause: str | None = None, duration_ms: int | None = None,
                   tool_calls: int = 0, retries: int = 0, cost_usd: float | None = None) -> None:
        self._conn.execute(
            "UPDATE runs SET status=?, ended_at=?, duration_ms=?, tool_calls=?, retries=?, "
            "cost_usd=?, error=?, root_cause=? WHERE id=?",
            (status, _now_ms(), duration_ms, tool_calls, retries, cost_usd, error, root_cause, run_id),
        )
        self._conn.commit()

    def set_run_root_cause(self, run_id: str, root_cause: str | None) -> None:
        self._conn.execute("UPDATE runs SET root_cause=? WHERE id=?", (root_cause, run_id))
        self._conn.commit()

    def list_runs(self, project_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if project_id:
            rows = self._conn.execute(
                "SELECT * FROM runs WHERE project_id=? ORDER BY started_at DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["metadata"] = json.loads(d.get("metadata") or "{}")
            out.append(d)
        return out

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        r = self._conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["metadata"] = json.loads(d.get("metadata") or "{}")
        return d

    # ----- events -----
    def insert_events(self, run_id: str, events: Iterable[AgentCrashEvent]) -> int:
        rows = []
        fts_rows = []
        for e in events:
            payload = {
                "input": e.input,
                "output": e.output,
                "metadata": e.metadata,
                "replay": e.replay.model_dump() if e.replay else None,
                "artifacts": [a.model_dump() for a in e.artifacts],
                "source": e.source.model_dump(),
                "name": e.name,
            }
            rows.append((
                e.id, run_id, e.trace_id, e.parent_id, e.seq, e.timestamp, e.type,
                e.actor.type if e.actor else None, e.actor.name if e.actor else None,
                e.name, e.status, e.duration_ms, e.source.integration,
                json.dumps(payload, default=str),
                json.dumps(e.error.model_dump()) if e.error else None,
                json.dumps(e.privacy.model_dump()),
            ))
            fts_rows.append((run_id, e.id, _fts_text(payload)))
        self._conn.executemany(
            "INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        self._conn.executemany(
            "INSERT INTO events_fts(rowid, run_id, event_id, content) VALUES (NULL, ?, ?, ?)",
            fts_rows,
        )
        self._conn.commit()
        return len(rows)

    def get_events(self, run_id: str) -> list[AgentCrashEvent]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE run_id=? ORDER BY seq ASC", (run_id,)
        ).fetchall()
        return [self._row_to_event(r) for r in rows]

    def get_event(self, run_id: str, event_id: str) -> AgentCrashEvent | None:
        r = self._conn.execute(
            "SELECT * FROM events WHERE run_id=? AND id=?", (run_id, event_id)
        ).fetchone()
        return self._row_to_event(r) if r else None

    def search_events(self, run_id: str, query: str, limit: int = 50) -> list[AgentCrashEvent]:
        # Wrap as an FTS5 phrase literal so hyphens/colons/punctuation in the
        # query (e.g. IDs like "CUST-001") are matched literally instead of being
        # parsed as column filters or operators (which raise "no such column").
        phrase = '"' + query.replace('"', '""') + '"'
        rows = self._conn.execute(
            "SELECT e.* FROM events_fts f JOIN events e ON f.event_id = e.id "
            "WHERE f.run_id=? AND f.content MATCH ? LIMIT ?",
            (run_id, phrase, limit),
        ).fetchall()
        return [self._row_to_event(r) for r in rows]

    def _row_to_event(self, r: sqlite3.Row) -> AgentCrashEvent:
        payload = json.loads(r["payload"])
        from agentcrash.schema import (
            Actor,
            ActorType,
            Artifact,
            ErrorInfo,
            Privacy,
            ReplayMeta,
            Source,
        )

        actor = None
        if r["actor_type"]:
            actor = Actor(type=ActorType(r["actor_type"]), name=r["actor_name"])
        return AgentCrashEvent(
            id=r["id"],
            trace_id=r["trace_id"],
            parent_id=r["parent_id"],
            seq=r["seq"],
            timestamp=r["timestamp"],
            type=r["type"],
            name=r["name"],
            source=Source(**payload.get("source", {})),
            actor=actor,
            input=payload.get("input"),
            output=payload.get("output"),
            metadata=payload.get("metadata", {}) or {},
            status=r["status"],
            duration_ms=r["duration_ms"],
            error=ErrorInfo(**json.loads(r["error"])) if r["error"] else None,
            privacy=Privacy(**json.loads(r["privacy"])),
            replay=ReplayMeta(**payload["replay"]) if payload.get("replay") else None,
            artifacts=[Artifact(**a) for a in payload.get("artifacts", [])],
        )

    # ----- artifacts -----
    def store_artifact(self, run_id: str, event_id: str | None, kind: str, data: bytes,
                       mime: str = "application/json") -> str:
        import uuid

        aid = uuid.uuid4().hex
        sha = hashlib.sha256(data).hexdigest()
        run_dir = self.artifacts_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / f"{aid}.bin"
        path.write_bytes(data)
        self._conn.execute(
            "INSERT INTO artifacts(id, run_id, event_id, kind, mime, size, sha256, path, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (aid, run_id, event_id, kind, mime, len(data), sha, str(path), _now_ms()),
        )
        self._conn.commit()
        return aid

    def get_artifact_path(self, artifact_id: str) -> Path | None:
        r = self._conn.execute("SELECT path FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
        return Path(r["path"]) if r else None

    # ----- replays / interventions -----
    def create_replay(self, run_id: str, mode: str, config: dict[str, Any]) -> str:
        import uuid

        rid = uuid.uuid4().hex
        self._conn.execute(
            "INSERT INTO replays(id, run_id, mode, config, status, started_at) VALUES (?,?,?,?,?,?)",
            (rid, run_id, mode, json.dumps(config), "running", _now_ms()),
        )
        self._conn.commit()
        return rid

    def finish_replay(self, replay_id: str, status: str, outcome: dict[str, Any]) -> None:
        self._conn.execute(
            "UPDATE replays SET status=?, finished_at=?, outcome=? WHERE id=?",
            (status, _now_ms(), json.dumps(outcome, default=str), replay_id),
        )
        self._conn.commit()

    def list_replays(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM replays WHERE run_id=? ORDER BY started_at DESC", (run_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["config"] = json.loads(d["config"])
            d["outcome"] = json.loads(d["outcome"]) if d["outcome"] else None
            out.append(d)
        return out

    def add_intervention(self, replay_id: str, type_: str, target_event_id: str | None, spec: dict[str, Any]) -> str:
        import uuid

        iid = uuid.uuid4().hex
        self._conn.execute(
            "INSERT INTO interventions(id, replay_id, type, target_event_id, spec) VALUES (?,?,?,?,?)",
            (iid, replay_id, type_, target_event_id, json.dumps(spec, default=str)),
        )
        self._conn.commit()
        return iid

    # ----- tests -----
    def save_test(self, name: str, spec: dict[str, Any], run_id: str | None = None, test_id: str | None = None) -> str:
        import uuid

        tid = test_id or uuid.uuid4().hex
        self._conn.execute(
            "INSERT OR REPLACE INTO tests(id, run_id, name, spec, created_at) VALUES (?,?,?,?,?)",
            (tid, run_id, name, json.dumps(spec, default=str), _now_ms()),
        )
        self._conn.commit()
        return tid

    def list_tests(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM tests ORDER BY created_at DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["spec"] = json.loads(d["spec"])
            d["last_result"] = json.loads(d["last_result"]) if d["last_result"] else None
            out.append(d)
        return out

    def get_test(self, test_id: str) -> dict[str, Any] | None:
        r = self._conn.execute("SELECT * FROM tests WHERE id=?", (test_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["spec"] = json.loads(d["spec"])
        return d

    def record_test_result(self, test_id: str, result: dict[str, Any]) -> None:
        self._conn.execute(
            "UPDATE tests SET last_result=?, last_run_at=? WHERE id=?",
            (json.dumps(result, default=str), _now_ms(), test_id),
        )
        self._conn.commit()

    # ----- export / import (portable, single-file trace bundle) -----
    def export_run(self, run_id: str) -> dict[str, Any]:
        run = self.get_run(run_id)
        if not run:
            raise KeyError(f"run {run_id} not found")
        events = [e.model_dump() for e in self.get_events(run_id)]
        return {"schema_version": SCHEMA_VERSION_DB, "run": run, "events": events}

    def import_run(self, bundle: dict[str, Any], project_id: str | None = None) -> str:
        run = dict(bundle["run"])
        run["metadata"] = json.dumps(run.get("metadata") or {})
        if project_id:
            run["project_id"] = project_id
        # ensure project exists
        pid = run["project_id"]
        self._conn.execute(
            "INSERT OR IGNORE INTO projects(id, name, created_at, metadata) VALUES (?,?,?,?)",
            (pid, run.get("agent") or "imported", run.get("started_at") or _now_ms(), "{}"),
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO runs(id, project_id, status, agent, model, started_at, ended_at, "
            "duration_ms, tool_calls, retries, cost_usd, error, root_cause, metadata) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run["id"], pid, run.get("status", "completed"), run.get("agent"), run.get("model"),
                run.get("started_at") or _now_ms(), run.get("ended_at"), run.get("duration_ms"),
                run.get("tool_calls", 0), run.get("retries", 0), run.get("cost_usd"),
                run.get("error"), run.get("root_cause"), run["metadata"],
            ),
        )
        events = [AgentCrashEvent(**e) for e in bundle["events"]]
        self.insert_events(run["id"], events)
        return run["id"]

    def close(self) -> None:
        self._conn.close()


def _fts_text(payload: dict[str, Any]) -> str:
    """Flatten payload into searchable text, dropping giant blobs."""
    parts: list[str] = []
    for key in ("input", "output", "metadata"):
        val = payload.get(key)
        if val is None:
            continue
        s = json.dumps(val, default=str)
        if len(s) > 4000:
            s = s[:4000]
        parts.append(s)
    return " ".join(parts)