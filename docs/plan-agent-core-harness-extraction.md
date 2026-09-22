# Plan: agent-core — OpenCode Harness Extraction (Task 1)

## Status

Ready for review, 2026-08-06.

Derived from the approved [design](design-agent-core-harness-extraction.md) and
[spec](spec-agent-core-harness-extraction.md). Non-Jira platform work: no Jira
key, no Release approval, no Java coding-guidelines task (Python repo).

Target repo to create: `agent-core`
Source: `unit-test-agent/uta/opencode/` (9 modules, 3,373 LOC)
plus 11 test files (3,370 LOC).

## Dependency graph

Derived from the actual import edges, not assumed:

```
net ──────────┐
fallback      │        (no external imports — leaves)
rate_limit ───┤
stream ───────┤
              ├──> tiered_router ──> process ──> client
              │         │
              │         └──────────> config
              └──────────────────────> server
```

- `net`, `fallback`, `rate_limit`, `stream` — no intra-package imports (462 LOC total)
- `tiered_router` — settings only
- `process` — settings, `tiered_router`, `rate_limit`, `stream`
- `client` — settings, `net`, `process`, `rate_limit`
- `config` — settings, `tiered_router`
- `server` — settings, `net`

### Reconciling "port client and process early" with the dependency order

The design mandates porting `client.py` and `process.py` early, because that is
where hidden coupling lives. Strictly first is impossible — `process` needs
`tiered_router`, `rate_limit`, and `stream`; `client` needs `process` and `net`.

**Resolution:** the leaves plus `tiered_router` are 874 LOC of low-risk code
that unblocks both. They are Phase 1–2; `process` and `client` are Phase 2,
landing before `config.py` and `server.py` and before meaningful sunk cost.
"Early" means *before the easy remainder*, not literally first.

## Test-to-module mapping

Establishes which tests gate which task:

| Module | Gating tests |
|---|---|
| `stream` | `test_opencode_stream.py` |
| `tiered_router` | `test_parse_providers.py`, `test_tiered_routing.py`, `test_tiered_routing_resilience.py` |
| `process` | `test_opencode_process.py` |
| `client` | `test_opencode_client.py`, `test_opencode_client_streaming.py` |
| `config` | `test_opencode_config.py`, `test_context_providers.py` |
| `server` | `test_opencode_server.py`, `test_opencode_server_removal.py` |
| `net`, `fallback`, `rate_limit` | no dedicated file; exercised transitively |

`net`, `fallback`, and `rate_limit` having no dedicated tests is worth noting —
`rate_limit` carries the D1 rename, so its correctness rides entirely on
transitive coverage through `client` and `process`.

---

## Phase 0 — Foundation

### T1 · Create repo and record baselines

Scaffold `agent-core`: `git init`, `pyproject.toml`
(`name=agent-core`, `requires-python>=3.9`, deps `pydantic`,
`pydantic-settings`, `httpx`; dev extra `pytest`), `src/agent_core/` layout,
`.gitignore`.

Record two baselines in `docs/` of agent-core:
- UTA source commit SHA being ported from (design requires this for drift
  measurement at Task 9).
- `git status --porcelain` of **both** UTA and cragent. Both already carry
  unrelated local edits predating this work; AC5 is meaningless without the
  pre-existing baseline captured first.

**Acceptance:** `pip install -e ../agent-core` succeeds and imports.
`docs/port-baseline.md` records both SHAs and both dirty-file lists.

### T2 · Config layer

`agent_core/config.py`: flat `HarnessConfig(BaseSettings)` with all 39 fields
the harness reads, `env_prefix="AGENT_"`; module default `settings`;
`current_config()`, `use_config()`, `propagate()`.

Deviation **D3** applied here: `index_source_dirs` defaults `""`,
`opencode_turn_log_dir` defaults `.agent_cache/opencode_turns`.

`opencode_prompt_file_threshold_chars` is **deliberately not declared** (design
decision C2). Add a comment at the field list saying so, or the next person
will "fix" it.

New tests (`test_config_scoping.py`):
- `use_config()` scopes and restores
- **`propagate()` carries scoped config across a `ThreadPoolExecutor` hop**, and
  a bare `submit()` demonstrably does not — the hazard must be pinned by a test,
  not just documented
- secrets (`openai_api_key`, `deepseek_api_key`, `tencent_api_key`,
  `gemini_api_key`, `openrouter_api_key`) are redacted in `repr()` and
  `model_dump()`
