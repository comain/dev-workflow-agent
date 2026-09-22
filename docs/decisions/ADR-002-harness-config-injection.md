# ADR-002: Harness config resolves through a context-scoped accessor

## Status

Accepted, 2026-08-06.

## Context

The spec requires the extracted harness to take injected configuration rather
than import a global singleton (F3), and simultaneously requires UTA's 11
harness test files to be the correctness oracle, ported with changes limited to
import paths and config construction (F4).

Measurement of the source (2026-08-06) showed these requirements are in
tension:

- 99 read sites: 96 dot-access across `client.py` (17), `config.py` (24),
  `process.py` (22), `server.py` (17), `tiered_router.py` (16), plus 3 dynamic
  `getattr(settings, ...)` reads at `process.py:193,222,358`. *(Corrected from
  96 during design review — the getattr form is not matched by the dot-access
  substitution and needs its own pass.)*
- All are reads; the harness never mutates settings.
- All sit inside **module-level free functions taking no config parameter** —
  `generate_opencode_config()`, `effective_model()`, `parse_provider_chain()`.
  Config reaches them via `from uta.config import settings`.
- The tests patch that global **by dotted string**:
  `monkeypatch.setattr("uta.config.settings.opencode_model", "gpt-5")` —
  roughly 250 such lines.

Threading a config parameter through the free functions would change ~99
signatures and invalidate ~250 test lines, because the tests would have no
global left to patch. That rewrites the oracle the port depends on, and
maximises the migration cost for Tasks 8 and 9.

## Decision

Harness code resolves configuration through `agent_core.config.current_config()`,
backed by a `ContextVar` whose default is a **mutable module-level instance**
`agent_core.config.settings`.

- Consumers inject with `use_config(cfg)`, getting per-task/per-thread
  isolation.
- When nothing is scoped, `current_config()` returns the module default.
- Ported tests patch attributes on that default, preserving their existing
  idiom as a pure string substitution.
- Function signatures are unchanged.

`HarnessConfig` is a **flat** `BaseSettings` model (decided at spec approval),
default env prefix `AGENT_`, overridden by subclassing.

## Consequences

**Positive**

- Both halves of the port are mechanical and grep-verifiable: 99 read sites
  (96 dot-access + 3 getattr, the latter needing a separate pass) and ~250 test
  lines, each a string substitution.
- The oracle survives intact, which is the only reason we can claim behavioural
  parity.
- Real consumers get isolation the current global cannot provide — cragent
  already runs concurrent reviewers and will want per-task provider config.
- Task 8/9 migration is an import rewrite plus one `use_config()` call.

**Negative**

- A module-level mutable default still exists. It is a deliberate concession to
  the oracle, not an endorsement; it is reachable and mutable by any consumer.
- `ContextVar` **does not propagate into `ThreadPoolExecutor` worker threads** —
  a new thread starts with an empty context and would silently fall back to the
  module default. This is a live hazard because cragent fans out reviewers
  through an executor. Mitigated by shipping an explicit
  `agent_core.config.propagate()` helper, documenting it in the README, and
  covering it with a test.
- Indirection: reading harness code now requires knowing what `current_config()`
  resolves to.

## Alternatives rejected

- **Thread config through every signature.** The purest design, and correct if
  we were writing this fresh. Wrong for a port: it destroys the oracle.
  Reconsider at Task 1b, when call sites are being touched anyway.
- **Plain mutable global.** Simplest, and the tests would pass unchanged. But
  it re-homes the exact global that blocks per-task configuration, and we would
  pay to remove it almost immediately.
- **Class-based `Harness` object.** Cleanest long-term shape. Converts free
  functions to methods, which is the same oracle-destroying diff as threading,
  with more churn. Revisit at Task 1b.

## Follow-up

Design open question 2 asks whether `current_config()` should raise when
nothing is configured rather than silently defaulting. That is safer but breaks
the ported tests' idiom, so it is deferred to Task 1b as hardening.
