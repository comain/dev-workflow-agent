# Todo: agent-core Harness Extraction (Task 1)

Task list for [`plan-agent-core-harness-extraction.md`](plan-agent-core-harness-extraction.md).
Update status inline as work lands.

## Phase 0 — Foundation

- [x] **T1** Create `../agent-core` repo: `git init`, `pyproject.toml`, `src/agent_core/`, `.gitignore`
- [x] **T1** Record UTA source commit SHA in `agent-core/docs/port-baseline.md`
- [x] **T1** Record pre-existing `git status --porcelain` of UTA **and** cragent (AC5 baseline — both repos already dirty)
- [x] **T1** Verify `pip install -e ../agent-core` succeeds
- [x] **T2** `HarnessConfig` with all 39 harness fields, `env_prefix="AGENT_"`
- [x] **T2** Apply D3 defaults: `index_source_dirs=""`, `opencode_turn_log_dir=".agent_cache/opencode_turns"`
- [x] **T2** Do **not** declare `opencode_prompt_file_threshold_chars` (C2); leave a comment saying why
- [x] **T2** `settings` module default, `current_config()`, `use_config()`, `propagate()`
- [x] **T2** Test: `use_config()` scopes and restores
- [x] **T2** Test: `propagate()` survives a `ThreadPoolExecutor` hop; bare `submit()` does not
- [x] **T2** Test: provider API keys redacted in `repr()` and `model_dump()`
- [x] **T2** Verify pydantic-settings `_env_prefix` behaviour against the pinned version

> **Checkpoint A** — installable package, config layer proven, nothing ported.

## Phase 1 — Leaf modules

- [x] **T3** Port `net.py`
- [x] **T3** Port `fallback.py`
- [x] **T3** Port `rate_limit.py` with **D1**: `uta_debug_log_dir()`→`debug_log_dir()`, `/tmp/uta-run-logs`→`/tmp/agent-run-logs`
- [x] **T3** Port `stream.py`
- [x] **T3** Port `conftest.py`; inventory `fixtures_dir` data, port only what harness tests consume
- [x] **T3** Port `test_opencode_stream.py` → green

> **Checkpoint B** — leaf layer runs standalone.

## Phase 2 — Risk concentration

- [x] **T4** Port `tiered_router.py` (16 reads)
- [x] **T4** Port `test_tiered_routing.py`, `test_tiered_routing_resilience.py` → green (`test_parse_providers.py` dropped: zero harness imports, tests `uta.engine.parse`)
- [x] **T5** Port `process.py` (22 dot-access reads)
- [x] **T5** Separate pass for the 3 `getattr(settings, ...)` sites at lines 193, 222, 358
- [x] **T5** C2: port the dead `opencode_prompt_file_threshold_chars` read verbatim
- [x] **T5** Keep `_build_env` private; test imports it from the module
- [x] **T5** Port `test_opencode_process.py` → green
- [x] **T6** Port `client.py` (1,156 LOC, 17 reads) — **watch the deferred imports at `:521,527`**
- [x] **T6** Update `:527` call site for the D1 rename
- [x] **T6** Port `test_opencode_client.py`, `test_opencode_client_streaming.py` → green

> **Checkpoint C** — risk retired. Escalate here if the port is failing; there is no fallback.

## Phase 3 — Remainder

- [x] **T7** Port `config.py` (24 reads)
- [x] **T7** Apply **D2**: `PROJECT_ROOT` no longer depth-derived, not exported
- [x] **T7** Test: `PROJECT_ROOT` resolves correctly under `src/agent_core/harness/`
- [x] **T7** Port `test_opencode_config.py` → green, `:491` unedited (`test_context_providers.py` dropped: zero harness imports, tests `uta.engine.context`)
- [x] **T8** Port `server.py` (17 reads)
- [x] **T8** Port `test_opencode_server.py` → green (`test_opencode_server_removal.py` dropped: zero harness imports, tests `uta.cli`)

> **Checkpoint D** — all 8 harness test files green, zero skips added.

## Phase 4 — Interface and proof

- [x] **T9** `agent_core/harness/__init__.py` exporting the full design table (~40 symbols, 8 modules)
- [x] **T9** Confirm `PROJECT_ROOT` is **not** exported
- [x] **T9** New `test_consumer_integration.py` — injected config, stub turn, no UTA import (AC6)
- [x] **T10** Dual-run parity: deterministic AND live turn both PASSED (2026-08-06, token-pool endpoint)
- [x] **T10** Structural result diff: no differences between harnesses
- [x] **T10** Commit comparison script and output as evidence
- [x] **T10** Original blocker was environmental (internal IP unreachable); resolved by switching endpoint
- [x] **T11** `grep -rn "uta\.\|unit_test_agent" src/agent_core/` → empty
- [x] **T11** `grep -rni "uta" src/agent_core/` → only allowlisted matches
- [x] **T11** `grep -n "settings" src/agent_core/harness/` → only `current_config()` sites
- [x] **T11** UTA and cragent `git status` match the T1 baseline exactly
- [x] **T11** Full `pytest` green, zero skips added
- [x] **T12** `README.md`: install, interface table, env-prefix subclassing, **`propagate()` caveat prominently**
- [x] **T12** Complete the deviations table in the design doc
- [x] **T12** Update design changelog; mark roadmap Task 1 done
- [ ] **T12** File UTA follow-up on the dead `opencode_prompt_file_threshold_chars` read (needs a UTA issue/branch — left for the user)


## Outcome

Completed 2026-08-06. 243 tests green, 7 commits, one per task group.

**Not done, carried forward:**

1. ~~Live turn parity~~ **DONE 2026-08-06.** Both phases pass against
   `http://token-pool.example/v1`. The original blocker was
   environmental, not a port defect. Still uncovered by a live run: token
   accounting (empty on both sides for this provider), rate-limit detection,
   provider fallback, and patch application — all covered by synthetic fixtures
   only. See `agent-core/docs/parity.md`.
2. **UTA follow-up** for the dead `opencode_prompt_file_threshold_chars` read —
   needs a UTA branch, out of scope for a task forbidden from touching UTA.

**Scope corrections found during the build:**

- 3 of the "11 harness test files" had zero harness imports and test the
  language/CLI layers instead. Real oracle: 8 files / 3,510 LOC.
- 4 further tests inside `test_opencode_config.py` test the consumer's settings
  model or shipped data, not the harness. Excluded with per-test rationale in
  `tools/exclude_tests.py`.
- ADR-002's rewrite mechanism was replaced by a config proxy (human-ratified).
- Two new deviations, D4 and D5, neither anticipated by the design.
