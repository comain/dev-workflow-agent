# spec_generator_agent harness assessment

Done 2026-08-07, before scheduling Task 10.

## Finding: spec_generator_agent did not fork the harness, it wrote its own

The roadmap's premise was "four copies of the same harness, stop the fourth".
That premise **does not hold for spec_generator_agent**. A symbol-level diff of
`spec_generator_agent/core/opencode/` against `agent_core.harness` shows **36 of
its ~45 public symbols have no counterpart**, and there is no lineage note in
its source — unlike cragent, whose docstrings say "adapted from UTA".

The abstractions differ at the top level, not in details:

| Concept | agent-core (from UTA) | spec_generator_agent |
|---|---|---|
| result type | `TurnResult` | `OpenCodeResult`, `ProcessResult` |
| entry point | `run_turn()` | `run_json()`, `run_json_in_session()` |
| process | one concrete class | `OpenCodeProcess` **Protocol** with `OpenCodeCliProcess`, `OpenCodeFallbackProcess`, `FakeOpenCodeProcess` |
| fallback reason | strings | typed `FallbackReason` / `FallbackClassification` |
| cancellation | `is_cancelled` predicate (D7) | `bind_cancel_check`, `OpenCodeCancelled` exception |

## Three capabilities spec_generator_agent has that agent-core lacks entirely

Worth absorbing regardless of whether spec_generator_agent ever migrates:

1. **Session affinity** — `has_affinity_session`, `affinity_turn_count`,
   `reset_affinity_session`, `affinity_session_id`, `run_affinity`. Reusing one
   OpenCode session across turns preserves model context between related
   requests. agent-core passes `session_id` through but has no notion of
   managing affinity.
2. **Graceful shutdown** — `install_opencode_shutdown_handlers`,
   `terminate_active_opencode_processes`, `opencode_shutdown_requested`.
   agent-core terminates a process per turn but has nothing that reaps every
   live child on SIGTERM. A deploy that restarts the service currently orphans
   in-flight OpenCode processes.
3. **A process Protocol with a test fake** — `FakeOpenCodeProcess` lets callers
   test workflows without spawning anything. agent-core's tests stub
   `subprocess.Popen`, which is coarser and re-implemented per consumer.

## Consequence for the roadmap

**Task 10 is not a migration, it is a rewrite**, and should be re-scoped or
dropped. The "stop the fourth copy" rationale does not apply: there is no fourth
copy, there is a second independent implementation.

Options, in order of preference:

1. **Absorb the three capabilities above into agent-core; leave spec_generator_agent alone.**
   Cheapest, and the capabilities benefit dev-flow-agent and cragent
   immediately — graceful shutdown in particular is a real gap for anything
   running as a service.
2. **Migrate spec_generator_agent later, deliberately**, accepting it is a rewrite of ~1,450
   LOC with its own test suite to retarget. Only worth it if maintaining two
   harnesses proves expensive in practice.
3. **Leave spec_generator_agent out of the program entirely** and record it as a known second
   implementation.

Recommended: option 1 now, decide between 2 and 3 once dev-flow-agent and
cragent have run on the shared harness for a while.
