import type { AnalysisResponse } from "../types";

export function FailureReport({
  analysis,
  onJumpTo,
}: {
  analysis: AnalysisResponse;
  onJumpTo: (eventId: string) => void;
}) {
  const pct = Math.round(analysis.confidence * 100);
  return (
    <div className="failure-report">
      <div className="report-header">
        <h3>Root Cause Analysis</h3>
        <div className="confidence">
          <div className="confidence-bar">
            <div className="confidence-fill" style={{ width: `${pct}%` }} />
          </div>
          <span>{pct}% confidence</span>
        </div>
      </div>

      <div className="report-cause">{analysis.root_cause}</div>

      {analysis.recommended_fix && (
        <div className="report-fix">
          <strong>Recommended fix:</strong> {analysis.recommended_fix}
        </div>
      )}

      {analysis.suggested_invariant && (
        <div className="report-invariant">
          <strong>Suggested invariant:</strong>
          <pre>{JSON.stringify(analysis.suggested_invariant, null, 2)}</pre>
        </div>
      )}

      <h4>Evidence</h4>
      <div className="evidence-list">
        {analysis.candidates
          .filter((c) => c.averted)
          .flatMap((c) =>
            c.evidence.map((e, i) => (
              <div key={`${c.event_id}-${i}`} className={`evidence ${e.averted ? "averts" : "reproduces"}`}>
                <span className="evidence-mark">{e.averted ? "✅ averts" : "❌ reproduces"}</span>
                <span className="evidence-desc">{e.description}</span>
                {e.event_id && (
                  <button className="link" onClick={() => onJumpTo(e.event_id!)}>jump to event</button>
                )}
              </div>
            )),
          )}
        {analysis.candidates.filter((c) => c.averted).length === 0 && (
          <div className="muted">No decisive counterfactual found.</div>
        )}
      </div>

      <details className="report-raw">
        <summary>raw analysis</summary>
        <pre>{analysis.summary.join("\n")}</pre>
      </details>
    </div>
  );
}