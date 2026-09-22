# Spec: agent-core — OpenCode Harness Extraction (Task 1)

## Status

**Approved for design on 2026-08-06**, after resolving the config shape, the
timebox/fallback question, and the port-defect rule.

Implements Task 1 of [`docs/roadmap.md`](roadmap.md).

### Decisions taken at approval

1. **`agent-core` is a separate repository** at `../agent-core`, not a package
   inside dev-flow-agent.
2. **Task 1 is harness-only.** The task/runtime layer becomes Task 1b.
3. **Config is a single flat `HarnessConfig`**, not grouped into
   `ProviderConfig` / `ProcessConfig` / `ServerConfig`. Grouping reads better,
   but flat keeps the diff against UTA's existing call sites minimal, which
   directly lowers the cost of the Task 8 and Task 9 migrations. Revisit only
   if the flat object proves unwieldy in Task 1b.
4. **No fallback path.** The vendored-copy escape hatch is withdrawn. The
   extraction is committed to; if `client.py` resists, we fix the coupling
   rather than retreating to a third copy.
5. **A ported test that only passes after a behaviour change is a port defect**,
   strictly enforced — see Boundaries. This holds even when the failure looks
   like a pre-existing UTA bug: such a bug is reported, and fixed in UTA
   separately, never silently corrected during the port.

## Non-Jira Decision

This is agent-platform/tooling work, not a business feature. No Jira or
Release approval is required for the spec phase, following the precedent
set in `cragent/docs/spec-retrospective-feedback.md`.

Artifacts use the sibling-repo `docs/` convention:

- Spec: `dev-flow-agent/docs/spec-agent-core-harness-extraction.md` (this file)
- Design: `dev-flow-agent/docs/design-agent-core-harness-extraction.md`
- ADRs: `dev-flow-agent/docs/decisions/ADR-NNN-*.md`
- Usage: `agent-core/README.md`, created with the new repo

## Objective

Extract the OpenCode harness from `unit-test-agent` into a standalone,
installable `agent-core` package living in its own repository at
`../agent-core`, so that `dev-flow-agent`, `cragent`, and UTA can eventually
share one harness implementation instead of three divergent copies.

**Task 1 delivers the package and proves it against UTA's own test suite. It
does not migrate any consumer.** UTA and cragent are not modified.

### Why this is first