- pydantic-settings `_env_prefix` behaviour verified against the pinned version;
  if it does not work as expected, subclassing is the documented path

**Acceptance:** `test_config_scoping.py` green. 39 fields present. Grep confirms
`opencode_prompt_file_threshold_chars` absent from the model.

> **Checkpoint A** — installable package with a proven config layer. Nothing
> ported yet. Stop and confirm before touching harness code.

---

## Phase 1 — Leaf modules

### T3 · Port `net`, `fallback`, `rate_limit`, `stream`

Four modules, 462 LOC, zero intra-package imports.

Apply deviation **D1** in `rate_limit.py`: `uta_debug_log_dir()` →
`debug_log_dir()`, `/tmp/uta-run-logs` → `/tmp/agent-run-logs`. The only caller
is `client.py:527`, updated in T6.

Port `tests/conftest.py` (12 lines: `fixtures_dir` fixture +
`pytest_ignore_collect`). Inventory the fixture data `fixtures_dir` points at
and port what the harness tests actually consume.

Port `test_opencode_stream.py`.

**Acceptance:** `test_opencode_stream.py` green. `grep -rn "uta" ` over these
four modules returns nothing.

> **Checkpoint B** — the harness's leaf layer runs standalone.

---

## Phase 2 — Risk concentration

### T4 · Port `tiered_router`

412 LOC, 16 settings reads. Substitute `settings.X` → `current_config().X`.

Port `test_parse_providers.py`, `test_tiered_routing.py`,
`test_tiered_routing_resilience.py` — string-substitute
`"uta.config.settings.X"` → `"agent_core.config.settings.X"`.

**Acceptance:** three test files green, no behavioural change.

### T5 · Port `process` — risk task

788 LOC, 22 dot-access settings reads **plus the 3 `getattr(settings, ...)`
sites** at lines 193, 222, 358. The getattr sites need their own pass; the
dot-access substitution does not match them.

C2 non-change: port
`int(getattr(cfg, "opencode_prompt_file_threshold_chars", 0) or 60000)`
verbatim. The knob stays dead.

`_build_env` stays private; `test_opencode_process.py` imports it from the
module directly.

**Acceptance:** `test_opencode_process.py` green.
`grep -n "getattr(current_config()" process.py` shows exactly 3 sites.

### T6 · Port `client` — risk task

1,156 LOC, 17 settings reads. **The two deferred imports at `client.py:521,527`
are the specific thing to watch** — they are where coupling the import graph
did not reveal would live. `:527` is the `uta_debug_log_dir` call, updated for
D1.

Port `test_opencode_client.py`, `test_opencode_client_streaming.py`.

**Acceptance:** both files green. Any coupling found that is not resolvable by
substitution is escalated, not worked around — there is no fallback path
(spec decision 4).

> **Checkpoint C** — the risk is retired. If the port was going to fail, it has
> failed by here. Confirm before continuing.

---

## Phase 3 — Remainder

### T7 · Port `config`

360 LOC, 24 settings reads — the densest config consumer.

Deviation **D2**: `PROJECT_ROOT = Path(__file__).resolve().parents[2]` must not
survive as a depth-derived expression. Under `src/agent_core/harness/config.py`
that resolves to `src/`, not a repo root. Resolve explicitly; do not export.

Deviation **D3** consumed here (`config.py:221` reads
`opencode_external_dirs or index_source_dirs`).

New test: `PROJECT_ROOT` resolves to the intended location under the new layout.

Port `test_opencode_config.py`, `test_context_providers.py`.

**Acceptance:** both green, plus the `PROJECT_ROOT` test.
`test_opencode_config.py:491` patches `index_source_dirs` explicitly, so the
default change must not require editing it.

### T8 · Port `server`

195 LOC, 17 settings reads. Port `test_opencode_server.py`,
`test_opencode_server_removal.py`.

**Acceptance:** both green.

> **Checkpoint D** — all 11 ported test files green, zero skips added.

---

## Phase 4 — Interface and proof

### T9 · Public interface and consumer integration

`agent_core/harness/__init__.py` exporting the full table from the design — 8
modules, ~40 symbols, derived from production call sites. `PROJECT_ROOT` is not
exported.

New `test_consumer_integration.py` (spec AC6): construct the harness with an
injected config, run one OpenCode turn against a stub, **import nothing from
UTA**.

