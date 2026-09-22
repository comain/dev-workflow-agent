# Design Detail (dev-flow-agent): v1 product

Overview: [`design-dev-workflow-v1.md`](design-dev-workflow-v1.md).
This file is the `dev-flow-product` module.

## Changes In This Repo

This repo today is docs + `spikes/durable_gate/`. v1 adds the installable
product.

```
pyproject.toml                    # name=dev-flow-agent, python>=3.11
src/dev_flow_agent/
  __init__.py
  __main__.py                     # python -m dev_flow_agent
  config.py                       # DFA_ env prefix, HarnessConfig subclass
  app.py                          # FastAPI: mount core router + product routes
  cli.py                          # serve | worker | dev
  db.py                           # df_tasks schema
  tasks.py                        # claim / release / create / get / list
  worker.py                       # DaemonPorts.execute
  artifacts.py                    # namespace layout around SecureArtifactStore
  workflow/
    flow.yaml                     # the v1 graph
    nodes.py                      # persist_intent, persist_spec (thin)
    prompts.py                    # PromptLibrary from package resources
    graph.py                      # build_graph + context
  ui/
    templates/                    # home.html, task.html, base.html
    static/app.css
    static/task.js                # EventSource only
    render.py                     # mistune escape + jinja
  identity.py                     # IdentityResolver + Policy wiring
resources/
  prompts/write_intent.md.j2
  prompts/write_spec.md.j2
  skills/                         # copies or path config to plugins/dev-skills
tests/
```

Spike code stays; it is evidence, not imported.

## Key Data Structures And Abstractions

**`df_tasks`**

```sql
CREATE TABLE df_tasks (
  task_id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  request TEXT NOT NULL,
  repo_url TEXT NOT NULL,
  branch TEXT NOT NULL,
  status TEXT NOT NULL,
  created_by TEXT,
  idempotency_key TEXT,
  request_hash TEXT,
  workflow_run_id TEXT NOT NULL,
  lease_owner TEXT,
  lease_until TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX ix_df_tasks_idempotency
  ON df_tasks(idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX ix_df_tasks_updated ON df_tasks(updated_at DESC);
CREATE INDEX ix_df_tasks_claim ON df_tasks(status, lease_until);
```

Status: `queued` | `running` | `waiting_human` | `completed` | `failed`.

**Create payload** (JSON): `{title, request, repo_url, branch}`. Optional
header `Idempotency-Key`. `operator` is not in the body.

**Graph state** (serialisable only): `task_ref`, `title`, `request`,
`repo_url`, `branch`, `repo_path`, `commit_id`, `intent_md`, `spec_md`,
`intent_attempt`, `spec_attempt`, `decision`, `comments`, `prompt_file`,
`turn_text`.

**Artifact namespace** `{task_id}/` with
`IndexedFileRule` for `intent-{n}.md`, `spec-{n}.md`, plus latest files
`intent.md`, `spec.md` (mutable, not immutable). Latest writes are
replace-in-place; attempt files are `immutable=True`.

**`WorkflowRunIdentity`:** `product="dev-flow"`, `cycle="intent-spec"`,
`version="v1"`, `unit_id="main"`, `task_id`, `workflow_run_id` minted at
create.

## Data Dependency Flow

```
POST body → df_tasks.request (immutable originator text)
         → graph initial_state["request"]
         → write_intent prompt
         → turn_text → persist_intent → artifacts + state["intent_md"]
         → human_gate prompt.artifact_markdown
         → answer.comments → state["comments"]
         → write_spec prompt (intent_md + comments + request)
         → persist_spec → artifacts + state["spec_md"]
```

UI reads artifacts from `SecureArtifactStore`, not from graph state and not
from the workspace. Workspace may be recycled after `SUSPENDED`.

## Key Process Flow (intra-repo)

**serve**

- Load settings, `assert_trusted_deployment` if not loopback.
- Open RuntimeStore, artifact store, task DB.
- Mount `create_task_router` with `TaskServicePorts(get_task, list_tasks)`.
- Add product routes and Jinja pages.
- `dev` also starts `TaskDaemon` on a thread.

**worker execute(task_ref)** — always returns `SUSPENDED` or `COMPLETED`,
or raises (`FAILED`). Never `None`.

1. Load task. Build context (workspace, harness session, prompts, store).
2. **Every execute, including resume:** `prepare_workspace` with
   `scope=task_id` into `{DFA_DATA_DIR}/workspaces`. Do not use the originator
   machine's local checkout; a local `repo_url` is cloned into the scoped
   cache (core skips the inplace short-circuit when `scope` is set).
