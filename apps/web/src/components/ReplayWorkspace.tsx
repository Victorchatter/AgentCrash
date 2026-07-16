import { useState } from "react";
import { api } from "../api";
import type { ReplayResponse } from "../types";

/**
 * The signature feature: run a counterfactual replay against a recorded run and
 * compare the behavioral diff side by side. For the demo run, a one-click
 * "disambiguate to correct customer" counterfactual is provided that averts the
 * failure.
 */
export function ReplayWorkspace({ runId }: { runId: string }) {
  const [mode, setMode] = useState("exact");
  const [agent, setAgent] = useState("buggy");
  const [interventionType, setInterventionType] = useState("none");
  const [result, setResult] = useState<ReplayResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function runReplay() {
    setBusy(true);
    setError(null);
    try {
      const interventions: unknown[] = [];
      if (interventionType !== "none") {
        if (interventionType === "disambiguate_correct") {
          // Target the search tool by kind+name; replace with only the correct customer.
          interventions.push({
            type: "replace_tool_response",
            kind: "tool",
            name: "search_customer",
            spec: { response: [{ id: "CUST-002", name: "John Smith", email: "b@x.com", order_id: "ORD-456", order_total: 89.5 }] },
          });
        } else if (interventionType === "inject_timeout_refund") {
          interventions.push({ type: "inject_timeout", kind: "tool", name: "refund_order", spec: { ms: 30000 } });
        } else if (interventionType === "drop_search") {
          interventions.push({ type: "inject_failure", kind: "tool", name: "search_customer", spec: { message: "injected" } });
        }
      }
      const res = await api.replay(runId, { mode, agent, interventions });
      setResult(res);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="replay-workspace">
      <div className="replay-controls">
        <label>
          mode
          <select value={mode} onChange={(e) => setMode(e.target.value)}>
            <option value="exact">exact (all frozen)</option>
            <option value="selective">selective (simulated)</option>
            <option value="live">live (real — consent)</option>
          </select>
        </label>
        <label>
          agent
          <select value={agent} onChange={(e) => setAgent(e.target.value)}>
            <option value="buggy">buggy</option>
            <option value="fixed">fixed</option>
          </select>
        </label>
        <label>
          intervention
          <select value={interventionType} onChange={(e) => setInterventionType(e.target.value)}>
            <option value="none">none</option>
            <option value="disambiguate_correct">replace search → only correct customer</option>
            <option value="inject_timeout_refund">inject timeout on refund_order</option>
            <option value="drop_search">force search_customer to fail</option>
          </select>
        </label>
        <button className="primary" onClick={runReplay} disabled={busy}>
          {busy ? "replaying…" : "run replay"}
        </button>
      </div>

      {error && <div className="error-box">{error}</div>}

      {result && (
        <div className="replay-result">
          <div className={`replay-status ${result.status}`}>
            replay → <strong>{result.status}</strong>
            {result.error && <span className="muted"> · {result.error}</span>}
            <span className="muted"> · new run {result.new_run_id.slice(0, 8)}</span>
          </div>
          <div className="diff-grid">
            <div className="diff-col">
              <h4>Original call sequence</h4>
              <ul>{result.diff.tool_calls_a.map((t, i) => <li key={i}>{t}</li>)}</ul>
            </div>
            <div className="diff-col">
              <h4>Replay call sequence</h4>
              <ul>{result.diff.tool_calls_b.map((t, i) => <li key={i}>{t}</li>)}</ul>
            </div>
          </div>
          <div className="diff-summary">
            {result.diff.added.length > 0 && <div className="diff-added">+ added: {result.diff.added.join(", ")}</div>}
            {result.diff.removed.length > 0 && <div className="diff-removed">− removed: {result.diff.removed.join(", ")}</div>}
            {result.diff.reordered && <div className="diff-changed">~ order changed</div>}
            {result.diff.result_changed && <div className="diff-changed">= result changed</div>}
            {result.diff.regressions.length > 0 && (
              <div className="diff-regression">⚠ regression: {result.diff.regressions.join("; ")}</div>
            )}
            {!result.diff.added.length && !result.diff.removed.length && !result.diff.result_changed && !result.diff.regressions.length && (
              <div className="muted">no behavioral difference — identical behavior</div>
            )}
          </div>
          <details>
            <summary>full diff</summary>
            <pre>{result.diff_lines.join("\n")}</pre>
          </details>
        </div>
      )}
    </div>
  );
}