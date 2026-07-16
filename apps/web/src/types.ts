export type EventStatus = "started" | "completed" | "failed";

export interface AgentCrashEvent {
  id: string;
  trace_id: string;
  parent_id: string | null;
  seq: number;
  timestamp: number;
  duration_ms: number | null;
  type: string;
  name: string | null;
  source: { integration: string; framework?: string; version?: string };
  actor?: { type: string; name?: string } | null;
  input: unknown;
  output: unknown;
  metadata: Record<string, unknown>;
  status: EventStatus;
  error?: { type?: string; message?: string; stack?: string } | null;
  privacy: { redacted: boolean; redaction_types: string[] };
  artifacts: unknown[];
  replay?: { replayable: boolean; frozen: boolean; fixture_key?: string | null } | null;
}

export interface Run {
  id: string;
  project_id: string;
  status: string;
  agent: string | null;
  model: string | null;
  started_at: number;
  ended_at: number | null;
  duration_ms: number | null;
  tool_calls: number;
  retries: number;
  cost_usd: number | null;
  error: string | null;
  root_cause: string | null;
  metadata: Record<string, unknown>;
}

export interface Project {
  id: string;
  name: string;
  created_at: number;
}

export interface ReplayResponse {
  replay_id: string;
  new_run_id: string;
  status: string;
  error: string | null;
  agent_output: unknown;
  diff_lines: string[];
  diff: {
    tool_calls_a: string[];
    tool_calls_b: string[];
    added: string[];
    removed: string[];
    reordered: boolean;
    arg_changes: string[];
    result_changed: boolean;
    regressions: string[];
    lines: string[];
  };
}

export interface AnalysisResponse {
  run_id: string;
  failed: boolean;
  root_cause: string | null;
  confidence: number;
  recommended_fix: string | null;
  suggested_invariant: Record<string, unknown> | null;
  summary: string[];
  candidates: {
    event_id: string;
    name: string;
    score: number;
    averted: boolean;
    evidence: { description: string; event_id: string | null; averted: boolean; replay_run_id: string | null }[];
  }[];
}

export interface TestSpec {
  name: string;
  input: unknown;
  source_run_id: string | null;
  invariants: Record<string, unknown>[];
  forbidden_actions: string[];
  required_actions: string[];
  must_succeed: boolean;
  description: string;
}

export interface TestRunResult {
  test_name: string;
  passed: boolean;
  status: string;
  violations: string[];
  run_id: string | null;
  events: AgentCrashEvent[];
}