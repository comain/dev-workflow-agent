# ADR-001: agent-core lives in its own repository

## Status

Accepted, 2026-08-06.

## Context

`cragent` is a fork-by-copy of `unit-test-agent`; its own docstrings record
the lineage (`cragent/src/cr_agent/review_v2/daemon.py:1`,
`.../opencode_process.py:1`). `dev-flow-agent` would be the third copy of the
OpenCode harness. Scope discovery showed `uta/opencode/` (9 modules, 3,373 LOC)
depends on exactly one symbol outside its own package, so a shared package is
achievable.

Three placements were considered: a package inside `dev-flow-agent`, a separate
`../agent-core` repository, or a vendored copy behind an interface.

## Decision

`agent-core` is a **separate repository** at `../agent-core`, installed by
consumers via path or git ref.

## Consequences

**Positive**

- A hard boundary from day one. A package nested inside `dev-flow-agent` would
  invite dev-flow-specific assumptions to leak in precisely while the API is
  most malleable, and the three-consumer goal depends on that not happening.
- Consumers depend on a published artifact rather than a sibling directory,
  which is the relationship Tasks 8 and 9 need anyway.

**Negative**

- A second repo, its own CI, and a release process before there is a single
  consumer.
- Version-pinning friction during the phase when the API is still moving.
  Mitigated by path installs (`pip install -e ../agent-core`) until Task 8.

**Rejected: package inside dev-flow-agent.** Faster to iterate, but the
boundary would be conventional rather than enforced, and the extraction exists
specifically to enforce a boundary.

**Rejected: vendored copy.** Withdrawn at spec approval. It creates the third
copy that this task exists to prevent.
