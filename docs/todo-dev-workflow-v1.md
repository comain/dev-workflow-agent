# Todo: dev-flow-agent v1

Plan: [`plan-dev-workflow-v1.md`](plan-dev-workflow-v1.md).
Update status inline as work lands.

## Phase 0 — agent-core seam

### T1: `_bind` forwards LangGraph runtime config

**Description:** Change `agent_core.workflow.graph._bind` so the wrapper is
`call(state, config=None)`, merges YAML node config with LangGraph
`configurable`, and injects `graph_node=<YAML node name>`.

**Acceptance:**
- [ ] A node `def f(state, config, context)` sees `config["configurable"]["thread_id"]`
- [ ] `config["graph_node"]` is the YAML **name**, not `uses`
- [ ] Existing `render_prompt` / `agent_turn` still see their YAML keys

**Verification:** pytest on graph bind tests + existing `test_workflow_from_file.py`

**Dependencies:** None

**Files:** `agent-core/src/agent_core/workflow/graph.py`, `agent-core/tests/test_workflow_graph.py` (new) or extend `test_workflow_from_file.py`

**Scope:** S

**Status:** done (2026-09-09)

### T2: `invoke_workflow` disposition `suspended`

**Description:** Fifth disposition. Optional `resume_value` issues
`Command(resume=)`. After every `invoke`, classify via `get_state`.
Interrupted + no `resume_value` → `suspended` without invoking.
Crash-pending (`next`, no interrupts) still `invoke(None)`.

**Acceptance:**
- [ ] Gated graph first start returns `suspended`, not `started`
- [ ] `invoke(None)` on the interrupted `test_gates_langgraph` snapshot does not apply a human answer
- [ ] `resume_value` completes or hits the next gate
- [ ] RecordingGraph pending fixture still `invoke(None)`

**Verification:** `pytest tests/test_workflow_execution.py tests/test_gates_langgraph.py -q`

**Dependencies:** T1 (thread_id available if a YAML node is used; T2 may still use a hand-built graph)

**Files:** `execution.py`, `gates/langgraph.py` (`is_interrupted`), `test_workflow_execution.py`, `test_gates_langgraph.py`

**Scope:** M

**Status:** done (2026-09-09)

### T3: `human_gate` node + `gate_decision` selector

**Description:** Register shared node `human_gate` and selector
`gate_decision`. Node calls existing helper with `node=config["graph_node"]`.

**Acceptance:**
- [ ] YAML `uses: human_gate` suspends, one inbox row, id uses YAML name
- [ ] Two nodes both `uses: human_gate` get distinct ids
- [ ] Re-execution does not duplicate the gate
- [ ] `gate_decision` returns `approve`/`reject`; empty decision errors

**Verification:** `pytest tests/test_workflow_nodes.py tests/test_gates_langgraph.py -q`

**Dependencies:** T1, T2

**Files:** `workflow/nodes.py`, `tests/test_workflow_nodes.py`

**Scope:** S

**Status:** done (2026-09-09)

### T4: `prepare_workspace` respects `scope`

**Description:** Pass `scope=state.get("task_ref") or config.get("scope")`
into `workspace.prepare`. When `scope` is set, skip the local-path inplace
short-circuit.

**Acceptance:**
- [ ] Two scopes of one local repo are different trees
- [ ] No `scope` keeps today's inplace behaviour (UTA/CR)

**Verification:** extend workspace / nodes tests

**Dependencies:** None (can parallel T2)

**Files:** `workflow/nodes.py`, matching test

**Scope:** S

**Status:** done (2026-09-09)

### T5: `allow_anonymous_gates` + inbox artifact rendering

**Description:** New policy flag mints `Principal(kind="user")` for
`ANSWER_GATE` only. Inbox renders `prompt["artifact_markdown"]` as escaped
`<pre class="artifact">`.

**Acceptance:**
- [ ] `allow_anonymous` still cannot answer gates
- [ ] `allow_anonymous_gates` can; service tokens still cannot
- [ ] `<script>` in artifact_markdown is text in HTML

**Verification:** `pytest tests/test_identity.py tests/test_ui_inbox.py -q`

**Dependencies:** None

**Files:** `identity/policy.py`, `ui/inbox.py`, `test_identity.py`, `test_ui_inbox.py`

**Scope:** S

**Status:** done (2026-09-09)

## Checkpoint A

- [ ] Agent-core suites above green; no skip added to ported tests

## Phase 1 — product graph

### T6: package, `df_tasks`, create API, artifact GET

**Description:** Installable `dev-flow-agent`. SQLite `df_tasks`.
`POST /api/v1/tasks` with `Idempotency-Key`. `GET /api/v1/tasks/{id}/artifacts/{name}`
allowlist `intent.md`|`spec.md`, 1 MiB cap. Mount core task router ports.