3. Peek `graph.get_state(identity.invoke_config(...))`. If
   `is_interrupted(snapshot)` and the gate for `snapshot.next[0]` has a
   `response`, `resume_value = gate.response`. Else `resume_value = None`.
4. `invoke_workflow(..., resume_value=resume_value)`.
5. Disposition:
   - `suspended` → `TaskOutcome.SUSPENDED`
   - `started` / `resumed` / `reused_completed` with no interrupt → `COMPLETED`
   - raise → daemon `FAILED`; product `release` also `cancel_gate` on any
     still-pending gate for this `task_ref` so the inbox has no corpse
6. After a successful invoke that consumed an answer, `mark_resumed` as
   telemetry (ignore False). Never before invoke.

**claim** (exclusive lease, CR-shaped):

```
status IN ('queued', 'running')
  AND (lease_until IS NULL OR lease_until < now)
OR
status = 'waiting_human'
  AND exists ac_human_gates
        WHERE task_ref = task_id AND state = 'answered'
```

`running` with an expired lease is crash-during-turn recovery
(`invoke(None)`). `waiting_human` with an answered gate is human-resume.
Claim compare-and-sets status to `running` and writes `lease_owner` /
`lease_until`. Two daemons cannot claim the same row.

**release:** `SUSPENDED` → `waiting_human`; `COMPLETED` → `completed`;
`FAILED` → `failed` + cancel pending gates. Clears lease.

## Key Control Flow

`flow.yaml` (normative — this is the graph, not a sketch):

```yaml
name: intent-spec
entry: prepare_workspace
nodes:
  - name: prepare_workspace
    uses: prepare_workspace
  - name: render_intent
    uses: render_prompt
    config: {template: write_intent.md.j2, into: prompt}
  - name: write_intent
    uses: agent_turn
    config: {result_mode: normalized}
  - name: persist_intent
    uses: persist_intent
  - name: review_intent
    uses: human_gate
    config:
      kind: input
      artifact_key: intent_md
      attempt_key: intent_attempt
      prompt_keys: [title]
  - name: render_spec
    uses: render_prompt
    config: {template: write_spec.md.j2, into: prompt}
  - name: write_spec
    uses: agent_turn
    config: {result_mode: normalized}
  - name: persist_spec
    uses: persist_spec
  - name: review_spec
    uses: human_gate
    config:
      kind: input
      artifact_key: spec_md
      attempt_key: spec_attempt
      prompt_keys: [title]
edges:
  - [prepare_workspace, render_intent]
  - [render_intent, write_intent]
  - [write_intent, persist_intent]
  - [persist_intent, review_intent]
  - [render_spec, write_spec]
  - [write_spec, persist_spec]
  - [persist_spec, review_spec]
branches:
  - from: review_intent
    selector: gate_decision
    routes: {approve: render_spec, reject: render_intent}
  - from: review_spec
    selector: gate_decision
    routes: {approve: __end__, reject: render_spec}
```

`result_mode: normalized` is mandatory. Legacy `agent_turn` puts a live
result object in state; ADR-003 already measured
`TypeError: Type is not msgpack serializable` at the gate. Normalized mode
returns failed turns as state (`turn_status != ok`); `persist_*` **raises**
in that case so the task fails rather than opening a gate on empty markdown.

Form POST values are `decision=approve` and `decision=reject`. The task
page button **Request changes** submits `reject`, matching the core inbox
and `gate_decision` routes.

`persist_intent` increments `intent_attempt` so the next `human_gate` gets
a new id. Same for spec.

Idempotent create: insert; on unique conflict, if `request_hash` matches
return 200 with existing task; else 422.

Gate POST from the **task page** hits core `POST /gates/{id}/answer`.
`GET /gates` is a **list of links** to `/tasks/{id}`, not a second review
form (spec F7). Implement by wrapping `pending_gates()` in a product
template; do not mount `render_inbox` as the operator surface.

Cancel task: core `request_control`; product `is_cancelled` in context
checks it. v1 cancel during a turn uses existing harness cancel; during a
gate, `cancel_gate` + task `failed`.

## API And Schema Changes (this repo)

