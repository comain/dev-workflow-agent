# ADR-004: agent-core ships a canonical runtime schema for adoption, not a unification of the existing three

## Status

Accepted, 2026-08-06. Implements Task 1b.

## Context

Task 1 established that the task DB does not lift: each product's task table is
domain-shaped. The roadmap then claimed three tables were "genuinely identical
in both repos" and could be generalised directly. With a fourth consumer now in
scope (`spec_generator_agent`), that claim was measured across all
three existing products.

**It does not hold.**

### Events — near-converged

| | UTA | cragent | spec_generator_agent |
|---|---|---|---|
| FK | `repo_task_id` + `class_task_id` | `task_id TEXT` | `task_id INTEGER` |
| payload | `payload_json` | `payload_json` | `metadata_json` |
| severity | default `'INFO'` | default `'info'` | **absent** |
| stage | nullable | nullable | `NOT NULL` |
| extra | `ts` | — | — |

Same idea, three incompatible spellings. The FK type alone (TEXT vs INTEGER vs
a dual parent/child pair) rules out one canonical foreign key.

### Controls — structurally different

- UTA and cragent: an **append-only log** (autoincrement id, `action`,
  `acknowledged_at`).
- spec_generator_agent: `task_id` is the **PRIMARY KEY** — a single command slot per task,
  latest wins, with no history at all.

These are not variations on a theme; they answer different questions. A log can
tell you a stop was requested twice and acknowledged once. A slot cannot.

### Heartbeats — not universal

`runner_heartbeats` exists in UTA and cragent. spec_generator_agent has none.

## Decision

**agent-core ships a canonical runtime schema that products adopt, rather than a
generalisation that accommodates what the three already have.**

Concretely:

1. Core tables are namespaced `ac_*` (`ac_task_events`, `ac_task_controls`,
   `ac_runner_heartbeats`, `ac_human_gates`) so they can live in the same
   SQLite file as a product's existing tables without collision.
2. The core owns **no** task table. It refers to work by an opaque
   `task_ref TEXT` — the product's own identifier, stringified. No foreign key
   into product tables, because there is no single shape to point at.
3. Products adopt **per capability**, not big-bang. A product can take
   `ac_human_gates` and the SSE event stream while keeping its legacy
   `task_control` untouched. Migration off the legacy tables is optional and
   can be indefinite.
4. `dev-flow-agent` is greenfield and uses the canonical layer exclusively,
   which makes it the proving ground before any migration is attempted.
5. The core does **not** persist workflow state. Task 2 established that
   LangGraph owns that in its own `checkpoints`/`writes` tables. The runtime
   layer stores the *gate record* — what was asked, of whom, what came back —
   and queue state.

## Consequences

**Positive**

- No product is forced into a migration it did not ask for, and none of the
  three has to change to unblock dev-flow-agent.
- The opaque `task_ref` sidesteps the TEXT/INTEGER/dual-FK divergence entirely
  instead of trying to reconcile it.
- Per-capability adoption gives each migration an independently revertible
  step, rather than one large schema change per product.
- Being greenfield-first means the schema is proven by a real consumer before
  anyone migrates onto it.

**Negative**

- **Duplication during the transition.** A migrating product will have both
  `task_events` and `ac_task_events` for a period. That is deliberate — the
  alternative is a synchronised four-repo change — but it is real cost and the
  legacy tables must eventually be retired or the duplication becomes
  permanent.
- No referential integrity between `ac_*` tables and product task tables. An
  orphaned `task_ref` is possible. Accepted: a foreign key is exactly what
  cannot be expressed across three incompatible key types.
- Four consumers will drift in *how much* they have adopted, so "does this
  product use the core runtime?" becomes a per-capability question rather than
  a yes/no.

## Alternatives rejected

- **One unified schema all four migrate to.** The honest reading of the
  measurements is that this requires four coordinated migrations to deliver the
  first line of new value. Rejected: it front-loads the entire cost before any
  benefit, and spec_generator_agent's control table would lose information it currently has
  no history for.
- **Configurable table and column names**, so the core adapts to each product's
  existing spelling. Rejected as over-engineering: it makes every query
  indirect and encodes three historical accidents as permanent API surface.
- **Leave the runtime layer in each product** and share only the harness.
  Rejected: human gates, the approval inbox, and identity are new
  cross-cutting capabilities. Building them four times is exactly the problem
  this program exists to stop.

## Follow-ups

- Retirement plan for legacy tables belongs to the individual migration tasks
  (8, 9, 10), not here.
- Gate timeout policy (auto-reject / escalate / wait indefinitely) is Task 5.
- Identity for gate attribution is Task 3; `ac_human_gates` carries the columns
  now but nothing enforces them until then.