**Acceptance:**
- [ ] Same key + same body → 200 replay
- [ ] Same key + different body → 422
- [ ] `operator` is not read from the body
- [ ] Unknown artifact name → 404

**Verification:** `pytest tests/test_create_task.py -q`

**Dependencies:** Checkpoint A (path-install core)

**Files:** `pyproject.toml`, `src/dev_flow_agent/{config,db,app,artifacts}.py`, `tests/test_create_task.py`

**Scope:** M

**Status:** done (2026-09-09)

### T7: claim / release / daemon ports

**Description:** Exclusive claim: `queued|running` with expired/empty lease,
or `waiting_human` with an answered gate. Release maps
`SUSPENDED`→`waiting_human`, `COMPLETED`/`FAILED` accordingly.

**Acceptance:**
- [ ] Expired `running` is reclaimable
- [ ] Two claimers: one wins
- [ ] `waiting_human` without answered gate is not claimed

**Verification:** `pytest tests/test_claim.py -q`

**Dependencies:** T6

**Files:** `src/dev_flow_agent/tasks.py`, `tests/test_claim.py`

**Scope:** S

**Status:** done (2026-09-09)

### T8: `flow.yaml` + persist nodes + execute

**Description:** Normative graph from the design. `persist_*` writes
SecureArtifactStore and raises if `turn_status != ok`. `execute` peeks
interrupt, passes `resume_value`, never returns `None`, `mark_resumed`
after success. `prepare_workspace` on every execute.

**Acceptance:**
- [ ] Reject at intent re-enters render_intent; new gate id
- [ ] Approve at intent proceeds to spec
- [ ] Failed turn does not open a gate
- [ ] `execute is None` does not occur

**Verification:** `pytest tests/test_flow.py -q` with stub runner

**Dependencies:** T3, T4, T7

**Files:** `workflow/flow.yaml`, `workflow/nodes.py`, `worker.py`, `tests/test_flow.py`

**Scope:** M

**Status:** done (2026-09-09)

### T9: two-process durable-gate proof

**Description:** Process A runs until `waiting_human` and exits. Process B
answers (or test answers in store) and resumes. Stub harness, no live model.

**Acceptance:**
- [ ] After A exits, `GET` artifact still works
- [ ] B's spec artifact contains A's human comments
- [ ] Writer pid of A is not B's pid

**Verification:** `pytest tests/test_durable_product_gate.py -q`

**Dependencies:** T8

**Files:** `tests/test_durable_product_gate.py`, stub runner helper

**Scope:** S

## Checkpoint B

- [ ] Spec AC5–AC9 proven with stub harness

## Phase 2 — UI

### T10: home — create + list

**Description:** `GET /` form + task table. Empty and error states.

**Acceptance:**
- [ ] Submit creates a task and redirects to `/tasks/{id}`
- [ ] Empty list copy is not a blank page

**Verification:** template tests + API client posting the form

**Dependencies:** T6

**Files:** `ui/templates/{base,home}.html`, `ui/static/app.css`, home route in `app.py`

**Scope:** S

### T11: task page — pipeline, artifact, gate form

**Description:** `/tasks/{id}` four regions. mistune with HTML escaped.
Approve → `decision=approve`; Request changes → `decision=reject`.

**Acceptance:**
- [ ] Waiting task shows form; completed task does not
- [ ] Artifact markdown rendered; `<script>` is text
- [ ] Form posts to core `/gates/{id}/answer`

**Verification:** template tests

**Dependencies:** T5, T8, T10

**Files:** `ui/templates/task.html`, `ui/render.py`, task route

**Scope:** M

### T12: SSE progress, `/gates` links, demo banner

**Description:** `task.js` EventSource. `/gates` is links to task pages, not
`render_inbox`. Banner when `allow_anonymous_gates`.

**Acceptance:**
- [ ] `agent_progress` frames append to `#progress` (`aria-live`)
- [ ] `/gates` has no approve button
- [ ] Banner present only when the demo flag is on

**Verification:** JS/template tests; EventSource unit with a fake stream if cheap

**Dependencies:** T11

**Files:** `ui/static/task.js`, `ui/templates/gates.html`, identity wiring in `app.py`

**Scope:** S

## Checkpoint C

- [ ] Spec AC3, AC4, AC10 visible in HTML

## Phase 3

### T13: FAILED cancels leftover gates; README + usage

**Description:** On `FAILED`, cancel pending gates for that `task_ref`.
README quick start matching usage doc. Confirm `dev` / `serve` / `worker`.

**Acceptance:**
- [ ] Failed task leaves zero pending gates
- [ ] README commands match `usage-dev-workflow-v1.md`

**Verification:** pytest + readme dry-run of `--help`

**Dependencies:** T8, T12

**Files:** `worker.py` or `tasks.py`, `README.md`, `docs/usage-dev-workflow-v1.md` if commands drifted

**Scope:** S
