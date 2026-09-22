# ADR-003: LangGraph 1.x on Python 3.13 for durable human gates

## Status

Accepted, 2026-08-06. Implements Task 2 of the roadmap.

Evidence: the executed results are preserved below. The exploratory spike code
was retired after `tests/test_durable_product_gate.py` superseded it with a
maintained product-graph proof.

## Context

The roadmap's load-bearing problem is that a `design-review` gate may wait hours
or overnight. Holding a worker and a task lease for that duration breaks the
execution model: the slot is pinned, stall detection fires, and a deploy or
crash loses the run. The required shape is **suspend, don't block** — persist,
release the worker, resume later in a possibly different process.

The roadmap proposed LangGraph's `interrupt()` / `Command(resume=...)` and
flagged an open risk: interrupt semantics moved between 0.2 and 1.x, and both
existing repos pin `langgraph>=0.2,<1.0`.

## Findings

### 1. LangGraph 1.x requires Python >= 3.10 — a hard fork in the road

`pip` refuses every 1.x release on Python 3.9:

```
ERROR: Ignored the following versions that require a different python version:
  1.0.0 Requires-Python >=3.10 ... 1.2.10 Requires-Python >=3.10
```

The ceiling on Python 3.9 is **0.6.11**, while upstream is at **1.2.10**. UTA
and cragent both declare `requires-python >= 3.9` and run on 3.9.6, so
staying aligned with them means adopting a LangGraph line that is several
major-minor generations behind.

Python **3.13.3** is already present on the machine via `uv`, and cragent's
own venv uses it.

### 2. `agent-core` imports cleanly on 3.13

The extracted harness is pure Python with `requires-python >= 3.9`, so it
imports and exports all 39 symbols under 3.13. **A consumer may run a newer
interpreter than the shared package targets.** This is what makes the version
split safe: dev-flow-agent can move to 3.13 without dragging agent-core,
cragent, or UTA with it.

### 3. Cross-process suspend and resume works — proven

The original spike ran a three-node graph (`design` → `design_review` →
`build`) against a `SqliteSaver`. Process A hit the gate and **exited**;
process B resumed from the database alone:

```
"worker_trace": ["design@pid:98927", "design_review@pid:98931", "build@pid:98931"]
```

The process ids straddle the gate. The worker was genuinely released.

### 4. It holds with the OpenCode harness in the loop

The roadmap demanded the proof survive a real harness. `harness_flow.py` runs a
**real OpenCode turn** through `agent-core`, gates, and runs a **second real
turn** after resumption:

- pid 4848 — produced a design proposal (146 chars), then exited at the gate
- pid 4935 — resumed, received the reviewer comment *"must survive a worker
  restart"*, and generated a revision that visibly addressed it

The post-gate turn can therefore see and act on what the human supplied. That is
the entire feature.

### 5. The serialization boundary is enforced by the framework, not by discipline

The design asserted that a gate must be its own node and that no live subprocess
handle may cross it. That is now proven, and the failure mode is better than
assumed — **loud, not silent**:

```
TypeError: Type is not msgpack serializable: Popen
```

Consequence for node authors: a `TurnResult` must **not** enter graph state.
Extract the text at the boundary and store that. The spike does this in `_turn`.

### 6. A daemon can detect "waiting on a human" without running the graph

```python
snapshot = graph.get_state(config)
snapshot.next            # ("design_review",) when gated, () when complete
snapshot.tasks[0].interrupts  # the gate payload, for rendering in an inbox
```

This is what the queue integration needs: cheap polling, no graph execution, and
the gate payload available for an approval inbox.

### 7. The checkpointer owns only two tables

`checkpoints` and `writes`. **Workflow state is LangGraph's problem, not ours.**
This materially narrows Task 1b: the generic runtime schema needs the *gate*
record (who, what, when, response, authorisation) and task-queue state — it does
not need to persist workflow state, which an earlier reading of the roadmap
assumed.

### 8. Gates carry structured input, not just approval

`interrupt()` accepts an arbitrary payload and `Command(resume=...)` returns an
arbitrary value into the node. So the roadmap's ACP distinction maps directly:
`approve` (`session/request_permission`) and `input` (`elicitation/create`, with
a response schema) are the *same* mechanism with different payload shapes.
`design-review` is an `input` gate and works as one.

## Decision

1. **dev-flow-agent targets Python >= 3.10 and runs on 3.13**, and depends on
   **LangGraph 1.x** (currently 1.2.10).
2. **agent-core stays `requires-python >= 3.9`** so cragent and UTA can adopt
   it without an interpreter bump. The consumer may be newer than the library.
3. **Human gates use `interrupt()` / `Command(resume=...)`** with a persistent
   checkpointer. We do not hand-roll suspend/resume.
4. **A gate is its own node**, sited between OpenCode turns. Only serializable
   values enter graph state; harness results are reduced to text at the node
   boundary.
5. **Gate payloads are structured**, carrying `kind` (`approve` | `input`) and,
   for input gates, a response schema.

## Consequences

**Positive**

- The load-bearing risk in the roadmap is retired with executed evidence rather
  than argument.
- Current LangGraph, with documented interrupt semantics, instead of a version
  line that is generations stale.
- Task 1b's schema shrinks: no workflow-state persistence needed.
- The framework enforces the serialization constraint, so a node author cannot
  quietly smuggle a subprocess handle past a gate.

**Negative**

- **dev-flow-agent's runtime diverges from its siblings** (3.13 vs 3.9). Shared
  code must therefore live in agent-core, which stays 3.9-compatible, and CI
  must test agent-core on both interpreters. Divergence is a cost paid to avoid
  a worse one — adopting a stale engine for the feature the product exists for.
- A second checkpointer database sits alongside the task database. Two stores,
  two backup and retention concerns. Consolidating them is possible later
  (LangGraph supports a Postgres saver) but not now.
- If cragent or UTA ever want to run dev-flow graphs in-process, they will
  need the interpreter bump too.

## Alternatives rejected

- **LangGraph 0.6.11 on Python 3.9.** Keeps all three products on one
  interpreter. Rejected: it locks the feature that justifies the whole project
  onto a line that upstream has moved several generations past, and the
  interrupt semantics we would build on are the older ones. The alignment it
  buys is with repos that are not going to run these graphs anyway.
- **Hand-rolled suspend/resume** over our own tables. Rejected: it reimplements
  checkpointing, replay, and interrupt resumption — and finding 5 shows the
  framework catches a class of bug (unserializable state crossing a gate) that
  a hand-rolled version would let through silently.
- **Blocking the worker for the duration of the gate.** Rejected in the roadmap
  and confirmed unnecessary here.

## Follow-ups

1. **`HarnessConfig.model_dump()` is lossy for round-tripping.** Secret fields
   redact to `"***"`, so `HarnessConfig(**cfg.model_dump())` silently produces a
   config whose provider token is the literal `"***"`, surfacing later as a
   confusing auth failure. Hit during this spike. Needs a decision in agent-core:
   keep redaction on `model_dump` and document the hazard, or move redaction to
   an explicit `redacted_dump()` and keep `model_dump` lossless.
2. **`claude-haiku-4-5-20251001` stalls through opencode** (`OpenCodeNoOutputStall`)
   while responding normally over plain HTTP; `gpt-5.5` works. Both the source
   and ported harnesses stall identically, so it is not a port defect, but model
   selection for dev-flow needs care.
3. Gate timeout policy — auto-reject, escalate, or wait indefinitely — is
   undecided and belongs to Task 5.
