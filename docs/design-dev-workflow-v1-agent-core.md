# Design Detail (agent-core): dev-flow-agent v1

Overview: [`design-dev-workflow-v1.md`](design-dev-workflow-v1.md).
This file is the `human-gate-runtime` module.

## Changes In This Repo

| Location | Change |
|---|---|
| `src/agent_core/workflow/graph.py` | **`_bind` must accept LangGraph's runtime config.** Today's wrapper is `call(state)` and freezes YAML `node.config` only (`graph.py:82–100`). Change it to `call(state, config=None)` so LangGraph injects `RunnableConfig`; merge `{**yaml_config, "configurable": lg["configurable"], "graph_node": node.name}`. Without this, YAML `uses: human_gate` cannot read `thread_id` and two nodes that `uses: human_gate` collide on gate id. Additive for existing nodes: they keep YAML keys and ignore `configurable` / `graph_node`. |
| `src/agent_core/workflow/nodes.py` | Register `human_gate` node and `gate_decision` selector. `human_gate` node calls the existing helper with `node=config["graph_node"]` and `config=config`. `prepare_workspace` passes `scope=state.get("task_ref") or config.get("scope")` into `workspace.prepare`. When `scope` is set, **skip the local-path inplace short-circuit** so two tasks do not share a checkout. |
| `src/agent_core/workflow/execution.py` | Fifth disposition `suspended`; optional `resume_value`. After **every** `invoke`, `get_state` and classify — do not sniff the invoke return dict. |
| `src/agent_core/workflow/__init__.py` | Export `is_interrupted`. |
| `src/agent_core/gates/langgraph.py` | `is_interrupted(snapshot)`; `resume_answered_gates` documented as test helper. |
| `src/agent_core/identity/policy.py` | `allow_anonymous_gates: bool = False`. When true, an unauthenticated `ANSWER_GATE` mints `Principal(kind="user", subject=anonymous_subject)`. Today's `allow_anonymous` mints `kind="service"` and **cannot** answer gates (`test_allow_anonymous_does_not_open_human_gates`). `allow_service_gate_answers` stays off. |
| `src/agent_core/ui/inbox.py` | Render `prompt["artifact_markdown"]` as escaped `<pre class="artifact">`. No markdown parser in core. |
| `tests/test_workflow_graph.py` (new or existing graph tests) | Bound node sees `config["configurable"]["thread_id"]` and `config["graph_node"]`. |
| `tests/test_workflow_nodes.py` | YAML `uses: human_gate`; scoped `prepare_workspace` does not use the inplace path. |
| `tests/test_workflow_execution.py` | `suspended` cases; crash-resume fixtures with `next` and no interrupts still `invoke(None)`; after invoke, classification uses `get_state`. |
| `tests/test_gates_langgraph.py` | Keep existing; add that `invoke(None)` on the interrupted snapshot does not apply a human answer. |
| `tests/test_identity.py` | `allow_anonymous_gates` can answer; service tokens still cannot. |
| `tests/test_ui_inbox.py` | `<script>` in `artifact_markdown` is text |

agent-core does **not** grow a markdown parser. Parsing markdown in the
shared inbox would add a dependency every consumer of `agent_core.ui` pays
for. The reserved key is displayed escaped. Rich rendering is product-owned.

## Key Data Structures And Abstractions

**`human_gate` node config**

```python
{
  "kind": "input",              # or "approve"
  "prompt_keys": ["title"],     # copied from state into prompt
  "artifact_key": "intent_md",  # state text → prompt["artifact_markdown"]
  "attempt_key": "intent_attempt",
  "decision_key": "decision",
  "comments_key": "comments",
}
```

Missing `artifact_key` is allowed (approve-only gates). Missing `kind`
defaults to `input`. Unknown keys ignored.

**`gate_decision` selector** returns `str(state.get("decision") or "")`.
Empty string is a KeyError at the branch (no silent default). Product YAML
must map `approve` and `reject`.

**`WorkflowInvocationResult.disposition`**

`"started" | "resumed" | "reused_completed" | "suspended"`

**`invoke_workflow(..., resume_value: Any = None)`**

`resume_value` is omitted on start and crash-resume. It is the gate
`response` mapping on human resume.

**Interrupt detection** — measured helper, not guessed:

```python
def is_interrupted(snapshot) -> bool:
    for task in getattr(snapshot, "tasks", ()) or ():
        if getattr(task, "interrupts", None):
            return True
    values = getattr(snapshot, "values", None) or {}
    return bool(values.get("__interrupt__"))
```

Pinned by a test against a real `interrupt()` graph (existing
`test_gates_langgraph` graph).

## Data Dependency Flow

- **In:** graph state (serialisable), `context["runtime_store"]`, LangGraph
  `config['configurable']['thread_id']`.
- **Out:** `ac_human_gates` row; state keys `decision`, `comments`;
  invocation disposition.
- **Not in graph state:** `RuntimeStore`, harness, `Popen`, `TurnResult`.