| Method | Path | Notes |
|---|---|---|
| GET | `/` | HTML home |
| GET | `/tasks/{id}` | HTML task page |
| POST | `/api/v1/tasks` | create, 201 or 200 replay |
| GET | `/api/v1/tasks` | core port |
| GET | `/api/v1/tasks/{id}` | core port + status, title, artifact names |
| GET | `/api/v1/tasks/{id}/artifacts/{name}` | `intent.md` \| `spec.md` only, 1 MiB cap |
| GET | `/api/v1/tasks/{id}/events` | core SSE |
| GET | `/gates` | product HTML: pending gates as links to `/tasks/{id}` |
| POST | `/gates/{id}/answer` | core (used by the task page form) |

`name` for artifacts is an allowlist, not a free path.

## Key Design Tradeoffs (repo-local)

- **Product `persist_*` nodes, not a generic core `persist_text_artifact`.**
  Only one consumer today. Promote when a second product wants it (same rule
  as UTA's ledger). Also considered promoting now; rejected as unused
  abstraction.
- **mistune in the product** for `/tasks/{id}` artifact HTML. Core inbox
  stays `<pre>` escaped. Also considered rendering only `<pre>` everywhere;
  rejected because the spec requires a readable spec document, not a dump.
- **Skill files:** config `DFA_SKILLS_ROOT` defaulting to
  `../plugins/plugins/dev-skills/skills` if present, else packaged copies
  under `resources/skills/` of `interview-me/SKILL.md` and
  `spec-driven-development/SKILL.md`. Packaged copies are the test fixture;
  live plugins dir is the operator default. Also considered git-submodule;
  rejected as ops weight for two files.
- **Harness:** `create_configured_harness` + `open_harness_session` per
  execute, closed in `finally`. New session after each gate (process may
  have died). Also considered session affinity across the gate; rejected —
  a live session cannot cross the serialization boundary (ADR-003).
- **Demo identity:** `Policy(allow_anonymous_gates=True)`, not
  `allow_anonymous` (service principal, cannot answer) and not
  `allow_service_gate_answers` (would let tokens approve). Mints a human
  anonymous principal for `ANSWER_GATE` only. Banner on every page.

## Capacity, Reliability, And Security

- List: one SQL, 50 rows, no artifacts.
- Task page: 1 task row + 1 latest artifact read (≤1 MiB) + EventSource.
- mistune on ≤1 MiB markdown once per page load. If that is slow we cap
  render at 200 KiB and offer a raw download; v1 cap is 1 MiB.
- Serve and worker must share the same `DFA_DATA_DIR` (tasks.db, runtime.db,
  artifacts/, checkpoints/). Split dirs would look like "create succeeds,
  worker never sees it".
- SQLite: `WAL`, `busy_timeout=5000`. v1 is single-host.

## Failure-Mode Handling

| Mode | Detection | Recovery | Blast |
|---|---|---|---|
| Worker down while waiting | UI still serves | start worker | queue drains |
| Serve down | browser error | restart serve; worker may still run | no new creates, no answers |
| Repo path missing | `prepare_workspace` fails | task failed | one task |
| Skill file missing | startup check | refuse to start worker | none |
| mistune unavailable | import at render | show escaped `<pre>` fallback | UX |

Logs: `task_id`, disposition, gate_id. No prompt or artifact body in logs.

## Repo-Local Risks And Verification

- **Risk:** SQLite locking between serve and worker. Mitigation: WAL + timeout;
  claim and answer are short transactions; model calls hold no SQLite lock.
- **Risk:** graph state holds full markdown; large specs bloat the
  checkpointer. Mitigation: 1 MiB cap on persist; state stores the same cap.
- Verification: two-process proof in `tests/test_durable_product_gate.py`
  using a stub runner (no live model in CI) that writes a canned intent.md.
  Optional `@pytest.mark.live` for a real turn.

## UI structure (this repo)

`base.html`: system font, one accent (`#1d4ed8`, same family as CR), no
gradient, no CDN.

**Home:** form (title, request textarea, repo_url, branch) + table of tasks
(status, title, repo, updated). Empty: "No runs yet" + the form. Error: banner.

**Task:** header (title, status, repo); ol pipeline of five nodes with
`aria-current` on the active one; `#progress` region `aria-live="polite"`
filled by `task.js` EventSource; `#artifact` server-rendered markdown;
`#gate` form only when `waiting_human` (comments + Approve submits
`decision=approve`; Request changes submits `decision=reject`).

**JS:** `task.js` opens EventSource to `/api/v1/tasks/{id}/events`, appends
public `agent_progress` rows, reconnects on `Last-Event-ID`. No SPA router.
On `task_completed` / `task_failed` / `gate_opened` it `location.reload()`
once so the artifact and form appear without a second protocol.

Keyboard: form controls native; pipeline is an `<ol>` not divs-as-buttons.
