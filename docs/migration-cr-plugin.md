# cragent migration to agent-core

Status as of 2026-08-07. Branch: `cragent/agent-core-migration`.

Sequenced ahead of dev-flow-agent's own workflow so the shared layer would be
tested by a real product rather than by its own tests. That decision paid for
itself immediately — see "What this found" below.

## Done

| module | before | after | result |
|---|---|---|---|
| `opencode_stream.py` | 168 | **14** (re-export) | suite green |
| `opencode_routing.py` | 177 | **35** (re-export) | suite green |

cragent test suite: **233 passed, 1 failed** — identical to the pre-migration
baseline. The one failure shells out to `ripgrep`, absent from this environment,
and predates the work.

## Not done, and why

`opencode_process.py` (567) and `opencode_config.py` (317) were attempted and
**rolled back**. The adapter itself was small — 66 and 77 lines — but it
produced 16 test failures resolving to two genuine divergences that need a
design decision rather than an adapter:

### 1. `opencode.json` permission model differs

agent-core emits `permission: {external_directory: {...}}`. cragent emits a
richer block including `edit` and `*` keys. This is **runtime behaviour** —
what the model is permitted to do inside the repo — not a test artifact.
Symptoms: `KeyError: 'edit'`, `KeyError: '*'`.

Deciding this properly means answering whether the permission block belongs in
the shared package at all, or whether config generation should be a product
concern with agent-core supplying only the provider/model registration.

### 2. Model selection semantics differ

For the same chain, agent-core selects `token-pool/claude-fable-5` where
cragent selects `token-pool/gpt-5.5`. agent-core takes the first healthy chain
candidate; cragent applies its own preference. Neither is obviously right, and
choosing changes which model reviews production code.

## What this found

The migration was worth doing for what it exposed, independent of how far it
got. Every item below is now in agent-core:

| Deviation | Capability the fork had and upstream lacked |
|---|---|
| D7 | `is_cancelled` — cooperative cancellation, wired to the stop/cancel control channel. Without it a migrated product silently loses the ability to stop a running review. |
| D8 | `TurnResult.model_id`, `.cost_usd` |
| D9 | `extract_session_id`, `extract_cost`, `is_model_healthy`, `reset_model_health`, and non-dict rejection in `parse_line` |
| D10 | prompt file as a first-class input |
| D11 | config build/write split with a per-turn model override |
| — | provider fallback orchestration (`run_turn_with_fallback`) — upstream sets `fallback_eligible` but nothing acts on it |
| — | per-turn workspace isolation (`per_turn_workspace`) — fixes a live concurrency bug where concurrent turns overwrite one another's `opencode.json` |

It also exposed a real bug in agent-core's own `propagate()`: it captured a
`contextvars.Context` and reused it, which raises when one wrapped callable is
used across a thread pool — precisely what it exists for. Earlier tests only
invoked it once.

**The premise correction:** the Task 1 spec recorded cragent's harness as a
"trimmed adaptation" of UTA's. That is true by module count and LOC, and false
by capability. UTA has more *surface* (server mode, auth client, rate-limit log
scanning); cragent has more *depth* on the surface it kept. Extracting from
UTA alone was the wrong reference for anything cragent needs.

## Next

1. Decide the permission-model question (blocker 1) — likely an ADR.
2. Decide model-selection semantics (blocker 2).
3. Re-attempt `opencode_process.py` and `opencode_config.py`; the adapter code
   is small once those two are settled.
