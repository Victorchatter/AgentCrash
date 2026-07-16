import type {
  AgentCrashEvent,
  AnalysisResponse,
  Project,
  ReplayResponse,
  Run,
  TestRunResult,
  TestSpec,
} from "./types";

const BASE = "/api";

async function j<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status} ${res.statusText}: ${text}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  health: () => fetch(`${BASE}/health`).then(j<{ status: string; version: string }>),
  projects: () => fetch(`${BASE}/projects`).then(j<Project[]>),
  runs: (projectId?: string) =>
    fetch(`${BASE}/runs${projectId ? `?project_id=${projectId}` : ""}`).then(j<Run[]>),
  run: (id: string) => fetch(`${BASE}/runs/${id}`).then(j<Run>),
  events: (id: string) => fetch(`${BASE}/runs/${id}/events`).then(j<AgentCrashEvent[]>),
  replays: (id: string) => fetch(`${BASE}/runs/${id}/replays`).then(j<unknown[]>),
  replay: (id: string, body: unknown) =>
    fetch(`${BASE}/runs/${id}/replay`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(j<ReplayResponse>),
  analyze: (id: string, agent = "buggy") =>
    fetch(`${BASE}/runs/${id}/analyze`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ agent }),
    }).then(j<AnalysisResponse>),
  generateTest: (id: string, agent = "buggy") =>
    fetch(`${BASE}/runs/${id}/test/generate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ agent }),
    }).then(j<{ test_id: string; spec: TestSpec }>),
  tests: () => fetch(`${BASE}/tests`).then(j<{ id: string; name: string; spec: TestSpec; last_result: unknown }[]>),
  runTest: (testId: string, agent: string) =>
    fetch(`${BASE}/tests/${testId}/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ agent }),
    }).then(j<TestRunResult>),
};