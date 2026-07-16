# Execution Replay for AI Agents — Research for AgentCrash

> Research note. Establishes the conceptual landscape for AgentCrash's replay
> subsystem: capture/replay patterns, the determinism spectrum, nondeterminism
> handling, checkpoint and time-travel analogies, and a concrete proposed
> architecture. Companion to `agent-tracing.md` (capture) and
> `agent-crashes.md` (failure model).

---

## 1. Why replay matters for agents

An agent run is a **sequence of boundary crossings**: the LLM produces a
decision, the harness turns that decision into a tool call, the tool crosses
the process/network boundary and returns bytes, the harness feeds those bytes
back to the LLM. Almost every bug an agent can exhibit — a wrong tool argument,
a hallucinated file path, a retry storm, a regression after a model bump —
lives at one of those boundaries. Pure log inspection tells you *what*
happened; replay lets you *re-execute* the run and ask what-if questions.

The single most important property of a replay system is stated bluntly by the
"Stateful Agent Replay" practitioner write-up: **replay asserts "the agent did
the *same* thing," not "the *right* thing."** Correctness is a separate layer
on top. Replay gives you a deterministic substrate; you then layer assertions,
diffs, and counterfactuals on that substrate.

Three use cases drive the design:

1. **Reproduce a crash.** Record a failing run once, debug it as many times as
   needed without the model or tools being live.
2. **Regression test.** A recorded trace becomes a golden master; a code or
   model change that diverges from the golden master fails CI.
3. **Counterfactual analysis.** "What if this tool result had been different?"
   — answer by replaying with a substitution and observing downstream effects.

---

## 2. The core idea: capture at the boundaries, replay at the boundaries

All record/replay systems share one insight (Undo's time-travel-debugging
technical paper states it cleanly): **only nondeterministic *inputs* need
recording, not every instruction.** For an agent, the nondeterministic inputs
are exactly the boundary crossings:

| Boundary | Nondeterministic input |
|---|---|
| LLM provider | completion (tokens, tool calls), stop reason, usage |
| Tool / function call | return value, raised exception, latency |
| Shell / subprocess | stdout, stderr, exit code, signal |
| HTTP | status, headers, body |
| Filesystem | file contents read, directory listings, mtimes |
| Clock | `time.time()`, `time.monotonic()`, `datetime.now()` |
| RNG | `random` / `numpy` draws |
| IDs | UUIDs, snowflakes, ULIDs |
| Retries / backoff | which attempt succeeded, how long it slept |

Everything between boundaries — the agent loop logic, prompt assembly, parsing
— is assumed deterministic and is *not* recorded. It is re-executed on replay.
This is the same split rr makes for native code: re-execute the deterministic
instructions, inject the recorded syscall/signal inputs at the right moments.

### 2.1 The record phase

A recording harness wraps every boundary with a thin shim. In record mode the
shim calls the real boundary, captures the (input, output) pair, appends it to
an append-only capture, and returns the real result to the agent. The agent
code is **identical in record and replay mode**; only the shims switch
behavior. This is the "same loop body, different boundaries" pattern.

### 2.2 The replay phase

In replay mode each shim reads the next captured output for its boundary kind
and returns it *without calling the real boundary*. The agent loop re-executes
its own logic but every external touch is served from the capture. A mismatch
— the replaying agent asks for a tool the recording did not call, or calls it
with different arguments — is a **divergence** and the harness fails fast with
a diff rather than silently serving wrong data.

---

## 3. The determinism spectrum

Replay is not binary. Three modes, in increasing order of liveness and
decreasing order of determinism:

### 3.1 Deterministic replay (all external frozen)

Every boundary is served from the capture. The LLM is *not* called; its
recorded completion is returned. Tools are *not* re-executed; their recorded
outputs are returned. Clock reads return recorded timestamps. RNG draws return
recorded values. The run is bit-identical across replays (modulo the small
residual nondeterminism inside the agent process itself — see §4).

