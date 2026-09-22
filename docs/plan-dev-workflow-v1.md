# Implementation Plan: dev-flow-agent v1

Work-type: non-Jira tooling (no release-approval workflow)
Spec: [`spec-dev-workflow-v1.md`](spec-dev-workflow-v1.md)
Design: [`design-dev-workflow-v1.md`](design-dev-workflow-v1.md)
Usage: [`usage-dev-workflow-v1.md`](usage-dev-workflow-v1.md)
ADR: [`decisions/ADR-005-leased-gate-resume.md`](decisions/ADR-005-leased-gate-resume.md)
Task list: [`todo-dev-workflow-v1.md`](todo-dev-workflow-v1.md)

## Overview

Close the agent-core human-gate seam, then ship a product that creates a
task from web or API, writes `intent.md`, suspends, resumes under a lease
after a human answers, writes `spec.md`, and suspends again. Proof is a
two-process restart at the first gate.

Build order follows the capability map: `human-gate-runtime` then
`dev-flow-product`. High-risk core work is first.

## Architecture Decisions

- Additive agent-core `0.8.x` (`suspended`, `_bind` merge, `allow_anonymous_gates`).
- Resume token is interrupt + gate `response`; task lease is the lock.
- YAML graph with `result_mode: normalized`; persist raises on failed turns.
- Server-rendered HTML + EventSource; no SPA.
- `dev` runs serve + worker; SQLite under `DFA_DATA_DIR`.

## Requirement Coverage

| Source | Requirement | Covered by | Notes |
| --- | --- | --- | --- |
| spec AC1 | YAML `uses: human_gate` | T1, T3 | `_bind` then the node |
| spec AC2 | `invoke_workflow` → `suspended` | T2 | |
| spec AC3 | Create via UI and `POST /api/v1/tasks` | T6, T10 | |
| spec AC4 | Live `agent_progress` on task page | T12 | |
| spec AC5 | Kill worker; artifact + form still served | T9, T11 | |
| spec AC6 | Approve with comments; spec uses them | T8, T9 | |
| spec AC7 | Approve spec; complete; artifacts GET-able | T6, T8, T11 | |
| spec AC8 | Reject loops; second inbox entry | T8 | |
| spec AC9 | Two concurrent resumes: one continues | T7 | exclusive claim |
| spec AC10 | `<script>` renders as text | T5, T11 | |
| spec F3 / ADR-005 | Leased resume, not maintenance invoke | T2, T7, T8 | |
| spec F4 | Inbox shows artifact, not JSON dump | T5 | `<pre>` in core |
| spec F7 | Task page is the working surface; `/gates` is a finder | T11, T12 | |
| design C1 | `_bind` forwards LangGraph config + `graph_node` | T1 | |
| design C2 | Claim `queued\|running` expired + `waiting_human` answered | T7 | |
| design C3 | Resume from interrupt+response; `mark_resumed` after | T2, T8 | |
| design I1 | Normative `flow.yaml`, normalized turns | T8 | |
| design I2 | `prepare_workspace` every execute, `scope=task_id` | T4, T8 | |
| design I3 | `Policy.allow_anonymous_gates` | T5 | |
| design I4 | `execute` never returns `None` | T8 | |
| design I5 | `get_state` after every invoke | T2 | |
| design | FAILED cancels pending gates | T13 | |
| design | Idempotent create | T6 | |
| usage | `dev` / `serve` / `worker`, `DFA_DATA_DIR` | T6, T13 | |
| usage | Banner when anonymous gates on | T12 | |

No uncovered spec AC. No orphan tasks. Non-Jira: no release-approval evidence task.

## Task List

### Phase 0 — agent-core seam

- [x] T1 `_bind` forwards LangGraph runtime config
- [x] T2 `invoke_workflow` disposition `suspended` + `resume_value`
- [x] T3 `human_gate` node + `gate_decision` selector
- [x] T4 `prepare_workspace` respects `scope`
- [x] T5 `allow_anonymous_gates` + inbox `artifact_markdown`

### Checkpoint A — core

- [x] `pytest tests/test_workflow_execution.py tests/test_gates_langgraph.py tests/test_workflow_nodes.py tests/test_identity.py tests/test_ui_inbox.py` green in agent-core (143 passed)
- [x] YAML `uses: human_gate` suspends and resumes via `invoke_workflow`
- [x] Existing four `invoke_workflow` dispositions unchanged

### Phase 1 — product graph

- [x] T6 package, `df_tasks`, create API, artifact GET
- [x] T7 claim / release / daemon ports
- [x] T8 `flow.yaml` + persist nodes + execute
- [x] T9 two-process durable-gate proof (stub harness)

### Checkpoint B — path survives a restart

- [x] Process A writes canned intent, exits `waiting_human`
- [x] Serve still returns `GET .../artifacts/intent.md`
- [x] Process B resumes after answer; comments appear in spec artifact
- [x] `execute` never returns `None`

### Phase 2 — UI

- [x] T10 home: create + list
- [x] T11 task page: pipeline, artifact, gate form
- [x] T12 SSE progress, `/gates` links, demo-identity banner

### Checkpoint C — operator surface

- [x] Empty / waiting / completed HTML regions present
- [x] Keyboard-usable forms
- [x] `<script>` in artifact is text

### Phase 3 — ship locally

- [x] T13 FAILED cancels leftover gates; README + usage commands verified

### Checkpoint: Complete

- [ ] Spec AC1–10 each have a passing test or scripted proof
- [ ] Ready for five-axis review

## Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| LangGraph interrupt shape differs by 1.x minor | High | T2 pins `is_interrupted` against a real `interrupt()` graph |
| `_bind` change breaks existing YAML nodes | High | Merge, do not replace, YAML config; T1 tests `render_prompt`/`agent_turn` still see `template` |
| SQLite lock between serve and worker | Med | WAL + short transactions; model calls hold no DB lock |
| Stub-only proof hides harness issues | Med | T9 stub is CI-mandatory; optional `@pytest.mark.live` not a gate |

## Open Questions

None that block implementation. Live-model proof is optional after T9.

## Parallelization

T1 → T2 → T3 is sequential (each needs the previous). T4 and T5 can run
after T1 in parallel with T2. Product T6 can start after Checkpoint A.
Do not start T8 until T2 and T3 are green.
