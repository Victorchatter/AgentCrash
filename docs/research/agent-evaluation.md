# Agent Evaluation & Behavioral Testing — Research for AgentCrash

> Research note. Establishes the 2026 landscape of agent/LM evaluation
> frameworks, then drills into the distinction between *textual* and
> *behavioral* evaluation, the structure of regression tests for agents, and
> the "recorded failure → regression test" pipeline that is AgentCrash's
> reason for existing. Companion to `agent-tracing.md` (capture),
> `agent-replay.md` (deterministic re-execution), and
> `agent-chaos-engineering.md` (fault injection).

---

## 1. The 2026 evaluation landscape at a glance

The frameworks the user asked about fall into three tiers that overlap but
have different centers of gravity:

| Tool | Layer | Language | Core idea | What it is *not* good at |
|---|---|---|---|---|
| **Ragas** | RAG metrics library | Python | Academic-grade faithfulness/context precision/recall; isolates retrieval vs. generation faults | Non-RAG agents; trajectory/assertion gating |
| **DeepEval** | "Pytest for LLMs" | Python | 14+ metrics, `assert_test()`, G-Eval (plain-English rubric), standard exit codes for CI merge gates | Multi-provider model matrices; red-teaming |
| **Promptfoo** | Declarative CLI matrix | YAML/JS | `promptfooconfig.yaml` → run N prompts × M providers × assertions; built-in red-team (jailbreak, prompt injection, PII) | Live production monitoring; deep RAG metrics |
| **Inspect AI** (UK AISI) | Agent eval harness | Python | `Task` (dataset of `Sample`s) → `Solver`/`Agent` operating on `AgentState` → `Scorer`; sandboxing (Docker/K8s/Proxmox); 200+ prebuilt evals | Production observability — it is an *eval lab*, not a tracing backend |
| **MLflow Evals** | Full-lifecycle MLOps | Python | 50+ built-in eval metrics, OTel-native tracing, AI gateway, Unity Catalog; 60M+ monthly downloads | Best features need Databricks; GenAI eval layer is younger than RAG-specific tools |
| **LangSmith datasets** | Dataset + experiment store | Python/JS | `Dataset` of `Example`s (input + reference output + metadata); `Experiments` compare versions; regression view highlights red/green vs. baseline | Framework-locked to LangChain/LangGraph ecosystem |
| **Braintrust evals** | Eval-first platform | Py/TS/Go/Java… | Data + Task + Scores; immutable experiment snapshots; `BaseExperiment()` hill-climbing; GitHub Actions CI; AutoEvals (`ExactMatch`, `Factuality`, `Battle`, `NumericDiff`) | Self-hosting / full data ownership (proprietary, SOC2/HIPAA) |
| **OpenAI Evals** | Benchmark registry | Python | YAML eval definitions, model-graded scoring, community eval registry; lightweight | Production iteration; dataset management at scale |

Three honest observations from the 2026 comparative literature:

1. **Nobody runs just one.** Mature teams layer two: a **CI gate**
   (DeepEval or Promptfoo) plus a **monitoring/drift** scorer (Ragas on
   sampled production traffic, or Braintrust/LangSmith experiments on a
   schedule).
2. **All LLM-as-judge metrics have a real per-run cost and a drift problem.**
   Recommendations converge on: cheap fast judges for high-volume metrics,
   strong judges for safety-critical scoring, aggressive caching in CI,
   **pinning the judge model version**, and keeping gating suites small
   (10–30 cases) — the same "thin golden suite" rule that classical
   regression testing arrived at decades ago.
3. **The interesting frontier in 2026 is not RAG metrics, it is *agent
   trajectory* and *behavioral* regression.** That is where AgentCrash lives.

### Where Inspect AI sits, and why it matters to us

Inspect (UK AISI, MIT-licensed, 2.2k★) is the most architecturally relevant
comparator for AgentCrash because it is the only mainstream framework that
treats **the agent run as the unit of evaluation** rather than the completion:

- **`Task`** = a dataset of `Sample`s (input + optional target).
- **`AgentState`** carries `state.messages` (full conversation: system prompts,
  assistant turns, tool calls, tool results) and `state.output` — this *is* the
  trajectory.
- **`Solver`/`Agent`** runs `get_model().generate_loop(state.messages, tools)`,
  looping until the model stops calling tools. Each tool invocation and model
  response is appended to `state.messages`.
- **`Scorer`** is decoupled from the agent and scores `state.output.completion`
  (or the whole trajectory).