This is what VCR.py's `record_mode="none"` does for HTTP, what `agentrr` and
`AgentRewind` do for full agent traces, and what rr does for native code.
It is the mode for **crash reproduction** and **golden-master regression**.

### 3.2 Semi-deterministic replay (some frozen, some live)

A subset of boundaries are frozen, the rest are live. Two common shapes:

- **Freeze the LLM, run tools live.** The recorded model completions are
  replayed (so the decision path is fixed), but tool calls actually execute
  against a live (often stubbed or sandboxed) environment. Use case: verifying
  that a code change to a tool still produces compatible outputs for the
  recorded decision sequence.
- **Freeze tools, call the LLM live.** Tool outputs are served from the
  capture, but the LLM is called fresh. Use case: testing a new prompt or a
  new model against the same world state, or measuring a provider's residual
  nondeterminism by re-issuing identical requests.

Semi-deterministic replay is where **divergence detection** becomes essential:
because one side is live, it can disagree with the recording, and the harness
must surface that disagreement as a diff, not hide it.

### 3.3 Live replay (re-execution against live boundaries)

Nothing is frozen; the run is re-executed end to end against live model and
tools. This is not really "replay" in the record/replay sense — it is a fresh
run — but it is the **control** against which replay fidelity is measured.
agentrr and AgentRewind both include a fidelity check: run live, run replayed,
assert byte-for-byte (or behavior-for-behavior) equality. The residual
difference between two live runs is your lower bound for "honest" nondeterminism;
a replay that diverges no more than two live runs differ from each other is
as faithful as physics allows.

---

## 4. Handling nondeterminism

Nondeterminism is the enemy of replay. It comes in four flavors, each with a
known mitigation.

### 4.1 Temperature and sampling

Even at `temperature=0`, hosted LLMs are **not bit-identical across calls**.
Provider-side batching, kernel selection, and floating-point reduction order
mean the same prompt can yield a different tool-call sequence on a different
day. (Current Claude Opus models do not accept a `temperature` parameter at
all.) The honest response, used by `causal-agent-replay` and `agentrr`, is to
**measure** residual nondeterminism rather than assume it away: re-issue
identical requests N times, report the action-match distribution, and treat
anything below 100% as irreducible noise to be quantified, not eliminated.

For replay fidelity, the only robust answer is **freeze the model boundary**:
serve the recorded completion. Any replay that re-calls the model is
counterfactual by construction (§7), not deterministic replay.

### 4.2 Time

`time.time()`, `time.monotonic()`, `datetime.now()`, and `asyncio.sleep()` all
return different values across runs and drive time-dependent branches
(retry-with-backoff, TTL checks, "is the market open" guards). Mitigation:
wrap the clock in a `Clock` shim. Record every read; replay returns recorded
values in order. The agent calls `clock.now()` instead of `time.time()`.

### 4.3 Randomness

`random`, `numpy.random`, `secrets` all draw from entropy. Mitigation: wrap in
an `Rng` shim. In record mode, capture the seed once at start (a seeded
`random.Random` is deterministic from that seed, so individual draws need not
be captured — only the seed). In replay, reconstruct the generator from the
seed. For `secrets`/`os.urandom` (which do not accept a seed), capture each
draw individually.

### 4.4 Model and schema versioning

A recording made against `gpt-4o-2024-07-18` is not valid evidence about
`gpt-4o-2024-11-20`. A recording made when a tool returned schema v2 is
misleading once the tool returns v3. Mitigation:

- The capture header pins **model id**, **tool versions**, and **recorder
  version**. Replay refuses to run if the live environment's versions do not
  match the header (deterministic mode) or emits a warning (semi-deterministic
  mode).
- Treat a model bump as a **counterfactual intervention** (§7), not as a
  replay of the original. You are asking "what would this run have looked like
  under model X?" — which is a different question with a distributional answer.

---

## 5. Checkpoints and time-travel debugging analogies

### 5.1 rr — deterministic record/replay for native code

Mozilla's [rr](https://rr-project.org/) records a Linux process tree and
replays it with identical memory, registers, syscall data, and control flow.
The mechanism:

