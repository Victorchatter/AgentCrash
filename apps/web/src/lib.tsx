import { useState } from "react";
import type { AgentCrashEvent } from "./types";

export type Category = "run" | "llm" | "tool" | "mcp" | "env" | "state" | "human" | "error" | "decision";

export function categoryOf(type: string): Category {
  if (type.startsWith("run.")) return "run";
  if (type.startsWith("llm.")) return "llm";
  if (type.startsWith("tool.")) return "tool";
  if (type.startsWith("mcp.")) return "mcp";
  if (type.startsWith("agent.")) return "decision";
  if (type.startsWith("human.")) return "human";
  if (type.startsWith("error.")) return "error";
  if (type.startsWith("filesystem.") || type.startsWith("shell.") || type.startsWith("browser.") || type.startsWith("http."))
    return "env";
  if (type.startsWith("memory.") || type.startsWith("retrieval.")) return "state";
  return "env";
}

export const CATEGORY_COLORS: Record<Category, string> = {
  run: "#8b9bb4",
  llm: "#c084fc",
  tool: "#38bdf8",
  mcp: "#34d399",
  env: "#fbbf24",
  state: "#60a5fa",
  human: "#f472b6",
  error: "#f87171",
  decision: "#a78bfa",
};

export function statusColor(status: string): string {
  if (status === "failed") return "#f87171";
  if (status === "started") return "#fbbf24";
  return "#34d399";
}

export function fmtTime(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

export function fmtDate(ts: number): string {
  return new Date(ts).toLocaleString();
}

export function short(s: unknown, n = 60): string {
  const str = typeof s === "string" ? s : JSON.stringify(s);
  return str && str.length > n ? str.slice(0, n - 1) + "…" : str ?? "";
}

/** A collapsible, syntax-ish JSON viewer. No external deps. */
export function JsonView({ data, redacted }: { data: unknown; redacted?: boolean }) {
  const [open, setOpen] = useState(false);
  if (data === null || data === undefined) return <span className="muted">—</span>;
  const text = typeof data === "string" ? data : JSON.stringify(data, null, 2);
  const preview = typeof data === "string" ? data : JSON.stringify(data);
  return (
    <div className={`jsonview ${redacted ? "redacted" : ""}`}>
      <button className="jsonview-toggle" onClick={() => setOpen((o) => !o)}>
        {open ? "▼ collapse" : "▶ expand"}
      </button>
      {redacted && <span className="badge redacted-badge">REDACTED</span>}
      {open ? (
        <pre className="jsonview-full">{text}</pre>
      ) : (
        <pre className="jsonview-preview">{short(preview, 120)}</pre>
      )}
    </div>
  );
}

/** Build a parent->children tree from a flat event list. */
export type TreeNode = AgentCrashEvent & { children: TreeNode[] };

export function buildTree(events: AgentCrashEvent[]): TreeNode[] {
  const byId = new Map<string, TreeNode>();
  events.forEach((e) => byId.set(e.id, { ...e, children: [] }));
  const roots: TreeNode[] = [];
  events.forEach((e) => {
    const node = byId.get(e.id)!;
    if (e.parent_id && byId.has(e.parent_id)) {
      byId.get(e.parent_id)!.children.push(node);
    } else {
      roots.push(node);
    }
  });
  return roots;
}