- **Sandboxing** is first-class: three isolation axes (tooling, host, network)
  via Docker Compose / Kubernetes / Proxmox plugins. AISI reports CAISI, METR,
  and Apollo Research all running these sandbox providers.
- **Limits** (token/message/time) and **Checkpointing** (mid-execution state
  save for recovery) are built in.

Inspect's `AgentState.messages` is essentially the same object AgentCrash's
trace capture produces (see `agent-tracing.md`). The difference is that Inspect
runs *forward* (drive the agent, capture state, score it), while AgentCrash
runs *backward* from a recorded crash (replay the trace, assert invariants).
Both need the same substrate: a faithful, boundary-captured trajectory.

---

## 2. Textual vs. behavioral evaluation — the distinction that defines AgentCrash

### Textual evaluation

Textual eval asks **"was the output text good?"** It treats the agent as a
black box that maps `input → output_text` and scores that text:

- Exact match / BLEU / ROUGE / semantic similarity to a reference.
- LLM-as-judge rubrics: faithfulness, answer relevancy, toxicity, correctness
  (Ragas faithfulness, DeepEval G-Eval, Promptfoo `llm-rubric`, Braintrust
  `Factuality`, openevals `CORRECTNESS_PROMPT`).
- Pairwise / battle comparisons (Braintrust `Battle`).

This is the right layer for: RAG answer quality, summarization, classification,
copywriting — any task where the *final string* is the product. It is
**necessary but not sufficient** for agents, because two agents can produce
the identical correct final string while one took a safe path and the other
nearly deleted a production database and recovered by luck.

### Behavioral evaluation

Behavioral eval asks **"did the agent *behave* correctly across the whole
run?"** It opens the black box and asserts properties of the *trajectory*: the
sequence of decisions, tool calls, state transitions, and resource
consumption. Concretely it checks:

| Property | Example assertion |
|---|---|
| **Tool choice** | "Agent called `read_file` before `write_file`." |
| **Tool arguments** | "`write_file` path stayed inside `/workspace`." |
| **Forbidden actions** | "Agent never called `drop_table` / never hit `rm -rf` / never sent an outbound email." |
| **Invariants** | "Shopping cart total == sum of line items after every step." |
| **Policy / guardrail boundaries** | "Agent escalated to human when confidence < 0.6." |
| **State contracts** | "After `checkout`, order status ∈ {paid, failed}." |
| **Resource envelope** | "≤ 12 tool calls, ≤ $0.40, ≤ 45s wall, ≤ 2 retries per tool." |
| **Recovery behavior** | "On HTTP 500, agent retried with backoff then degraded gracefully." |
| **Trajectory shape** | "Agent did not loop > 3 times on the same tool with the same args." |

The 2026 literature names this explicitly. The **B.A.S.E. harness**
(AgentEngineering.org) decomposes agent regression tests into:

- **B**aseline tasks — a fixed, curated set of representative scenarios.
- **A**ssertions — property checks over outputs *and* trajectories.
- **S**ide effects — externally observable state changes (files written, rows
  mutated, HTTP calls made), asserted independently of the agent's self-report.
- **E**nvelopes — the operating limits (cost, latency, retries, tool-call
  count) inside which the agent must stay.

LangSmith's "Evaluate a complex agent" doc codifies three evaluation *levels*
that line up cleanly:

1. **Final response** — end-to-end correctness vs. reference (textual).
2. **Trajectory** — did the agent take the expected sequence of steps, with
   partial credit for order/omission.