`task_ref` is read from `state["task_ref"]`. Absence is `MissingContextError`
at the node, same pattern as `agent_turn` needing `repo_path`.

## Key Process Flow (intra-repo)

```
invoke_workflow
  snapshot = get_state
    absent? → invoke(initial); snapshot = get_state
              interrupted? suspended : started
    next && interrupted && resume_value is None → return suspended (no invoke)
    next && interrupted && resume_value → invoke(Command(resume=resume_value));
              snapshot = get_state
              interrupted? suspended : resumed
    next && not interrupted → invoke(None); snapshot = get_state → resumed
    no next → reused_completed
    else → WorkflowCheckpointError
```

`human_gate` node (inside invoke):

```
open_gate(gate_id_for(thread_id, graph_node, attempt))  # idempotent
append_event gate_opened
interrupt(payload)
# on resume, interrupt() returns the response
return {decision_key, comments_key}
```

## Key Control Flow

- **Re-execution:** gate id = `gate_id_for(thread_id, graph_node, attempt)`.
  `graph_node` is the YAML node **name** (`review_intent` / `review_spec`),
  injected by `_bind` as `config["graph_node"]`. Using `uses` (`human_gate`)
  as the stem is forbidden — the two gates would collide. `attempt` from
  `state[attempt_key]` default `""`. Product increments the attempt key
  each time the writer runs.
- **Reject:** not handled in core. Core returns the answer. Product YAML
  branches `approve` / `reject`.
- **Resume token:** interrupted snapshot ∧ that node's gate has a
  `response`. `resumed_at` is not consulted. Product `execute` peeks
  `get_state`, loads the gate, passes `resume_value=gate.response`.
- **Lease:** product exclusive claim. Core does not lock resume.
- **`mark_resumed`:** after a successful invoke that consumed the answer,
  best-effort telemetry. Never before `Command(resume=)`.
- **`execute` never returns `None`.** `TaskDaemon` maps `None` to
  `COMPLETED` (`daemon.py:196`). Outcomes are only `SUSPENDED`,
  `COMPLETED`, or raise (`FAILED`).

## API And Schema Changes (this repo)

- **No SQLite schema change.** `ac_human_gates.prompt_json` already holds the
  artifact.
- **`invoke_workflow` signature:** add optional `resume_value=None`. Additive.
- **`WorkflowInvocationResult.disposition`:** new documented value. Additive
  for callers that switch on the three old strings.
- **Inbox JSON `gate_to_dict`:** prompt included as today; clients that
  displayed the whole dict still work; HTML grows a `<pre class="artifact">`
  when the key is present.

Minimum version the product pins: whatever tag contains `suspended`. During
dev: path install.

## Key Design Tradeoffs (repo-local)

- Core inbox does not parse markdown. Also considered `mistune` in agent-core;
  rejected — optional UI extra would leak into `agent_core.ui` imports, and
  CR/UTA inbox users should not start depending on a parser they do not need.
- `resume_value` on `invoke_workflow` rather than a new `resume_workflow`.
  One function already classifies four cases; a fifth parameter is cheaper
  than a second entry point that would duplicate snapshot validation.
  Also considered; rejected a second function.
- Merge LangGraph `configurable` into the YAML config dict rather than
  replacing it. Replacing would break `render_prompt` / `agent_turn`, which
  already take `config`. Also considered threading a second argument;
  rejected — nodes already have a `config` parameter.

## Capacity, Reliability, And Security

- `get_state` + at most one `invoke` per `invoke_workflow` call. No extra RPC.
- `open_gate` is one indexed insert / select by `gate_id`.
- Interrupt check walks `snapshot.tasks` (tiny).
- Secrets: prompt must not include API keys; product puts only markdown and
  title in `prompt_keys`.

## Failure-Mode Handling

| Mode | Detection | Recovery | Blast |
|---|---|---|---|
| `interrupt()` API drift | test_gates + test_workflow_execution against real LangGraph | pin `langgraph>=1.0,<2.0` already | product cannot suspend |
| `invoke(None)` on interrupt | we never do it when `is_interrupted` | — | would drop the human answer |
| Missing `runtime_store` in context | `MissingContextError` at node | graph fails the task | one task |
| Missing `thread_id` | existing `ValueError` in `human_gate`; `_bind` now forwards it | — | one task |
| Crash mid-`Command(resume=)` | snapshot still interrupted; gate still has `response` | next claimed execute passes `resume_value` again | one retry, no strand |

## Repo-Local Risks And Verification

- **Risk:** LangGraph interrupt representation differs across 1.x minors.
  Guard: helper tested against the pinned range; if both `tasks[].interrupts`
  and `__interrupt__` are empty after a known `interrupt()`, the test fails.
- **Risk:** existing `invoke_workflow` tests with `snapshot.next` (crash
  resume) must not be classified as `suspended`. Guard: crash-resume fixture
  has `next` and no interrupts.
- Verification: commands in the spec; do not skip `test_workflow_execution.py`.
