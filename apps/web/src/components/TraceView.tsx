import { useMemo } from "react";
import type { AgentCrashEvent } from "../types";
import { buildTree, categoryOf, CATEGORY_COLORS, fmtTime, JsonView, statusColor, TreeNode } from "../lib";
import { EventInspector } from "./EventInspector";

function EventRow({
  node,
  depth,
  selectedId,
  onSelect,
  minTs,
  maxTs,
}: {
  node: TreeNode;
  depth: number;
  selectedId: string | null;
  onSelect: (id: string) => void;
  minTs: number;
  maxTs: number;
}) {
  const cat = categoryOf(node.type);
  const color = CATEGORY_COLORS[cat];
  const span = maxTs - minTs || 1;
  const left = ((node.timestamp - minTs) / span) * 100;
  const width = node.duration_ms ? Math.max(2, (node.duration_ms / span) * 100 * 1000) : 4;
  return (
    <>
      <div
        className={`event-row ${selectedId === node.id ? "selected" : ""} ${node.status}`}
        style={{ paddingLeft: 8 + depth * 16 }}
        onClick={() => onSelect(node.id)}
      >
        <span className="event-bar" style={{ background: color }} />
        <span className="event-seq">#{node.seq}</span>
        <span className="event-type" style={{ color }}>{node.type}</span>
        <span className="event-name">{node.name || ""}</span>
        <span className="event-status" style={{ color: statusColor(node.status) }}>{node.status}</span>
        {node.duration_ms != null && <span className="event-dur">{fmtTime(node.duration_ms)}</span>}
        {node.replay?.frozen && <span className="tag frozen" title="frozen — replayable">❄</span>}
        {node.privacy.redacted && <span className="tag" title="redacted">REDACTED</span>}
        {(node.status === "failed" || !!node.error) && <span className="tag error-tag">ERR</span>}
      </div>
      <div className="waterfall">
        <div
          className="waterfall-bar"
          style={{ background: color, left: `${left}%`, width: `${Math.min(width, 100 - left)}%` }}
        />
      </div>
      {node.children.map((c) => (
        <EventRow
          key={c.id}
          node={c}
          depth={depth + 1}
          selectedId={selectedId}
          onSelect={onSelect}
          minTs={minTs}
          maxTs={maxTs}
        />
      ))}
    </>
  );
}

export function TraceView({
  events,
  selectedId,
  onSelect,
  filter,
  onFilter,
  query,
  onQuery,
}: {
  events: AgentCrashEvent[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  filter: string;
  onFilter: (f: string) => void;
  query: string;
  onQuery: (q: string) => void;
}) {
  const tree = useMemo(() => {
    let filtered = events;
    if (filter) filtered = filtered.filter((e) => categoryOf(e.type) === filter);
    if (query)
      filtered = filtered.filter((e) =>
        JSON.stringify(e).toLowerCase().includes(query.toLowerCase()),
      );
    return buildTree(filtered);
  }, [events, filter, query]);

  const timestamps = events.map((e) => e.timestamp);
  const minTs = timestamps.length ? Math.min(...timestamps) : 0;
  const maxTs = timestamps.length ? Math.max(...timestamps) : 1;

  const selected = events.find((e) => e.id === selectedId) || null;
  const categories: string[] = ["run", "llm", "decision", "tool", "mcp", "env", "state", "human", "error"];

  return (
    <div className="trace-view">
      <div className="trace-toolbar">
        <input
          className="filter-input"
          placeholder="search events…"
          value={query}
          onChange={(e) => onQuery(e.target.value)}
        />
        <select value={filter} onChange={(e) => onFilter(e.target.value)}>
          <option value="">all categories</option>
          {categories.map((c) => (
            <option key={c} value={c}>{c}</option>
          ))}
        </select>
        <span className="muted">{events.length} events</span>
      </div>
      <div className="trace-body">
        <div className="trace-timeline">
          {tree.map((n) => (
            <EventRow
              key={n.id}
              node={n}
              depth={0}
              selectedId={selectedId}
              onSelect={onSelect}
              minTs={minTs}
              maxTs={maxTs}
            />
          ))}
          {tree.length === 0 && <div className="empty">No events match.</div>}
        </div>
        <div className="trace-inspector">
          {selected ? (
            <EventInspector event={selected} />
          ) : (
            <div className="empty">Select an event to inspect input, output, and metadata.</div>
          )}
        </div>
      </div>
    </div>
  );
}