`cragent` is already a fork-by-copy of UTA — see
`cragent/src/cr_agent/review_v2/daemon.py:1` ("adapted from UTA
`uta/tasks/scheduler.py`") and `.../opencode_process.py:1` ("adapted from UTA
`uta/opencode/process.py`"). `dev-flow-agent` would be the third copy. The
harness is the largest and most bug-prone shared surface, and it is the one
piece that turns out to have a clean seam.

## Target Users

- `dev-flow-agent` (first consumer, Task 7)
- `cragent` maintainers (migration, Task 8)
- UTA maintainers (migration, Task 9)

Not an external/public package. Internal consumers only.

## Scope Discovery

Performed 2026-08-06 against `unit-test-agent` at the current working tree.

### Method

Enumerated every intra-repo import in `uta/opencode/*.py` and `uta/tasks/*.py`,
then enumerated every `settings.*` attribute referenced by each package, to find
where the module graph actually cuts.

### Candidate modules found

| Module | LOC | Imports outside own package | Decision |
|---|---|---|---|
| `uta/opencode/client.py` | 1156 | `uta.config.settings` | **In scope** |
| `uta/opencode/process.py` | 788 | `uta.config.settings` | **In scope** |
| `uta/opencode/tiered_router.py` | 412 | `uta.config.settings` | **In scope** |
| `uta/opencode/config.py` | 360 | `uta.config.settings` | **In scope** |
| `uta/opencode/server.py` | 195 | `uta.config.settings` | **In scope** |
| `uta/opencode/rate_limit.py` | 187 | none | **In scope** |
| `uta/opencode/stream.py` | 165 | none | **In scope** |
| `uta/opencode/fallback.py` | 102 | none | **In scope** |
| `uta/opencode/net.py` | 8 | none | **In scope** |
| `uta/tasks/db.py` | 933 | `uta.tasks.models` | Out of scope |
| `uta/tasks/manager.py` | 2315 | `uta.engine.coverage_recompute`, `uta.tasks.targets` | Out of scope |
| `uta/tasks/targets.py` | — | `uta.engine.languages` | Out of scope |
| `uta/tasks/scheduler.py` | — | `uta.tasks.db`, `uta.tasks.manager` | Out of scope |
| `uta/tasks/render.py` | 427 | `uta.config`, `uta.tasks.*` | Out of scope |
| `uta/tasks/autopush.py` | — | `uta.api_trigger.auto_push` | Out of scope |
| `uta/tasks/run_error_classifier.py` | — | none | Out of scope (this task) |

**Total in scope: 3,373 LOC across 9 modules.**

### Why the harness is in scope

The entire `uta/opencode/` package depends on exactly one symbol outside
itself: `uta.config.settings`. It has **zero** imports from `uta.tasks`,
`uta.engine`, `uta.language`, `uta.graph`, or `uta.api_trigger`. Four of the
nine modules have no external imports at all. This is a real architectural
seam, not one we have to carve.

### Why the task layer is out of scope

The roadmap assumed the task DB would lift with the harness. Scope discovery
disproves this:

- UTA's schema is domain-shaped: `repo_branches`, `repo_tasks`, `class_tasks`
  (`uta/tasks/db.py:147-233`). `class_tasks` models a Java-class-under-test.
- cragent's schema is differently domain-shaped: `cr_tasks`,
  `reviewer_plans`, `reviewer_runs`, `findings`
  (`cragent/src/cr_agent/review_v2/storage.py:65-140`).
- **Neither repo has a generic `tasks` table.** There is nothing to lift.
- `uta/tasks/manager.py` imports `uta.engine.coverage_recompute` and dispatches
  on `task["language"]` (`manager.py:1961`); `uta/tasks/targets.py` builds
  `TargetIdentity.java_class(...)` and calls `uta.engine.languages`
  (`targets.py:54`). These are UTA's domain orchestrators, not infrastructure.

What *does* generalize is the **pattern** — a parent job fanning out to N child
work units — plus three tables that are genuinely identical in both repos
(`task_events`, `task_control`/`task_controls`, `runner_heartbeats`) and the
lease-claim / quarantine / retry logic around them. Generalizing those requires
a schema design pass, not an extraction. **This is promoted to its own roadmap
task and removed from Task 1.**

### Domain leaks identified inside the in-scope code

Two of the 39 settings attributes used by the harness are not harness-generic
and must not be inherited as-is:

1. `index_source_dirs` (`uta/config.py:52`) — defaults to a list of
   deployment checkout roots. Those paths are specific to one environment
   and must not be embedded in a shared package.
2. `opencode_turn_log_dir` (`uta/config.py:286`) — defaults to a
   product-branded cache path.

Additionally `uta/config.py:6` sets `env_prefix="UTA_"`, which cannot be
hard-coded in a shared package.

## Core Features

### F1 — Standalone `agent-core` repository

A new git repository at `../agent-core`, pip-installable, `src/agent_core/`
layout, no dependency on UTA or cragent.

### F2 — Harness modules ported

All 9 `uta/opencode/` modules present as `agent_core.harness.*`, behaviour
unchanged: process spawn, server attach/spawn, event-stream parsing, tiered
model routing, provider fallback, rate-limit detection, turn logging.

### F3 — Config decoupled by injection

The harness must not import a global `settings` singleton. It receives an
explicit configuration object covering the 39 attributes it actually uses.

Requirements:

- Configurable env prefix — a consumer chooses its own, such as `DFA_`.
- No deployment-specific defaults. `index_source_dirs` defaults empty;
  turn-log directory defaults to a neutral, consumer-overridable path.
- A consumer with an existing settings object can adapt it without rewriting
  its own config layer.

### F4 — Test suite ported and green

UTA's 11 harness test files (3,370 LOC — a near 1:1 test-to-code ratio) run
against `agent_core` and pass. Changes permitted to those tests are limited to
import paths and config construction. **A behavioural change required to make a
test pass is a defect in the port, not a test update.**

### F5 — Explicit public interface

`agent_core.harness.__init__` exports a documented surface. Consumers must not
reach into module internals. Anything not exported is private and may change.

## Acceptance Criteria

1. `../agent-core` exists as a git repo with `pyproject.toml`, installable via
   `pip install -e ../agent-core`.
2. `pip install -e ../agent-core && pytest` passes in agent-core with all 11
   ported harness test files, no skips added.
3. `grep -rn "uta\.\|unit_test_agent" src/agent_core/` returns nothing.
4. `grep -rn "wms/api\|bach/api\|\.uta_cache\|UTA_" src/agent_core/` returns
   nothing.
5. `git -C ../unit-test-agent status --porcelain` and the same for `cragent`
   show no modifications attributable to this task.
6. A consumer can construct the harness with an injected config and run one
   OpenCode turn against a stub, exercised by a test in agent-core that does
   not import UTA.
7. The public interface is enumerated in `agent-core/README.md`.
8. Behaviour parity is argued in the design doc: every intentional deviation
   from UTA's harness is listed with a reason, or the list is empty.

## Out of Scope

- Migrating UTA or cragent onto agent-core (Tasks 8, 9).
- Task DB / daemon / event generalization (promoted to its own task).
- Identity and authz (Task 3).
- SSE, human gates, image input (Tasks 4, 5, 6).
- Any new harness capability. This is a port, not an improvement.
- Publishing to an internal package index. Path/git-ref installs suffice.

## Tech Stack and Constraints

- Python, `>=3.9` — matches both existing repos, so a migration is not blocked
  on a runtime bump.
- `pydantic` / `pydantic-settings` for config, consistent with both consumers.
- `httpx` for the OpenCode server client, as UTA uses today.
- `pytest`.
- No LangGraph dependency. The harness is workflow-engine agnostic; the
  LangGraph decision belongs to Task 2 and must not leak into the core.

## Project Structure

```
../agent-core/
  pyproject.toml
  README.md                     # public interface, install, usage
  src/agent_core/
    __init__.py
    config.py                   # injectable harness config
    harness/
      __init__.py               # public exports
      client.py  process.py  server.py  stream.py
      config.py  tiered_router.py  fallback.py
      rate_limit.py  net.py
  tests/                        # 11 ported test files
```

## Code Style

Follow the source repo so that diffs against UTA stay readable during the port:
`from __future__ import annotations`, explicit typing on public functions,
`logging` module-level loggers, no new formatter or linter introduced in this
task.

## Testing Strategy

**Ported tests are the correctness oracle.** The port is verified by UTA's
existing tests, not by newly written ones — new tests cannot detect a
behavioural regression against code they were not written for.

1. Port all 11 files unchanged except imports and config construction.
2. Record any test that required more than that as a finding in the design doc.
3. Add exactly one new test: F3/AC6, a consumer-shaped integration test proving
   the package works with injected config and no UTA import.
4. No coverage target for this task. Ported ratio is already ~1:1.

## Boundaries

**Always:**
- Keep UTA and cragent working trees clean.
- Preserve harness behaviour exactly; port defects are bugs.
- Record every deviation in the design doc.

**Ask first:**
- Any change to harness behaviour, however small an improvement it appears.
- Adding a dependency not already used by UTA's harness.
- Changing the public interface shape away from UTA's call sites, since that
  raises the cost of Tasks 8 and 9.

**Never:**
- Modify `unit-test-agent` or `cragent` in this task.
- Carry deployment-specific paths or another product's env prefix into agent-core.
- Introduce a workflow-engine dependency into the harness.
- Weaken or skip a ported test to make the suite green.

## Risks and Mitigations

| Risk | Mitigation |
|---|---|
| `uta/opencode/client.py` (1156 LOC) hides coupling the import graph did not reveal — e.g. runtime `settings` reads or filesystem assumptions. Two deferred imports already exist at `client.py:521,527`. | Port client.py first, not last. If it resists, that is the timebox signal. |
| Config injection changes call-site ergonomics enough to make Tasks 8/9 expensive. | Design an adapter path so an existing settings object can be wrapped, not replaced. Validate against cragent's `Settings` before freezing. |
| Ported tests depend on UTA fixtures in `tests/conftest.py`. | Audit conftest during design; port or stub the harness-relevant parts. |
| Divergence: UTA's harness keeps moving while agent-core is built. | Task is timeboxed; record the source commit SHA in the design doc so drift is measurable at migration time. |

## Open Questions for Design

1. ~~Config shape~~ — **resolved at approval: flat `HarnessConfig`.**
2. ~~Timebox and fallback~~ — **resolved at approval: no fallback.** Ordering
   guidance stands regardless: port `client.py` and `process.py` first, because
   that is where hidden coupling will surface, and surfacing it early is worth
   more than an easy start.
3. Does `uta/tasks/run_error_classifier.py` (no external imports) belong to the
   harness rather than the task layer? It classifies run errors, which is
   arguably harness-adjacent. Deferred to Task 1b, not blocking.
4. `tests/conftest.py` audit — which fixtures the 11 harness test files
   actually need, and whether they port or stub. To be answered in design.
