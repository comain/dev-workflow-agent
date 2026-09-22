# dev-flow-agent Roadmap

## Status

Draft, 2026-08-06. Records the intended direction and task sequence for a remote
dev-workflow agent built on the existing UTA / CR agent foundations.

## Objective

`dev-flow-agent` runs a **development workflow remotely** as a node graph:
spec → design → design-review → plan → build → verify → ship, with some nodes
gated on a human.

Three capabilities distinguish it from `unit-test-agent` (UTA) and `cragent`:

1. **Manual task creation** with free-form input, including images (design
   sketches, screenshots, error captures), alongside the existing RDC/webhook
   triggers.
2. **Agent progress streaming** — live tool-level verbosity from the OpenCode
   harness, not stage-level polling.
3. **Hard human-in-the-loop gates** on named nodes (`design-review` is the
   motivating case): the workflow suspends, releases its worker, and resumes
   only after a human approves or supplies input.

Everything else — OpenCode harness, task queue, daemon, task-management UI,
callbacks — is intended to be **reused**, not rewritten.

## Context: three existing implementations, one lineage

`cragent` is already a fork-by-copy of UTA. The docstrings say so:

- `cragent/src/cr_agent/review_v2/daemon.py:1` — *"CR v2 daemon adapted from
  UTA `uta/tasks/scheduler.py`"*
- `cragent/src/cr_agent/review_v2/opencode_process.py:1` — *"Per-turn OpenCode
  process runner adapted from UTA `uta/opencode/process.py`"*

There is also **`spec_generator_agent`** (referred to as "spec-gen-agent
in ../corba"; the path on disk is `spec_generator_agent`). It carries its
own harness copy at `core/opencode/` — client, config, fallback, process,
~1,453 LOC, a subset of UTA's — plus its own task layer (`spec_tasks`,
`spec_task_events`, `spec_task_control`, `scheduler.py`). Like cragent it
declares `langgraph` but does not use it.

So dev-flow-agent would be the **fourth** copy of the harness, not the third.
Two copies is defensible; four is where drift compounds and the same OpenCode
stream-parsing bug gets fixed four times. The program starts with extraction,
not with a new fork.

**agent-core therefore has four consumers**, and anything called "generic" has
to be checked against all four.

UTA's copy is the **superset**. `uta/opencode/` carries `client.py`,
`server.py`, `tiered_router.py`, `rate_limit.py`, `fallback.py`, `net.py`,
`stream.py`, `process.py`. `cragent/src/cr_agent/review_v2/opencode_*` is a
trimmed adaptation of it. **Extract from UTA, not from cragent.**

Conversely, cragent's **task-management UI is the more developed one**:
`/admin`, `/api/v1/admin/{metrics,costs,tasks,running,fix-sessions}`,
stop/cancel/requeue, recent-task lists, per-task progress pages, and the
retrospective-feedback confirm/requeue queue. Take the UI shell from there.

### Division of labour

`agent-core` lives in its own repository at `../agent-core` (decided 2026-08-06).