- **Record** captures all kernel inputs (syscalls, signals) plus the few
  nondeterministic CPU effects, using `ptrace` and hardware performance
  counters. Overhead ~1.2x on single-threaded Firefox — low enough to replace
  gdb in daily work.
- **Replay** re-executes the deterministic instructions and injects recorded
  inputs at the recorded moments, producing a bit-identical re-run.
- **Reverse execution** is implemented via **checkpoints**: rr takes
  cloneable replay sessions at exponentially growing intervals (O(log L)
  checkpoints for a run of length L). To "go backwards" to a point P, rr
  restores the nearest checkpoint before P and **executes forward** to P.
  Reverse execution is therefore not literally running backwards; it is
  forward replay from an earlier snapshot.
- **Marks** are positional references (trace time + tick count + step key)
  that can be seeked to efficiently.

The deep paper is "Engineering Record And Replay For Deployability"
([arXiv:1705.05937](https://arxiv.org/pdf/1705.05937)).

**Analogy to AgentCrash:** an agent trace is the rr trace; the boundary
crossings are the syscalls; the agent loop is the deterministic instruction
stream. An AgentCrash "checkpoint" is a snapshot of agent state (scratchpad,
working memory, loop index) at a step boundary, used to fast-forward
counterfactual replay (§7) without re-running prefix steps.

### 5.2 UndoDB / Undo — time-travel debugging for C/C++

[UndoDB](https://undo.io/products/udb/) uses binary instrumentation to
capture nondeterministic inputs (syscalls via ptrace, JIT rewriting of
nondeterministic CPU instructions, thread scheduling, shared memory, async
I/O). It maintains **dynamic snapshots scattered throughout execution** as
restart points for fast reverse replay — the same checkpoint idea as rr but
proprietary and not limited to emulating a single core. 100% gdb-compatible
with `reverse-continue`, `reverse-step`, etc.

Undo's technical paper makes the two key insights explicit:
1. Only nondeterministic *inputs* need recording.
2. Reverse execution is achieved by replaying from a snapshot before the
   target point.

These are the two insights AgentCrash inherits directly.

### 5.3 Pernosco — omniscient debugging on top of rr

[Pernosco](https://pernos.co/) builds on rr recordings to give instant access
to full program state at any point in time, with data-flow and control-flow
history visualization. The lesson for AgentCrash: once you have a deterministic
recording, you can build a **queryable past** — every variable's value at
every step, every data dependency — without re-running. The trace becomes a
database of "what was true when."

### 5.4 Playwright Trace Viewer — agent-shaped record/replay already exists

[Playwright Trace Viewer](https://playwright.dev/docs/trace-viewer) is the
closest existing analog to what AgentCrash needs, because a browser automation
run *is* an agent run (a loop that observes DOM state, decides an action,
executes it, observes again). A trace `.zip` contains:

- **DOM snapshots** at three moments per action: *before*, *action* (where the
  click landed, red dot), *after*. Interactive — open in a browser tab, use
  DevTools.
- **Screenshots** rendered as a filmstrip timeline; hover to magnify, click to
  jump, drag to select a range that filters all other tabs.
- **Network** — every HTTP request/response, sortable, with bodies.
- **Source mapping** — the exact line of test code for each action.
- **Errors** with a red vertical line on the timeline marking the failure.
- **Console, metadata, attachments** (visual-regression expected/actual/diff).

Recording modes (`on`, `on-first-retry`, `retain-on-failure`, `off`) are a
production-ready answer to the sampling problem: record everything during
failure retry, discard on success. This maps directly to AgentCrash's
"record on crash, sample otherwise" strategy.

The Trace Viewer's **multi-tab, timeline-correlated** UI is the UX template
for AgentCrash's viewer: one timeline, many synchronized panes (LLM turns,
tool calls, shell output, file diffs, errors), drag-to-filter.

### 5.5 VCR.py — the cassette pattern

[VCR.py](https://vcrpy.readthedocs.io/) (port of Ruby's VCR) is the canonical
HTTP record/replay library and the origin of the vocabulary ("cassette",
"record mode"). Its four record modes are worth memorizing because they map
exactly onto agent replay modes:

| VCR mode | Behavior | AgentCrash analog |
|---|---|---|
| `once` (default) | Replay if cassette exists, else record; error on unexpected new requests | Deterministic replay with strict divergence |
| `new_episodes` | Replay recorded, record new ones alongside | Semi-deterministic, append-on-novelty |
| `none` | Replay only; error on ANY new request | Strict deterministic (crash reproduction) |
| `all` | Always record, never replay | Live re-recording |

Internally VCR stores `(request, response)` tuples and matches incoming
requests against stored ones via configurable **matchers** (default: URI +
method; can add body, headers, query). A `play_counts` Counter tracks how many
times each response has been replayed; `all_played` reports whether everything
was consumed. `before_record_request` / `before_record_response` callbacks
allow redaction and transformation. This is almost exactly the
content-addressed, match-on-signature design AgentCrash needs.

**The match-on-signature decision is load-bearing.** VCR matches on URI+method
by default — a loose match that can serve the wrong body if a request varies
in a dimension you did not match on. The agent-replay ecosystem has converged
on the stricter **sequence-primary, signature-validated** matching used by
`agentrr`: the capture is an ordered list; the replay cursor advances in
order; the signature (tool name + canonical-arg hash) is checked and a
mismatch halts with a diff. This avoids the "served a stale response that
happened to match loosely" class of false-positive replay success.

---

## 6. What exists in the agent-replay ecosystem today

Several tools already implement pieces of this. AgentCrash should learn from
all of them and not reinvent the parts they got right.

- **agentrr** (PyPI, alpha) — deterministic record/replay debugger for AI
  agents. Records LLM calls (OpenAI/Anthropic), tool calls, clock/RNG/IDs.
  Crash-safe recording (`fsync` before acting; verified with `SIGKILL` in CI).
  Sequence-primary, signature-validated matching. Honest divergence: halts at
  the exact mismatch with a diff. Zero live calls in replay.

- **causal-agent-replay** (GitHub: jaineet17) — models a recorded run as a
  **structural causal model** (Pearl) and uses counterfactual interventions
  (`do_resample`, `do_action`, `do_observation`, `do_context`, `do_policy`)
  to attribute which step caused a failure. Honest that counterfactual replay
  yields a *distribution*, not a path, because the policy is stochastic;
  reports Monte-Carlo confidence intervals and action-match rate. Adapters for
  LangGraph/LangChain, OpenAI Agents SDK, CrewAI.

- **stepback** (GitHub: thehalleyyoung) — the most ambitious existing system
  and the closest architectural precedent for AgentCrash's counterfactual
  layer. Records agent runs as **signed `.sb` traces** (HMAC-SHA256 chain +
  Ed25519 signatures), then re-executes only steps affected by a substitution
  via **dirty-set propagation** through a dependency DAG. Includes bisect,
  minimization (ddmin, Shapley attribution), corpus sweeps, and policy audits.
  Has Rust/WASM/TypeScript/Go/JVM/.NET bindings and Lean/TLA+ proof
  artifacts. (See §7.3 — AgentCrash should adopt its dirty-set algorithm.)

- **AgentEval** (.NET) — two recording layers: conversation-level and
  model-round-trip-level ("Glass Box" sees internal retries, hidden turns,
  tool schemas sent to the model). Designed for CI/CD golden-master regression
  with zero API credentials at replay time.

- **AgentRewind / llm-run-recorder** (PyPI) — flight recorder that traces
  every LLM/tool call, replays deterministically, and **diffs two runs** to
  pinpoint divergence. Monkey-patches OpenAI/Anthropic SDKs in place. SQLite
  storage, web viewer, redaction policies, streaming capture, async support.

- **Stateful Agent Replay** (DEV Community write-up) — the clearest
  practitioner articulation of the boundary-wrapper pattern with working
  `Clock`, `Rng`, `ModelClient`, `ToolDispatcher` code and an OpenTelemetry
  tie-in (`replay.capture_uri` attribute on the root span; tail-based
  sampling keeping errors + a small percentage of healthy traces).

**Convergent lessons across all of them:**
1. Capture every side-effecting boundary, not just the LLM.
2. Sequence-primary, signature-validated matching; halt on mismatch with a diff.
3. Clock and RNG are first-class boundaries, not afterthoughts.
4. Measure residual model nondeterminism; never assume temperature=0 is deterministic.
5. Tie captures to OpenTelemetry trace IDs so replay is reachable from existing observability.
6. Replay checks "same," not "right" — correctness is a layer on top.

---

## 7. Proposed replay architecture for AgentCrash

### 7.1 What to capture

A **replay fixture** is a complete, self-contained description of one agent
run sufficient to reproduce it deterministically and to support
counterfactual interventions. Capture these boundary kinds, each as a typed
**step frame** in an append-only sequence:

| Step kind | Captured fields |
|---|---|
| `llm_call` | provider, model id, request (messages, tools, sampling params), response (content, tool_calls, stop_reason, usage), latency, retry count |
| `tool_call` | tool name, canonical argument hash + full args, return value, raised exception, latency |
| `shell` | command, stdout, stderr, exit code, signal, cwd |
| `http` | method, url, request headers/body, status, response headers/body |
| `fs_read` | path, bytes read, mtime |
| `clock` | kind (`wall`/`mono`), value |
| `rng` | generator id, seed (if reproducible) or draw value (if not) |
| `id_gen` | generator kind, value |
| `retry` | attempt index, delay, outcome |
| `exception` | error class, message, step reference |
| `router` | router name, choice, options (for branching decisions) |
| `parallel` | branch fan-out/fan-in metadata (for multi-agent) |

Every frame carries: `step_id` (monotonic), `parent_step_id` (dependency
edge), `trace_id` + `span_id` (OTel link), `input_hash` and `output_hash`
(canonical-JSON SHA-256), `timing`, and `cost` where applicable.

### 7.2 Replay fixture format

Learned from VCR (YAML/JSON cassettes), Playwright (zip of JSON + blobs),
rr (binary trace + checkpoints), and stepback (signed binary frames).

**File: `*.agentcrash` — a zip archive:**

```
trace.agentcrash (zip)
├── manifest.json          # header: magic, format_version, recorder_version,
│                          #   agent_id, model_ids[], tool_versions{},
│                          #   price_list_version, started_at, ended_at,
│                          #   outcome (success/error/crash), public_key
├── frames.jsonl           # append-only step frames, canonical JSON, one per line
├── blobs/                 # content-addressed large payloads (file contents,
│   └── sha256/...          #   big tool outputs, screenshots) referenced by hash
├── checkpoints/           # serialized agent-state snapshots at chosen steps
│   └── step_000042.json    #   (scratchpad, working memory, loop index)
└── attestation.sig        # Ed25519 signature over the Merkle root of frames
```

Design choices and rationale:

- **Append-only JSONL for frames** (not a single JSON blob) so a crashed run
  still yields a usable prefix — the same reason agentrr `fsync`s per frame.
  A crash mid-run is the *interesting* case; the format must survive it.
- **Canonical JSON** (sorted keys, no extra whitespace, UTF-8) for stable
  hashes — this is the "cache-safety boundary" from stepback's
  `canonical.py`. Hashes are `sha256:<hex>`.
- **Blobs out-of-line** so large tool outputs (file contents, screenshots)
  do not bloat frames.jsonl and can be deduplicated across runs.
- **Checkpoints** are agent-state snapshots stored as JSON. They are *not*
  required for deterministic replay (the boundary frames suffice) but they
  enable fast counterfactual replay (§7.4) and step-back debugging.
- **Signed attestation** so a fixture is tamper-evident and shareable — you
  can file a bug with a fixture and a reviewer can verify it is unmodified.
  Borrow stepback's HMAC-chain + Ed25519 approach.
- **Versioned manifest** pins model ids and tool versions; replay checks
  these and refuses or warns on mismatch.

### 7.3 The replay executor

The executor is the single component that stands between the agent loop and
the outside world. In AgentCrash it is a small, well-tested object with three
modes:

```
                  ┌─────────────────────────────────────────────┐
                  │               ReplayExecutor                │
   agent loop ───►│  record()  |  replay(strict)  |  replay(semi)│───► boundary
                  │     │             │                  │       │
                  │     ▼             ▼                  ▼       │
                  │  real call    captured frame     policy:      │
                  │  + capture    served verbatim    frozen/live  │
                  │               + signature check  per boundary │
                  └─────────────────────────────────────────────┘
```

**Strict (deterministic) mode** — every boundary is served from the fixture.
The executor maintains a **per-kind cursor** that walks forward through
frames. On each agent request:

1. Advance the cursor to the next frame of the requested kind.
2. Validate the signature: canonical-hash the agent's actual request and
   compare to the frame's `input_hash`. (Sequence-primary, signature-
   validated — the `agentrr` discipline.)
3. On match, return the frame's recorded output.
4. On mismatch, emit a `replay.divergence` event with a diff and **halt**.
   Never silently serve a mismatched frame.

**Semi-deterministic mode** — a per-boundary policy selects frozen or live for
each kind. Frozen boundaries behave as strict; live boundaries call through.
The executor still validates that live outputs are *compatible* with recorded
outputs (same schema, same shape) and logs divergences without halting — this
is the mode for "new model, same world" and "same model, new tool code"
experiments.

**Record mode** — every boundary call goes through, the executor captures
the (request, response, timing) frame, `fsync`s it, and returns the real
result. The agent loop is byte-for-byte identical to the other modes.

**Why one executor, not many wrappers?** A single chokepoint is auditable,
testable, and impossible to bypass by accident. Every SDK shim (OpenAI,
Anthropic, MCP, subprocess, requests, filesystem) funnels into it. This is
the lesson from AgentRewind's monkey-patching and from Stateful Agent
Replay's `ToolDispatcher` — but generalized to one object.

### 7.4 Counterfactual interventions

This is where AgentCrash graduates from "replay the same run" to "answer
what-if questions," and where the stepback architecture is the direct
precedent. The model is a **structural causal model**: the recorded trace is a
DAG of steps with dependencies (parent links, parallel-branch joins); an
**intervention** replaces one node's output; the replay engine re-executes
only the nodes whose inputs changed.

**Substitution types** (typed, from stepback's vocabulary):
- `substitute_tool_output(step_id, new_value)` — "what if this tool returned X?"
- `substitute_llm_response(step_id, new_completion)` — "what if the model said Y?"
- `substitute_model(step_id_range, new_model_id)` — "what if we used gpt-4o-mini here?"
- `substitute_prompt(step_id, new_messages)` — "what if the prompt were different?"
- `substitute_sampling(step_id, temperature, top_p)` — "what if we sampled?"
- `substitute_router_choice(step_id, new_choice)` — "what if the branch went the other way?"
- `substitute_exception(step_id, error)` — "what if this step had failed?"

**Dirty-set propagation** (adopt stepback's algorithm, which is mechanized in
Lean and TLA+ — do not reinvent it):

1. Walk the trace in topological order.
2. A step is **dirty** if (a) a substitution targets it directly, (b) its
   recomputed canonical input hash differs from the recorded one, (c) any
   parent is dirty, or (d) its recorded nondeterminism metadata requires
   re-execution.
3. **Clean steps** are served from the fixture with zero executor calls.
4. **Dirty steps** invoke the live executor (real LLM/tool) — unless the
   substitution directly supplies the output, in which case that value is
   used and the step is marked dirty-but-not-executed.
5. A **content-addressed step cache** keyed by `(step_kind, inputs_hash)`
   short-circuits dirty steps whose new inputs match a previously computed
   output (e.g., across a sweep of substitutions, many of which converge on
   the same recomputed step). Cache hits avoid executor calls entirely.

**Checkpoints make this fast.** Without checkpoints, a counterfactual at
step 900 of a 1000-step run requires re-executing steps 1-899 (even if
clean, they must be re-walked to reconstruct agent state). With a checkpoint
at step 890, the engine restores agent state at 890 and replays from there.
This is exactly rr's checkpoint-for-reverse-execution trick, repurposed for
forward counterfactual replay.

**Honesty about the answer.** A counterfactual replay with a live LLM
boundary yields a **distribution**, not a single path, because the policy is
stochastic. Report it as N replays with Monte-Carlo confidence intervals
(causal-agent-replay's discipline). A counterfactual with the LLM frozen
yields a single deterministic path but is answering "what if the *world* were
different, given the model made the same decisions?" — a narrower question.
Always state which.

### 7.5 Bisect and minimization

Two analysis operations on top of counterfactual replay, both directly from
stepback:

- **Bisect.** Given a good step and a bad step and a predicate, binary search
  the trace to find the first step where the predicate becomes true. CLI
  shape: `agentcrash bisect trace.agentcrash --good step:1 --bad step:42
  --predicate 'cost_usd > 0.50'`. The predicate runs against replayed state.
- **Minimization.** Given a failing trace and a candidate set of suspect
  substitutions, shrink the set to the minimal subset that still triggers the
  failure. Algorithms: ddmin (delta debugging), linear shrink, binary halving,
  and **Shapley attribution** to quantify each substitution's contribution.
  Output: a self-contained HTML report and a minimal failing fixture suitable
  for a regression test.

These turn a 1000-step trace into "the failure is caused by step 17's tool
output, and here is a 3-step fixture that reproduces it." That minimal
fixture is the unit of a regression test in AgentCrash's test generator.

### 7.6 Sampling: when to record

Borrow Playwright's `on-first-retry` discipline and the Stateful Agent Replay
OTel strategy:

- **Head-sample at 100% during canary** and for dev runs.
- **In production, tail-based sample:** keep every error/crash trace in full,
  plus a small percentage of healthy traces for golden-master drift detection.
- **Record-on-crash:** if the harness detects a crash signal (uncaught
  exception, agent loop budget exhausted, invariant violation), flush the
  in-progress capture to a fixture before exiting. agentrr's `fsync`-per-frame
  design makes this robust.
- Every root span carries `replay.capture_uri` so a trace in your OTel
  backend links directly to the fixture on disk or in object storage.

---

## 8. Risks and open questions

- **Provider nondeterminism is irreducible.** No capture strategy eliminates
  it; the best you can do is freeze the model boundary and measure the
  residual. AgentCrash must not advertise "100% faithful replay" for any mode
  that re-calls the model.
- **Tool schemas drift.** A fixture recorded against tool schema v2 is
  misleading under v3. The manifest must pin tool versions and the executor
  must refuse or loudly warn on mismatch. This is a real operational hazard
  for long-lived fixture libraries.
- **Capture size.** Full LLM responses + tool outputs + screenshots can be
  large. Blobs out-of-line, content-addressed dedup, and a redaction policy
  (VCR's `before_record_*` callbacks, AgentRewind's redaction) are mandatory.
  PII scrubbing must happen at record time, never at replay time.
- **Counterfactual cost.** Dirty-set propagation keeps it down, but a sweep
  over many substitutions across a corpus of fixtures is expensive. The step
  cache and checkpoint resumability (stepback's `sweep_checkpoint.py`) are
  needed for this to be tractable.
- **Multi-agent / parallel branches.** The frame format supports
  `parallel`/`router` steps and `parallel_branch_join` dependencies, but the
  dirty-set soundness proofs in the literature cover the modeled DAG, not
  arbitrary concurrency. AgentCrash should start with sequential + simple
  fan-out and treat full concurrent multi-agent as a research item.
- **Legal/sharing.** A fixture contains provider responses, which may be
  subject to provider terms. Signed attestation proves integrity but does not
  grant redistribution rights. Document this; do not build a public fixture
  sharing feature without resolving it.

---

## 9. Recommendation summary

AgentCrash's replay subsystem should:

1. **Capture at every boundary** via a single `ReplayExecutor` chokepoint —
   LLM, tools, shell, HTTP, filesystem, clock, RNG, IDs, retries. One
   executor, three modes (record / strict replay / semi replay), identical
   agent loop in all modes.
2. **Use a zip-based `.agentcrash` fixture** with append-only canonical-JSON
   frames, out-of-line content-addressed blobs, versioned manifest, signed
   attestation, and optional checkpoints. Survive mid-run crashes.
3. **Match sequence-primary, signature-validated** (the `agentrr` discipline).
   Halt on divergence with a diff; never serve a mismatched frame silently.
4. **Adopt stepback's dirty-set propagation** (Lean/TLA+-verified) for
   counterfactual replay, with a content-addressed step cache and checkpoints
   for fast forward replay from a saved agent-state snapshot.
5. **Treat counterfactuals as distributional** when the LLM is live; report
   Monte-Carlo intervals. Treat them as deterministic when the LLM is frozen,
   and state which question each mode answers.
6. **Provide bisect and ddmin/Shapley minimization** to reduce a failing
   trace to a minimal regression fixture.
7. **Pin model and tool versions in the manifest**; refuse or warn on
   mismatch. A model bump is a counterfactual, not a replay.
8. **Tie fixtures to OpenTelemetry trace IDs** (`replay.capture_uri` on the
   root span) so replay is reachable from existing observability, and use
   Playwright-style record-on-failure sampling in production.

---

## Sources

- [VCR.py documentation](https://vcrpy.readthedocs.io/en/latest/usage.html) — cassettes, record modes, matchers, callbacks
- [VCR.py advanced features](https://vcrpy.readthedocs.io/en/latest/advanced.html)
- [Playwright Trace Viewer](https://playwright.dev/docs/trace-viewer) — timeline, before/action/after snapshots, network, errors
- [Playwright Tracing API](https://playwright.dev/docs/api/class-tracing) — `start`/`stop`/`startChunk`, recording modes
- [rr: lightweight recording & deterministic debugging](https://rr-project.org/)
- [Engineering Record And Replay For Deployability (arXiv:1705.05937)](https://arxiv.org/pdf/1705.05937) — rr internals
- [Introducing rr — Robert O'Callahan](https://robert.ocallahan.org/2014/03/introducing-rr.html)
- [Pernosco](https://pernos.co/) — omniscient debugging on rr recordings
- [UndoDB / UDB](https://undo.io/products/udb/) — time-travel debugging for C/C++
- [Undo: Intro to Time Travel Debugging](https://undo.io/resources/technical-paper-time-travel-debugging)
- [Stateful Agent Replay (DEV Community)](https://dev.to/gabrielanhaia/stateful-agent-replay-deterministic-reruns-from-a-captured-trace-e9d) — boundary-wrapper pattern, Clock/Rng/ModelClient/ToolDispatcher, OTel tie-in
- [agentrr on PyPI](https://pypi.org/project/agentrr/) — sequence-primary matching, crash-safe recording, honest divergence
- [causal-agent-replay (GitHub)](https://github.com/jaineet17/causal-agent-replay) — SCM-based counterfactual attribution, distributional answers
- [stepback (GitHub)](https://github.com/thehalleyyoung/stepback) — signed `.sb` traces, dirty-set propagation, bisect, ddmin, Shapley, Lean/TLA+ proofs
- [AgentEval tracing docs](https://github.com/AgentEvalHQ/AgentEval/blob/main/docs/tracing.md) — conversation vs glass-box recording layers
- [llm-run-recorder / AgentRewind on PyPI](https://pypi.org/project/llm-run-recorder/) — run diffing, SDK monkey-patching, SQLite storage