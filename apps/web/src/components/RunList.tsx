import type { Run } from "../types";
import { fmtDate, fmtTime } from "../lib";

export function RunList({
  runs,
  selectedId,
  onSelect,
}: {
  runs: Run[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  return (
    <ul className="run-list">
      {runs.map((r) => (
        <li
          key={r.id}
          className={`run-item ${r.status} ${selectedId === r.id ? "selected" : ""}`}
          onClick={() => onSelect(r.id)}
        >
          <div className="run-item-head">
            <span className={`status-dot ${r.status}`} title={r.status} />
            <span className="run-id">{r.id.slice(0, 8)}</span>
            <span className="run-agent">{r.agent || "—"}</span>
          </div>
          <div className="run-item-meta">
            <span>{r.tool_calls} calls</span>
            <span>·</span>
            <span>{r.retries} retries</span>
            {r.duration_ms != null && (
              <>
                <span>·</span>
                <span>{fmtTime(r.duration_ms)}</span>
              </>
            )}
            {!!r.metadata?.replay_of && <span className="tag">replay</span>}
          </div>
          <div className="run-item-time">{fmtDate(r.started_at)}</div>
          {r.error && <div className="run-item-error">{r.error.slice(0, 70)}</div>}
        </li>
      ))}
    </ul>
  );
}