**Acceptance:** every symbol in the design's table importable from
`agent_core.harness`. Integration test green.

### T10 · Dual-run parity check

Run one real OpenCode turn through UTA's harness and agent-core's with
identical config, same prompt/model/provider/repo fixture. Diff the
**structural** event stream — event types, ordering, tool-call names, terminal
state. Not the model's prose.

Any structural difference is a port defect and blocks acceptance.

**Acceptance:** structural streams identical; the comparison script and its
output are committed as evidence.

**Escalation:** if provider access is unavailable in this environment, raise it
as a decision — do not silently downgrade the gate to Task 8.

### T11 · Acceptance verification sweep

| Check | Command |
|---|---|
| AC3 | `grep -rn "uta\.\|unit_test_agent" src/agent_core/` → empty |
| AC4 (widened) | `grep -rni "uta" src/agent_core/` → only allowlisted matches |
| Residual config reads | `grep -n "settings" src/agent_core/harness/` → only `current_config()` sites |
| AC5 | `git status --porcelain` of UTA and cragent matches the T1 baseline exactly |
| AC2 | full `pytest`, zero skips added versus UTA's suite |

### T12 · Documentation

- `agent-core/README.md`: install, public interface table, env-prefix
  subclassing, **the `ThreadPoolExecutor` / `propagate()` caveat prominently** —
  a consumer who misses it gets silent fallback to the module default
- Complete the deviations table in the design doc (D1–D3 plus any found during
  the port; target: no others)
- Update design changelog and roadmap Task 1 status
- File the UTA follow-up: `opencode_prompt_file_threshold_chars` is either a
  dead read to delete or a field to declare. A UTA bug, fixed in UTA.

---

## Requirement coverage matrix

Every acceptance criterion and design decision maps to a task; every task
traces back to a requirement.

| Requirement | Source | Task(s) |
|---|---|---|
| F1 standalone repo | Spec | T1 |
| F2 all 9 modules ported | Spec | T3, T4, T5, T6, T7, T8 |
| F3 config decoupled by injection | Spec | T2 |
| F4 11 test files green | Spec | T3–T8 |
| F5 explicit public interface | Spec | T9 |
| AC1 installable | Spec | T1 |
| AC2 pytest green, no skips | Spec | T11 |
| AC3 no uta imports | Spec | T11 |
| AC4 no deployment-specific values (widened) | Spec + review | T11 |
| AC5 sibling repos unchanged | Spec | T1 (baseline), T11 (verify) |
| AC6 consumer integration test | Spec | T9 |
| AC7 README enumerates interface | Spec | T12 |
| AC8 deviations argued | Spec | T12 |
| C1 interface from production call sites | Review | T9 |
| C2 undeclared field non-change | Review | T2, T5 |
| C3 getattr sites separate pass | Review | T5, T11 |
| I4 `index_source_dirs` dual ownership | Review | T2, T7, T12 (Task 9 note) |
| I5 dual-run parity check | Review | T10 |
| D1 `uta_debug_log_dir` rename | Design | T3, T6 |
| D2 `PROJECT_ROOT` | Design | T7 |
| D3 default changes | Design | T2, T7 |
| ADR-002 ContextVar + executor hazard | ADR | T2, T12 |
| Secret redaction | Design (security) | T2 |
| Source SHA recorded | Design | T1 |

**Orphan check:** no task lacks a requirement. **Gap check:** no requirement
lacks a task.

## Risks carried into implementation

| Risk | Signal | Response |
|---|---|---|
| `client.py` deferred imports hide runtime coupling | T6 fails in a way substitution cannot fix | Escalate. No fallback exists by decision. |
| `fixtures_dir` data is larger or more entangled than expected | T3 | Inventory before porting; stub what is not harness-relevant |
| Parity check blocked by provider access | T10 | Escalate as a decision, do not downgrade |
| `rate_limit.py` has no dedicated test but carries D1 | T3 | Rely on transitive coverage via T5/T6; if those pass, D1 is exercised |
| UTA drifts during the port | T12 | Diff against the T1 SHA before Task 9 |

## Suggested execution

T1 → T2 → **Checkpoint A** → T3 → **Checkpoint B** → T4 → T5 → T6 →
**Checkpoint C** → T7 → T8 → **Checkpoint D** → T9 → T10 → T11 → T12.

Checkpoints A and C are the two worth stopping at: A confirms the foundation
before any port work, C confirms the risk is retired before the easy remainder.
