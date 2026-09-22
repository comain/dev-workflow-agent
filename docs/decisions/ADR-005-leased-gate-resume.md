# ADR-005: Gate resume is a leased `invoke_workflow` disposition

## Status

Accepted, 2026-09-09. Implements Task 7 v1 of the roadmap and spec
`spec-dev-workflow-v1.md` F2 / F3.

## Context

`agent_core.gates.langgraph` already suspends a graph with `interrupt()` and
records the wait in `ac_human_gates`. The only resume driver is
`resume_answered_gates(graph, store)`, which a daemon is expected to call as a
maintenance tick: it `mark_resumed`s, then `graph.invoke(Command(resume=...))`
with no task lease.

That is the wrong execution model for a product daemon:

- The continuation after a gate is real work (another paid agent turn). It
  must hold a lease and heartbeat like any other execute.
- `invoke_workflow` is the product-facing start/resume API and currently has
  four dispositions. A pending interrupt looks like case 2 (`snapshot.next`
  set) and would `invoke(None)`, which re-enters the gated node *without* the
  human's answer.
- UTA, CR, and spec-gen do not call `human_gate`. Changing this path does not
  migrate them.

## Decision

1. **`invoke_workflow` gains a fifth disposition, `suspended`.** After
   `invoke`, if the snapshot has interrupts, the result is `suspended`, not
   `started`/`resumed`. If the snapshot already has interrupts and the caller
   did not pass `resume_value`, return `suspended` *without invoking*.
2. **Resume of an answered gate is `invoke_workflow(..., resume_value=response)`**,
   which issues `Command(resume=response)`. Products do not build LangGraph
   config or call `Command` themselves.
3. **Answering a gate does not run the graph.** `store.answer_gate` stays a
   store write. The product `claim` takes the task under an exclusive lease,
   then `execute` calls `invoke_workflow(..., resume_value=response)` while
   that lease is held. The resume token is **“snapshot is interrupted AND
   the gate for that node has a `response`”**, not `resumed_at IS NULL`.
4. **`mark_resumed` is telemetry after a successful invoke**, not a lock.
   Calling it before `Command(resume=)` strands the run if the process dies
   mid-resume: the snapshot is still interrupted, `answered_gates_awaiting_resume`
   is empty, and the next `invoke_workflow` returns `suspended` forever.
   The task lease is the concurrency control. `resume_answered_gates` may
   still `mark_resumed` first — it is a test helper, not the product driver.
5. **`resume_answered_gates` remains a test/spike helper.** It is not the
   product daemon driver. Docstring says so. No maintenance hook in
   `TaskDaemon` is added.
6. **After every `invoke`, classify via `get_state`, not the invoke return
   dict.** `is_interrupted` reads `snapshot.tasks[].interrupts` /
   `values["__interrupt__"]`. A pending crash-resume fixture has `next` and
   no interrupts and still `invoke(None)`.

This is an **additive** agent-core `0.8.x` change. Existing graphs never
`interrupt()`, so they never observe `suspended`.

## Alternatives considered

### Keep `resume_answered_gates` as the daemon driver
Rejected: it runs continuation work without a lease. Two daemons ticking
would rely only on `mark_resumed`; they would not renew heartbeats or appear
in `ac_runner_heartbeats` as running the task. Stall detection would lie.

### `invoke(None)` on an interrupted snapshot
Rejected: measured LangGraph 1.x behaviour is that `invoke(None)` re-executes
the gated node and hits `interrupt()` again without the answer. The human's
response would be ignored and the inbox would look unchanged.

### Hand-roll a product resume API in `dev-flow-agent`
Rejected: the provider owns interrupt classification. A consumer-side
re-derivation is the wrong-module fix the design principles forbid, and the
next consumer would copy it.

## Consequences

- `WorkflowInvocationResult.disposition` is
  `"started" | "resumed" | "reused_completed" | "suspended"`.
- Product `execute` maps `suspended` → `TaskOutcome.SUSPENDED` and never
  treats it as success or failure. `execute` never returns `None`
  (`TaskDaemon` maps that to `COMPLETED`).
- Tests that assumed `invoke_workflow` of a gated graph returns `started`
  with a finished-looking state must classify `suspended` instead. There are
  no such product tests today; only the new suite will cover it.
- A test pins that `invoke(None)` on the interrupted `test_gates_langgraph`
  snapshot does **not** apply a human answer (the rejected path).