| Layer | Source | Destination |
|---|---|---|
| OpenCode harness (spawn, stream, routing, fallback, rate limit) | `uta/opencode/` | `agent-core` |
| Generic runtime: events, controls, heartbeats, lease claim, quarantine | pattern shared by `uta/tasks/` + `cr_agent/review_v2/storage.py` — **not a liftable schema**, see below | `agent-core` |
| Event log + delivery | both (`task_events`, UTA stage events) | `agent-core` |
| Trigger/callback protocols (RDC, GitHub) | `uta/api_trigger/protocols/` | `agent-core` |
| Task daemon / scheduler loop | all three (425 LOC) | `agent-core` (Task 1c) |
| Task service API: CRUD, admin, controls, callbacks | all three (5,731 LOC) | `agent-core` (Task 1d) |
| Admin/report UI shell | `cr_agent/api/routes.py` | `agent-core` |
| Domain report rendering (findings / coverage / spec docs) | — | stays with each product |
| Identity, authz, human gates, SSE | **new** | `agent-core` |
| Findings, judge, personas, severity | — | stays in cragent |
| Language / mutation / coverage (~40k of UTA's 50k LOC) | — | stays in UTA |
| Spec generation, doc export, repo discovery | — | stays in spec_generator_agent |
| Workflow nodes, prompts, gate policy | — | dev-flow-agent |

## The load-bearing design problem: gates break the execution model

Today a daemon claims a task under a lease, runs the workflow **in-process**,
and heartbeats until done. A `design-review` gate that waits six hours — or
overnight — means:

- a worker slot pinned for the whole wait
- a lease that must be renewed throughout, or the task is reclaimed and re-run
- stall detection and `runner_heartbeats` firing false alarms
- any deploy or crash losing the entire run

The correct shape is **suspend, don't block**. The gate node persists workflow
state, writes a pending-gate record, and *releases* the worker. The task moves
to `waiting_human`, excluded from both `claim_next_task` and lease reclaim. On
human response the task re-enters the queue with a resume marker, and a
possibly different worker rehydrates state and continues.

Two constraints follow:

1. **A gate is its own node**, sited between OpenCode turns — never inside one.
   Checkpointed state must be serializable, so no live subprocess handle may
   cross a gate boundary.
2. **Gate policy is per-node configuration, not code**: `blocking`
   (design-review), `confirm-risky` (proceed unless the action is destructive),
   `notify-only`.

### Use LangGraph's durable interrupt; do not hand-roll it

UTA already runs `StateGraph` (`uta/graph/workflow.py`). cragent only carries
LangGraph as a dependency, exercised by a single import assertion
(`cragent/tests/test_config_defaults.py:91`); its workflow is imperative
Python plus a `ThreadPoolExecutor`. For a node graph with named human gates,
LangGraph's checkpointer + `interrupt()` / `Command(resume=...)` is precisely
the durable-suspend primitive we would otherwise reinvent.

**Resolved by Task 2** ([ADR-003](decisions/ADR-003-workflow-engine-and-durable-gates.md)):
LangGraph 1.x requires Python >= 3.10, so the 3.9 ceiling is 0.6.11 while
upstream is 1.2.10. dev-flow-agent moves to Python 3.13 + LangGraph 1.x;
`agent-core` stays `>=3.9` so cragent and UTA are not forced to follow. Suspend
and resume across processes is proven, harness in the loop.

## Gate contract vocabulary

Model gates on the Agent Client Protocol's distinction rather than inventing
one — it separates the two cases correctly, and matching it keeps the door open
to ACP-speaking clients later:

| Gate kind | ACP analogue | dev-flow use |
|---|---|---|
| `approve` | `session/request_permission` | confirm-risky nodes, destructive ops |
| `input` | `elicitation/create` (+ response schema) | `design-review` — wants comments, not a boolean |
| `cancel` | already exists as `task_controls` | abort a run |

`design-review` is an `input` gate, not an `approve` gate. Getting this
distinction into the schema on day one avoids a migration later.

## Known gaps in the inherited code

1. ~~**No SSE.**~~ **Addressed in Tasks 1b and 4.** Both repos persist events (`cr_agent/review_v2/storage.py:193`
   `task_events`; UTA stage events) but deliver by polling —
   `cr_agent/api/routes.py:869` emits a JS poll loop. A `StreamingResponse`
   over `id > last_seen` is cheap and satisfies the agent-progress requirement
   directly. Feed it from `uta/opencode/stream.py` for tool-level verbosity.
2. **No approval inbox.** *(Storage, query, and the workflow bridge landed in Tasks 1b and 5. The UI remains.)* This is a new cross-cutting object — a queue *across*
   tasks and across all three products, not a per-task page. **Timeout policy decided 2026-08-06: wait indefinitely.** An
   unanswered design review blocks its workflow rather than being auto-decided
   on a timer — acceptable precisely because the run holds no worker while
   suspended and the inbox makes every waiting gate visible. `expires_at` and
   `expire_gates()` exist for products that want a different policy.
3. ~~**No identity or authz**~~ **Addressed in Task 3.** cragent's routes
   carried no auth dependency and `feedback_sessions` recorded no user at all;
   `models.py:187` took an `operator` straight from an unauthenticated RDC
   payload. `agent_core.identity` now provides authenticated principals and
   authorized, attributed gate answers.

### Scope correction (2026-08-06)

Tasks 1c and 1d were **missing from the original roadmap**. It scoped the shared
layer to the harness and to runtime *storage*, and handed the daemon loop and
the HTTP surface back to the products without asking what else was duplicated
alongside them. Measurement:

| Layer | UTA | cragent | spec_generator_agent | total |
|---|---|---|---|---|
| daemon / scheduler | 53 | 248 | 124 | **425** |
| API / UI server (gross) | 1,581 | 2,349 | 1,801 | 5,731 |

**Correction (2026-08-06):** that API row was wrong. UTA's `api_trigger/service.py`
(1,581) has **zero routes** — it is business logic, not an HTTP layer. Measured
properly:

| Real HTTP layer | UTA | cragent | spec_generator_agent |
|---|---|---|---|
| lines | **329** (`routes.py` + `app.py`) | ~1,632 routing of 2,349 (717 are HTML helpers) | 1,801, mostly knowledge-graph UI |

The genuinely shared surface is ~15–18 routes, not 5,700 lines. Task 1d was
still worth doing — dev-flow-agent needs exactly that surface and would
otherwise have become copy #4 — but the payoff is **stopping the fourth copy**,
not deleting thousands of lines.

## Task sequence

| # | Task | Outcome | Depends on |
|---|---|---|---|
| **1** | ~~Extract OpenCode harness into `../agent-core`~~ **DONE 2026-08-06** | Standalone repo; 9 modules ported, 8 harness test files green (243 tests); config injected via proxy. Sibling repos untouched. Deterministic and live parity both pass. [spec](spec-agent-core-harness-extraction.md) · [design](design-agent-core-harness-extraction.md) · [plan](plan-agent-core-harness-extraction.md) | — |
| **1b** | ~~Generic runtime layer in `agent-core`~~ **DONE 2026-08-06** | `agent_core.runtime`: `ac_task_events`, `ac_task_controls`, `ac_runner_heartbeats`, `ac_human_gates` + `RuntimeStore`. A canonical schema products **adopt per capability**, not a generalisation of what they have — measurement showed the three forks disagree on FK type, payload column, severity, and control structure. [ADR-004](decisions/ADR-004-generic-runtime-layer.md) | 1, 2 |
| **2** | ~~Workflow engine decision + durable-suspend spike~~ **DONE 2026-08-06** | Proven cross-process suspend/resume, with real OpenCode turns either side of the gate. LangGraph 1.2.10 on Python 3.13. [ADR-003](decisions/ADR-003-workflow-engine-and-durable-gates.md) · [spike](../spikes/durable_gate/) | 1 |
| **3** | ~~Identity and authz in `agent-core`~~ **DONE 2026-08-06** | `agent_core.identity`: corp-SSO proxy header for humans, service tokens for machines, `assert_trusted_deployment()` guard, staged enforcement (gates + mutations now, report reads later). Gate answers are authorized and attributed. | 1b |
| **4** | ~~Event streaming (SSE)~~ **DONE 2026-08-06** | `stream_task_events()` yields frames (framework-agnostic), `HarnessEventBridge` forwards the harness's parsed tool calls into the log. Resumption via `Last-Event-ID` → `after_id`. | 1b |
| **5** | Human gates + approval inbox — **bridge DONE 2026-08-06** | `agent_core.gates.langgraph`: `human_gate()` node helper, `resume_answered_gates()` driver, deterministic gate ids surviving node re-execution. Timeout policy decided: **wait indefinitely**. Remaining: the inbox **UI**. | 2, 3, 4 |
| **6** | ~~Manual task creation with image input~~ **DONE 2026-08-06** | Assumption validated live first: provider reads `image_url` data URIs, and opencode `-f/--file` passes an image through to the model (a blue PNG was correctly described as "Blue"). `AttachmentStore` + harness deviation D6. | 1 |
| 7 | dev-flow-agent workflow proper | The node graph, prompts, and gate policy. Should be mostly nodes and prompts if 1–6 landed well. | 2–6 |
| 8 | Migrate cragent onto `agent-core` | Proves the core against a real second consumer. cragent is the smaller, more recent fork — the easier proof. | 7 |
| 9 | Migrate UTA onto `agent-core` | Deferred until the core has proven itself twice. UTA is the source, so its migration is the one that retires the original copy. | 8 |
| **10** | ~~Migrate `spec_generator_agent`~~ **RE-SCOPED 2026-08-07** | Assessment found spec_generator_agent **wrote its own harness** rather than forking: 36 of ~45 public symbols have no counterpart, and there is no lineage note. This is a rewrite, not a migration. Recommended instead: absorb its three genuinely missing capabilities (session affinity, graceful shutdown, a process Protocol with a test fake) and leave spec_generator_agent alone. See [assessment](corbell-harness-assessment.md). | — |

### Correction from scope discovery (2026-08-06)

This roadmap originally folded the task DB into Task 1. Scope discovery
disproved that assumption and Task 1 narrowed to the harness alone:

- `uta/opencode/` depends on exactly one symbol outside itself
  (`uta.config.settings`, 39 attributes) and nothing from `tasks`, `engine`,
  `language`, or `graph`. It is a genuine seam.
- The task DB is **not** liftable. UTA's tables are `repo_branches` /
  `repo_tasks` / `class_tasks`; cragent's are `cr_tasks` / `reviewer_plans` /
  `reviewer_runs` / `findings`. Neither repo has a generic `tasks` table, and
  `uta/tasks/manager.py` (2,315 LOC) is a domain orchestrator coupled to
  `engine.coverage_recompute` and language adapters. What generalizes is the
  parent-job → child-work-unit *pattern* plus three identical tables — that is
  a schema design job (Task 1b), not an extraction.

### Sequencing

**No fallback** (decided 2026-08-06). The vendored-copy escape hatch that this
roadmap originally proposed is withdrawn: the whole point of Task 1 is to stop
the third copy from existing, and a fallback that creates one defeats it. If
the harness resists extraction, we fix the coupling.

Ordering guidance stands. `uta/opencode/client.py` is 1,156 LOC and already
carries two deferred imports (`client.py:521,527`) — the usual hiding place for
coupling the import graph does not reveal. **Port `client.py` and `process.py`
first, not last**, so that anything unpleasant surfaces on day one rather than
after the easy modules have created sunk cost.

### Alternative ordering considered

Risk-first would put Task 2 (the durable-suspend spike) before Task 1, on the
grounds that suspend/resume is the genuinely novel part while extraction is
grunt work of known shape. This roadmap puts extraction first because the spike
needs a harness to hold in the loop to be meaningful, and because the
three-copies problem gets worse with every week of delay.

## Conventions

Follows the sibling repos: `docs/spec-*.md`, `docs/design-*.md`,
`docs/plan-*.md`, ADRs in `docs/decisions/ADR-NNN-*.md`.

This is platform/tooling work, not a business feature — no Jira or release approval
release approval is required for spec/design phases.
