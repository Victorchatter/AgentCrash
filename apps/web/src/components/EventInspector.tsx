import type { AgentCrashEvent } from "../types";
import { categoryOf, CATEGORY_COLORS, fmtDate, fmtTime, JsonView } from "../lib";

export function EventInspector({ event }: { event: AgentCrashEvent }) {
  const cat = categoryOf(event.type);
  const isError = event.status === "failed" || !!event.error;
  return (
    <div className="event-inspector">
      <div className="inspector-header">
        <span className="event-bar" style={{ background: CATEGORY_COLORS[cat] }} />
        <h3>{event.type}</h3>
        <span className="event-name">{event.name}</span>
        <span className={`status-pill ${event.status}`}>{event.status}</span>
      </div>

      <table className="kv">
        <tbody>
          <tr><td>event id</td><td><code>{event.id}</code></td></tr>
          <tr><td>seq</td><td>{event.seq}</td></tr>
          <tr><td>parent</td><td><code>{event.parent_id || "—"}</code></td></tr>
          <tr><td>timestamp</td><td>{fmtDate(event.timestamp)}</td></tr>
          <tr><td>duration</td><td>{event.duration_ms != null ? fmtTime(event.duration_ms) : "—"}</td></tr>
          <tr><td>actor</td><td>{event.actor ? `${event.actor.type}${event.actor.name ? " · " + event.actor.name : ""}` : "—"}</td></tr>
          <tr><td>source</td><td>{event.source.integration}{event.source.framework ? ` · ${event.source.framework}` : ""}</td></tr>
          <tr>
            <td>replay</td>
            <td>
              {event.replay?.replayable
                ? `${event.replay.frozen ? "frozen ❄ (deterministic)" : "live"}`
                : "not replayable"}
            </td>
          </tr>
          <tr>
            <td>privacy</td>
            <td>{event.privacy.redacted ? `redacted: ${event.privacy.redaction_types.join(", ")}` : "—"}</td>
          </tr>
        </tbody>
      </table>

      {isError && event.error && (
        <div className="error-box">
          <div className="error-box-title">{event.error.type || "Error"}</div>
          <pre>{event.error.message}</pre>
          {event.error.stack && <pre className="stack">{event.error.stack}</pre>}
        </div>
      )}

      <section>
        <h4>Input</h4>
        <JsonView data={event.input} redacted={event.privacy.redacted} />
      </section>
      <section>
        <h4>Output</h4>
        <JsonView data={event.output} redacted={event.privacy.redacted} />
      </section>
      {event.metadata && Object.keys(event.metadata).length > 0 && (
        <section>
          <h4>Metadata</h4>
          <JsonView data={event.metadata} />
        </section>
      )}
    </div>
  );
}