3. **Single step** — evaluate one component in isolation (e.g., the intent
   classifier's routing decision).

Behavioral eval = levels 2 + 3 + the side-effect/envelope checks B.A.S.E. adds
on top. **AgentCrash is a behavioral-regression tool.** Its job is to take a
recorded trajectory and turn the things the agent *did wrong* in it into
assertions the agent must *never do wrong again*.

### The non-determinism wrinkle

A naive behavioral test says "agent must call tools in this exact order." Agent
runs are stochastic, so that test flakes. The 2026 frontier work
(**AgentAssay**, arXiv:2603.02601) addresses this head-on:

- **Three-valued stochastic verdicts** — Pass / Fail / **Inconclusive** —
  backed by confidence intervals and sequential probability ratio tests
  (SPRT), replacing binary pass/fail.
- **Behavioral fingerprinting** — map execution traces to compact vectors on a
  low-dimensional manifold; detect regression via Hotelling's T² on the
  fingerprint distribution rather than per-case equality.
- **Agent-specific coverage** across tool, decision-path, state-space,
  boundary, and model dimensions.
- **Mutation testing operators** for prompts, tools, models, and context
  windows.
- **Metamorphic relations** tailored to multi-step workflows ("if I ask the
  same thing twice, the second run should not cost 10× more").
- Reports 78–100% cost reduction vs. naive repeated runs and **86% detection
  power where binary testing has 0%**, across 5 models and 7,605 trials.

The practical takeaway for AgentCrash: **deterministic replay is the substrate
that collapses stochasticity.** If you replay a *recorded* trace through the
agent loop with the LLM responses stubbed (see `agent-replay.md`), the run is
deterministic by construction and binary assertions are valid again. Where
AgentCrash instead *re-runs* the agent live (counterfactual / regression
against a fresh model call), it must adopt the three-valued verdict model and
treat a single failure as evidence, not proof.

---

## 3. The anatomy of an agent regression test

Synthesizing B.A.S.E., LangSmith's trajectory tests, Inspect's
Task/Sample/Scorer, and the OCI Agent Evaluation Framework's lifecycle model,
an agent regression test in 2026 has these parts:

### 3.1 Input (the stimulus)

- A fixed task description / user message (the "prompt under test").
- Optional initial state: pre-seeded files, DB rows, environment variables,
  mocked clock, mocked tool responses (the **frozen environment**).
- Optional `seed` / model config pins, so a live re-run is reproducible to the
  degree the model allows.

### 3.2 Expected behavior (the oracle)

The oracle is rarely a single expected string. It is a *set of properties*,
any of which can be textual or behavioral:

- **Expected final output** (reference string / schema / regex) — textual.
- **Expected trajectory** — ordered or partial-order sequence of tool calls,
  with optional argument matchers (`"path endswith .json"` rather than
  `"path == /tmp/x.json"`).
- **Expected side effects** — the diff the run should produce on the world
  (files created, DB rows written, HTTP requests issued), compared with
  tolerance.
- **Expected invariants** — predicates that must hold *at every step* (not
  just at the end): cart-total == sum(line_items); connection-pool-size ≥ 0;
  no two concurrent `write_file` to the same path.
- **Expected recovery** — on a simulated fault (HTTP 500, tool timeout), the
  agent must retry-with-backoff, then degrade, then surface a structured
  error — never silently retry forever.

### 3.3 Forbidden actions (the anti-oracle)

Equally important: things the agent must *never* do, asserted as hard failures
rather than scored:

- Forbidden tool calls (`drop_table`, `shell` with `rm -rf`, `send_email` to
  non-allowlisted recipients).
- Forbidden argument patterns (paths outside the workspace root; SQL with
  `DELETE` without `WHERE`; HTTP to non-allowlisted hosts).
- Forbidden loops (same tool + same args > N times → retry-storm).
- Forbidden information flow (PII appearing in an outbound request body; secret
  env vars echoed into a tool argument).

### 3.4 Tool constraints (the contract per tool)

Per-tool rules that constrain *how* a tool may be called:

- **Schema conformance** — arguments match the tool's JSON schema.
- **Preconditions** — `write_file` requires a prior `read_file` or explicit
  `create`; `delete_row` requires the row exists.
- **Idempotency expectations** — calling `create_user` twice with the same
  args must not produce two users.
- **Side-effect classification** — tool is `read`/`write`/`destructive`/
  `network`/`cost-incurring`; the test harness enforces allowlists per
  classification.
- **Argument matchers** — flexible matchers (contains, regex, JSONPath,
  schema-equal) so a re-run with a slightly different but valid path still
  passes.

### 3.5 Cost / latency / retry budgets (the envelope)

The operating envelope the agent must stay inside, asserted as hard ceilings:

| Budget | Assertion form | Typical source |
|---|---|---|
| Token cost | `total_cost_usd ≤ 0.40` | sum of `gen_ai.usage.*` across spans |
| Latency | `wall_clock_ms ≤ 45_000` | span duration of root `invoke_agent_internal` |
| Tool calls | `tool_call_count ≤ 12` | count of `execute_tool` spans |
| Retries | `retries_per_tool ≤ 2` and `total_retries ≤ 5` | per-tool retry counter |
| LLM turns | `model_call_count ≤ 8` | count of `gen_ai.chat` spans |
| Concurrency | `max_inflight_tools ≤ 4` | span overlap analysis |
| Error rate | `tool_error_rate ≤ 0.25` | ratio of errored `execute_tool` spans |

These map directly onto OpenTelemetry GenAI semantic-convention attributes
(see `agent-tracing.md`): `gen_ai.usage.input_tokens`,
`gen_ai.usage.output_tokens`, `gen_ai.usage.cost`, span durations,
`gen_ai.tool.name`, `gen_ai.tool.call.id`. The envelope is computed from the
trace; the assertion is `trace.aggregate(budget) ≤ limit`.

### 3.6 The three check modes (B.A.S.E.)

For non-deterministic re-runs, B.A.S.E. specifies three check strictness
levels:

- **Exact** — deterministic replay, full equality (trace + side effects +
  output). Use when the environment is frozen and the LLM is stubbed.
- **Tolerance** — numeric/structural equality within a band (`cost within
  ±10%`, `tool_call_count within ±2`, `output semantically similar ≥ 0.85`).
  Use for live re-runs against the same model.
- **Comparative** — "new run is no worse than baseline" (pairwise judge,
  hill-climbing à la Braintrust `BaseExperiment()`). Use across model bumps.

AgentCrash should support all three; the default depends on whether the test
was generated from a **stubbed replay** (Exact) or a **live re-run** (Tolerance
/ Comparative).

### 3.7 Verdict semantics

Borrowing from AgentAssay, AgentCrash's regression runner should emit
three-valued verdicts rather than binary pass/fail when run live:

- **Pass** — all invariants held, all budgets respected, oracle matched
  within tolerance, confidence ≥ threshold over N repetitions.
- **Fail** — a hard assertion violated (forbidden action, invariant broken,
  budget exceeded, required step missing). Even one violation is a fail.
- **Inconclusive** — live run, insufficient repetitions to reach confidence,
  or LLM-judge returned ambiguous. Triggers more sampling, never blocks CI on
  its own.

A **deterministic replay** run collapses this to binary Pass/Fail because
the stochasticity is gone.

---

## 4. From a recorded failure to a regression test — the pipeline

This is the core AgentCrash workflow and the thing none of the surveyed
frameworks do well today. They all assume a human writes the test. AgentCrash
can generate it from a crash recording.

### 4.1 The five-stage pipeline

```
   ┌────────────┐   ┌──────────────┐   ┌──────────────┐   ┌───────────────┐   ┌────────────┐
   │ 1. Record  │──▶│ 2. Diagnose  │──▶│ 3. Extract   │──▶│ 4. Freeze env │──▶│ 5. Emit    │
   │   crash    │   │   failure    │   │  conditions  │   │  + invariants │   │  test.yaml │
   └────────────┘   └──────────────┘   └──────────────┘   └───────────────┘   └────────────┘
   (tracing)        (replay+analyze)   (classify)         (snapshot)          (schema)
```

**Stage 1 — Record.** The agent run is captured as an OTel-shaped trace (see
`agent-tracing.md`): one `invoke_agent_internal` root span, nested
`gen_ai.chat` and `execute_tool` spans, with `gen_ai.tool.call.arguments` /
`gen_ai.tool.call.result` opt-in content. The crash is flagged by a signal —
an unhandled exception, a forbidden-action detector firing, an envelope
breach, or a human "this was wrong" report.

**Stage 2 — Diagnose.** Replay the trace deterministically (LLM stubbed, tool
results replayed — see `agent-replay.md`) and locate the first boundary
crossing where the behavior diverged from "correct." Classify the failure
against the AgentCrash failure model (`agent-chaos-engineering.md`): wrong
tool, wrong arguments, wrong order, retry storm, invariant violation, envelope
breach, forbidden action, recovery failure.

**Stage 3 — Extract conditions.** From the failing trace, mechanically derive
the assertions:

- The **negative assertion** is the mirror of the failure: if the agent
  called `rm -rf /`, the test forbids destructive shell to non-workspace
  paths. If the agent looped 11× on the same failing tool, the test caps
  retries at 2. If the run cost $1.80, the test sets the envelope at $0.40
  (or a tolerance band around the *good* baseline, if one exists).
- The **positive assertion** is the invariant that should have held: cart
  total == sum of line items; `write_file` only after `read_file`; outbound
  email only to allowlisted recipients.
- The **trajectory oracle** is the *good* prefix of the run up to the
  divergence point, if available — the steps before the failure are usually
  correct and become the expected prefix.

**Stage 4 — Freeze environment.** Capture the full nondeterministic input set
so the test is reproducible:

- Seed files, DB snapshot, env vars, mocked clock time, RNG seed.
- Stubbed LLM responses (the recorded `gen_ai.chat` outputs) — turns the test
  into an Exact-mode deterministic replay.
- Stubbed tool responses (the recorded `execute_tool` results) — OR, for
  tools whose behavior is deterministic and cheap, a real re-execution
  against the seeded state.
- Allowlisted tool set + per-tool argument matchers derived from the recorded
  calls.

The frozen environment is what makes a regression test *cheap* to run (no
live LLM calls) and *deterministic* (binary verdicts valid). AgentCrash stores
it alongside the test.

**Stage 5 — Emit test.** Serialize to the schema in §5. The test is now a
self-contained file: input + frozen env + oracle + forbidden actions +
envelope. Drop it into the regression suite; CI runs it on every change.

### 4.2 Failure-condition extraction heuristics

Mapping failure classes to assertion kinds:

| Failure class (from trace) | Extracted assertion |
|---|---|
| Tool called with out-of-schema args | `tool_constraint: schema_conform` for that tool |
| Path traversal outside workspace | `forbidden_action: arg_path_regex` + `tool_constraint: workspace_root` |
| Retry storm (same tool+args, N>threshold) | `envelope: retries_per_tool ≤ K` + `forbidden_action: identical_retry_loop` |
| Cost overrun | `envelope: total_cost_usd ≤ X` (X = good-baseline p95 × multiplier) |
| Invariant violated mid-run | `invariant: <predicate>` asserted `at_every_step` |
| Required step missing (e.g., no `read` before `write`) | `expected_trajectory: partial_order` constraint |
| Forbidden tool invoked | `forbidden_action: tool_name` hard-fail |
| Recovery failure (no backoff on 500) | `expected_recovery: retry_with_backoff_then_degrade` |
| Hallucinated file path | `tool_constraint: arg_path_must_exist` |

### 4.3 What a human still does

Auto-generation is a starting point, not a finish line. The pipeline should
flag for human review:

- The **envelope ceilings** (auto-derived from a single bad run are too tight;
  a human picks a realistic budget from baseline distribution).
- The **invariant predicates** (auto-extracted ones are often too narrow or
  too broad — a human generalizes them).
- The **oracle strength** (whether to assert exact trajectory or partial
  order — a human decides based on whether the steps are load-bearing).
- Whether the test is **Exact / Tolerance / Comparative** mode.

AgentCrash's UX should present the auto-generated test as a *draft* with
clearly marked review points, not as a fait accompli.

---

## 5. Proposed AgentCrash regression-test schema

A single test is a YAML (or JSON) document. The shape below is designed to (a)
subsume B.A.S.E., (b) interoperate with OTel GenAI attribute names so the
trace *is* the evidence, (c) be runnable in Exact (stubbed replay) or
Tolerance (live) mode, and (d) be auto-generatable from a crash recording.

```yaml
# agentcrash regression test — v1
id: cart-write-before-read-2026-07-15
generated_from:
  crash_id: cr_01HABCDEF...
  trace_id: 0af7...
  failure_class: forbidden_action.path_traversal
  auto_generated: true
  review_state: draft           # draft | reviewed | locked

mode: exact                     # exact (stubbed replay) | tolerance | comparative
repetitions: 1                  # >1 only meaningful for tolerance/comparative

input:
  task: "Refund order #4317 and email the customer a receipt."
  initial_state:
    files:
      /workspace/orders/4317.json: { content: "<seeded json>" }
    db_snapshot: snap_01HABC...
    env:
      ALLOWED_EMAIL_DOMAINS: "example.com"
    clock_frozen_at: "2026-07-15T10:00:00Z"
    rng_seed: 42

frozen_environment:
  llm_stubs: true               # replay recorded gen_ai.chat outputs
  tool_stubs:                   # which tools are stubbed vs re-executed
    read_file: reexecute        # deterministic on seeded fs
    send_email: stub            # never really send
    http_get: stub
  allowlisted_tools: [read_file, write_file, send_email, http_get]

oracle:
  expected_output:
    type: regex                 # exact | regex | schema | llm_judge | none
    value: "Refund processed.*receipt sent to .*@example.com"
  expected_trajectory:
    mode: partial_order         # exact_order | partial_order | set | none
    steps:
      - { tool: read_file, args: { path: { endswith: "4317.json" } } }
      - { tool: write_file, args: { path: { startswith: "/workspace/" } } }
      - { tool: send_email, args: { to: { domain_in: [example.com] } } }
  invariants:                   # predicates asserted at EVERY step
    - id: cart-total-integrity
      predicate: "cart_total == sum(line_items)"
      at_every_step: true
    - id: no-write-before-read
      predicate: "not exists(write_file) unless prior(read_file)"
      at_every_step: true
  side_effects:
    expected_files_written: [ /workspace/refunds/4317.json ]
    expected_db_mutations: [ orders.4317.status -> "refunded" ]
    expected_http: []
    forbidden_http_hosts: [ "*.internal", "169.254.169.254" ]

forbidden_actions:
  - { kind: tool_call, tool: drop_table }
  - { kind: tool_arg_regex, tool: shell, arg: command, regex: "rm\\s+-rf\\s+/" }
  - { kind: identical_retry_loop, max_same_call: 3 }
  - { kind: pii_in_tool_arg, tool: http_get, arg: url, pii_types: [email, ssn] }

tool_constraints:
  read_file:
    schema_conform: true
    arg_path_must_exist: true
  write_file:
    schema_conform: true
    preconditions: [ prior_tool: read_file ]
    arg_path_regex: "^/workspace/"
    classification: write
  send_email:
    arg_recipients_allowlist: [ "@example.com" ]
    classification: network
    rate_limit_per_run: 5

envelope:
  total_cost_usd:    { max: 0.40 }
  wall_clock_ms:     { max: 45_000 }
  tool_call_count:   { max: 12 }
  model_call_count:  { max: 8  }
  retries_per_tool:  { max: 2  }
  total_retries:     { max: 5  }
  tool_error_rate:   { max: 0.25 }

expected_recovery:
  - trigger: { tool: http_get, status: 500 }
    behavior: retry_with_backoff_then_degrade
    max_retries: 2
    on_exhaustion: surface_structured_error

scoring:
  # for tolerance / comparative modes
  judges:
    - { kind: llm_judge, metric: correctness, model: "gpt-5.4-mini", threshold: 0.85 }
  verdict: three_valued           # binary | three_valued
  inconclusive_action: resample   # resample | pass | fail
```

### 5.1 Schema design notes

- **`generated_from`** is the provenance link back to the crash. Every
  auto-generated test carries its crash id; a test is never orphaned from the
  failure that birthed it.
- **`mode`** determines the verdict semantics (§3.7). `exact` → binary;
  `tolerance`/`comparative` → three-valued.
- **`input.initial_state` + `frozen_environment`** together are the
  "freeze environment" stage output. `llm_stubs: true` is what makes the test
  free and deterministic.
- **`oracle.invariants`** with `at_every_step: true` is the behavioral
  invariant layer — evaluated after each span, not just at the end.
- **`forbidden_actions`** are hard-fail regardless of mode. A forbidden action
  is never "inconclusive."
- **`tool_constraints`** are per-tool contracts, independently checkable and
  reusable across tests (a library of tool contracts, not per-test rewrites).
- **`envelope`** values are numbers (or `{max}`/`{min}`/`{within}` objects),
  computed from the trace via the OTel attributes in §3.5.
- **`expected_recovery`** is first-class because recovery behavior is the
  failure class most often missed by textual eval and most often responsible
  for production incidents (see `agent-chaos-engineering.md`).
- **`scoring.verdict: three_valued`** defaults on for live modes; AgentCrash's
  runner emits Pass/Fail/Inconclusive and only Pass/Fail block CI (Inconclusive
  triggers resampling, gated by a max-repetitions cap).

### 5.2 Runner contract

The runner (proposed in a later doc) takes a test + an agent-under-test and:

1. Materializes `initial_state` (seed fs, db snapshot, env, clock, rng).
2. Drives the agent with `input.task`, either stubbed (Exact) or live.
3. Captures the resulting trace in OTel GenAI shape.
4. Evaluates every assertion in order: forbidden_actions (hard-fail) →
   invariants (per-step) → tool_constraints → envelope → oracle →
   expected_recovery → trajectory → side_effects.
5. Emits a verdict + the failing-span ids + a diff vs. expected (for
   trajectory/side-effects).
6. Writes the result back to the crash record that generated the test, closing
   the loop.

---

## 6. How AgentCrash relates to each surveyed framework

| Framework | AgentCrash relationship |
|---|---|
| **Ragas** | Out of scope for the core (Ragas scores RAG answer text). AgentCrash can *call* Ragas as an `oracle.expected_output` judge when the agent is a RAG agent. |
| **DeepEval** | Conceptual sibling ("pytest for LLMs"). AgentCrash's tests should be **exportable** to DeepEval `assert_test()` form so teams already on DeepEval can consume them. AgentCrash adds the auto-generation + trajectory layer DeepEval lacks. |
| **Promptfoo** | AgentCrash's red-team / forbidden-action probes overlap with Promptfoo's red-team mode. AgentCrash can emit a Promptfoo config for adversarial probing of the *same* agent under test. |
| **Inspect AI** | Closest architectural kin. AgentCrash's `Task/Input/Oracle` maps to Inspect's `Task/Sample/Scorer`; AgentCrash's frozen environment maps to Inspect's sandbox. A future bridge could run AgentCrash-generated tests inside Inspect's sandbox providers and vice-versa. |
| **MLflow Evals** | AgentCrash traces are OTel-native (per `agent-tracing.md`); MLflow ingests OTel. AgentCrash can ship traces to MLflow for lifecycle dashboards; MLflow's eval metrics layer is complementary, not competitive. |
| **LangSmith datasets** | AgentCrash's regression suite *is* a dataset of Examples (input + expected behavior). An exporter to LangSmith `Dataset` is straightforward and lets LangSmith users get AgentCrash auto-generated trajectory tests. |
| **Braintrust** | Braintrust's `BaseExperiment()` hill-climbing + `AutoEvals` are the right comparison layer for AgentCrash's `comparative` mode. AgentCrash's value-add is generating the test from a crash; Braintrust's is optimizing against it. |
| **OpenAI Evals** | AgentCrash can emit OpenAI-Evals-style YAML for any test whose oracle is purely textual (final-output check). The behavioral layers (trajectory, invariants, envelope) have no OpenAI Evals equivalent and stay AgentCrash-native. |
| **AgentAssay** | Theoretical foundation for AgentCrash's three-valued verdict model, behavioral fingerprinting, and trace-first offline analysis. AgentCrash should adopt fingerprint-based drift detection for "is the agent *gradually* regressing across releases" even when no single test fails. |
| **B.A.S.E.** | AgentCrash's schema is a superset of B.A.S.E.: Baseline = `input`, Assertions = `oracle` + `forbidden_actions` + `tool_constraints`, Side effects = `oracle.side_effects`, Envelopes = `envelope` + `expected_recovery`. |
| **OTel GenAI semconv** | The lingua franca AgentCrash uses for the trace that the tests assert over. `gen_ai.tool.name`, `gen_ai.tool.call.arguments`, `gen_ai.usage.*`, span durations — all double as assertion inputs. |

---

## 7. Concrete recommendations for AgentCrash

1. **Make the trace the evidence, the test the assertion.** Every regression
   test links to a `generated_from.crash_id`; every assertion evaluates
   against the OTel-shaped trace captured during the test run. No assertion
   without a trace field it reads from.

2. **Default to Exact mode (stubbed replay) for auto-generated tests.** It is
   free, deterministic, and gives binary verdicts — the cheapest credible CI
   gate. Offer Tolerance/Comparative as opt-in for tests that must re-run live
   (e.g., after a model bump).

3. **Adopt three-valued verdicts for any live mode.** Binary pass/fail on a
   stochastic re-run is a known footgun (AgentAssay: 0% detection power where
   three-valued gets 86%). Inconclusive → resample, with a capped retry
   budget; never block CI on Inconclusive alone.

4. **Ship a per-tool contract library, not per-test constraints.** Tool
   constraints (`schema_conform`, `arg_path_regex`, `classification`,
   `rate_limit_per_run`, `preconditions`) should be defined once per tool and
   referenced by tests. This is the reusable capital; per-test assertions are
   the disposable part.

5. **Auto-generate as draft, require human review for envelope + invariants.**
   The pipeline in §4 can produce the full schema mechanically, but envelope
   ceilings and invariant predicates derived from one crash are too tight or
   too narrow. Mark `review_state: draft` and surface the two fields for
   human edit before `locked`.

6. **Interoperate, don't reinvent.** Export to DeepEval (CI gate), LangSmith
   (dataset), Inspect (sandboxed run), Promptfoo (red-team), OpenAI Evals
   (textual oracle). AgentCrash's moat is the **crash → test generator** and
   the **behavioral assertion layer**, not another scoring metric.

7. **Pin judge models and cache judge results.** Every LLM-as-judge call is a
   drift source and a cost. Pin the judge model version per test suite; cache
   judge outputs keyed by (judge_model, prompt_hash, target_hash) in CI.

8. **Use behavioral fingerprinting for release-level drift.** Even when no
   single test fails, a shift in the trace-fingerprint distribution across a
   release signals latent regression. Adopt AgentAssay's manifold-fingerprint
   + Hotelling's T² approach as a release-gate signal alongside per-test
   verdicts.

9. **Forbid on hard violations, score on soft ones.** `forbidden_actions` and
   invariant breaks are hard-fail (one strike). Trajectory order, output
   similarity, and envelope proximity are scored. This separation maps to
   Inspect's "scorer vs. crash" split and to DeepEval's thresholded metrics.

10. **Close the loop to the crash record.** When a generated regression test
    later *fails* in CI, link the failure back to the original crash id and to
    the new failing trace — that is a new crash, which can itself generate a
    tighter test. AgentCrash's regression suite is self-seeding.

---

## 8. Open questions for later docs

- **Runner architecture.** How the test runner materializes initial_state,
  stubs the LLM/tools, captures the trace, and evaluates assertions —
  proposed in a follow-up `regression-runner.md`.
- **Tool contract library format.** Whether `tool_constraints` live in a
  separate registry file imported by tests, or inline. Lean toward registry +
  `$ref`.
- **Fingerprint algorithm.** Which features compose the behavioral vector
  (tool histogram, decision-path encoding, cost vector, error-rate vector)
  and the manifold dimensionality. Borrow from AgentAssay's evaluation.
- **Judge-cache key strategy.** How to key cached LLM-judge results so a
  model bump invalidates correctly but a code-only change does not.
- **Comparative mode baseline selection.** Which prior experiment is the
  `BaseExperiment()` for a given test — last green run on the same model? Best
  of last N? Needs a policy.

---

## Sources

- DeepEval vs Ragas vs Promptfoo comparisons: [qaskills.sh](https://qaskills.sh/blog/deepeval-vs-ragas-vs-promptfoo-2026), [genai.qa](https://genai.qa/blog/promptfoo-vs-deepeval-vs-ragas/), [knovo.dev](https://www.knovo.dev/guides/ai-evaluation-frameworks), [aiml.qa](https://aiml.qa/llm-evaluation-framework-benchmark-2026/)
- Inspect AI (UK AISI): [inspect.aisi.org.uk](https://inspect.aisi.org.uk/), [agents.html](https://inspect.aisi.org.uk/agents.html), [GitHub](https://github.com/ukgovernmentbeis/inspect_ai), [Sandboxing toolkit blog](https://www.aisi.gov.uk/blog/the-inspect-sandboxing-toolkit-scalable-and-secure-ai-agent-evaluations)
- AgentAssay (token-efficient regression testing): [arXiv:2603.02601](https://arxiv.org/pdf/2603.02601)
- Automated structural testing of LLM agents: [arXiv:2601.18827](https://www.arxiv.org/pdf/2601.18827)
- Layer-isolated evaluation: [arXiv:2606.11686](https://arxiv.org/html/2606.11686)
- Regression Testing for Agents (B.A.S.E.): [agentengineering.org](https://agentengineering.org/articles/regression-testing-for-agents/)
- OCI Agent Evaluation Framework: [blogs.oracle.com](https://blogs.oracle.com/ai-and-datascience/oci-agent-evaluation-framework)
- Braintrust vs OpenAI Evals / MLflow: [aisecurityandsafety.org](https://aisecurityandsafety.org/en/compare/openai-evals-vs-braintrust/), [respan.ai](https://www.respan.ai/market-map/compare/braintrust-vs-mlflow), [braintrust.dev/docs](https://www.braintrust.dev/docs/evaluation-quickstart)
- LLM Observability & Eval Index 2026: [dev.to](https://dev.to/srijan_paudel_d9837a5d8fd/the-llm-observability-eval-index-2026-25k4)
- LangSmith evaluation: [evaluation-concepts](https://docs.langchain.com/langsmith/evaluation-concepts), [evaluate-complex-agent](https://docs.langchain.com/langsmith/evaluate-complex-agent), [evaluation-types](https://docs.langchain.com/langsmith/evaluation-types)
- OpenTelemetry GenAI semantic conventions: [semantic-conventions-genai](https://github.com/open-telemetry/semantic-conventions-genai), [gen-ai-spans.md](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-spans.md)