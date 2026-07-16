import { useEffect, useState } from "react";
import { api } from "./api";
import type { AgentCrashEvent, AnalysisResponse, Run } from "./types";
import { RunList } from "./components/RunList";
import { TraceView } from "./components/TraceView";
import { FailureReport } from "./components/FailureReport";
import { ReplayWorkspace } from "./components/ReplayWorkspace";
import { fmtDate } from "./lib";

type View = "trace" | "report" | "replay" | "tests";

export function App() {
  const [runs, setRuns] = useState<Run[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [events, setEvents] = useState<AgentCrashEvent[]>([]);
  const [selectedEventId, setSelectedEventId] = useState<string | null>(null);
  const [view, setView] = useState<View>("trace");
  const [analysis, setAnalysis] = useState<AnalysisResponse | null>(null);
  const [tests, setTests] = useState<{ id: string; name: string; spec: { description: string }; last_result: unknown }[]>([]);
  const [filter, setFilter] = useState("");
  const [query, setQuery] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loadingEvents, setLoadingEvents] = useState(false);

  useEffect(() => {
    api.runs().then(setRuns).catch((e) => setError(String(e)));
  }, []);

  useEffect(() => {
    if (!selectedId) return;
    setLoadingEvents(true);
    setSelectedEventId(null);
    setAnalysis(null);
    api
      .events(selectedId)
      .then((ev) => setEvents(ev))
      .catch((e) => setError(String(e)))
      .finally(() => setLoadingEvents(false));
  }, [selectedId]);

  useEffect(() => {
    if (view === "report" && selectedId && !analysis) {
      api.analyze(selectedId).then(setAnalysis).catch((e) => setError(String(e)));
    }
    if (view === "tests") {
      api.tests().then(setTests).catch((e) => setError(String(e)));
    }
  }, [view, selectedId, analysis]);

  const selectedRun = runs.find((r) => r.id === selectedId) || null;

  function jumpToEvent(eventId: string) {
    setView("trace");
    setSelectedEventId(eventId);
  }

  async function generateTest() {
    if (!selectedId) return;
    await api.generateTest(selectedId);
    const t = await api.tests();
    setTests(t as typeof tests);
    setView("tests");
  }

  async function runTestAndShow(testId: string, agent: string) {
    const r = await api.runTest(testId, agent);
    const msg = `${agent}: passed=${r.passed}\nstatus=${r.status}\n${r.violations.length ? r.violations.join("\n") : "(no violations)"}`;
    alert(msg);
  }

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="logo">◈</span> AgentCrash
          <span className="brand-sub">agent debug + reliability lab</span>
        </div>
        {selectedRun && (
          <div className="topbar-run">
            <span className={`status-dot ${selectedRun.status}`} />
            <code>{selectedRun.id.slice(0, 12)}</code>
            <span className="muted">{selectedRun.agent}</span>
            <span className="muted">{fmtDate(selectedRun.started_at)}</span>
          </div>
        )}
      </header>

      <div className="layout">
        <aside className="sidebar">
          <div className="sidebar-head">
            <h3>Runs</h3>
            <span className="muted">{runs.length}</span>
          </div>
          <RunList runs={runs} selectedId={selectedId} onSelect={setSelectedId} />
        </aside>

        <main className="main">
          {!selectedId && <div className="empty big">Select a run to begin. Run <code>agentcrash demo</code> to record a failure.</div>}
          {selectedId && (
            <>
              <nav className="tabs">
                <button className={view === "trace" ? "active" : ""} onClick={() => setView("trace")}>Trace</button>
                <button className={view === "report" ? "active" : ""} onClick={() => setView("report")}>Failure report</button>
                <button className={view === "replay" ? "active" : ""} onClick={() => setView("replay")}>Replay workspace</button>
                <button className={view === "tests" ? "active" : ""} onClick={() => setView("tests")}>Regression tests</button>
              </nav>

              {error && <div className="error-box">{error}</div>}

              {view === "trace" && (
                loadingEvents ? <div className="empty">loading…</div> : (
                  <TraceView
                    events={events}
                    selectedId={selectedEventId}
                    onSelect={setSelectedEventId}
                    filter={filter}
                    onFilter={setFilter}
                    query={query}
                    onQuery={setQuery}
                  />
                )
              )}

              {view === "report" && (
                analysis ? (
                  <>
                    <FailureReport analysis={analysis} onJumpTo={jumpToEvent} />
                    <div className="report-actions">
                      <button className="primary" onClick={generateTest}>generate regression test</button>
                    </div>
                  </>
                ) : <div className="empty">analyzing…</div>
              )}

              {view === "replay" && <ReplayWorkspace runId={selectedId} />}

              {view === "tests" && (
                <div className="tests-view">
                  <p className="muted">
                    Behavioral regression tests assert agent invariants over the trace. A test generated from a
                    failure should fail against the buggy agent and pass against the fixed one.
                  </p>
                  <ul className="test-list">
                    {tests.map((t) => (
                      <li key={t.id} className="test-item">
                        <div className="test-head">
                          <strong>{t.name}</strong>
                          <code>{t.id.slice(0, 8)}</code>
                        </div>
                        <div className="test-desc">{t.spec.description}</div>
                        <div className="test-actions">
                          <button onClick={() => runTestAndShow(t.id, "buggy")}>run · buggy</button>
                          <button onClick={() => runTestAndShow(t.id, "fixed")}>run · fixed</button>
                        </div>
                      </li>
                    ))}
                    {tests.length === 0 && <li className="muted">No tests yet. Generate one from a failure report.</li>}
                  </ul>
                </div>
              )}
            </>
          )}
        </main>
      </div>
    </div>
  );
}