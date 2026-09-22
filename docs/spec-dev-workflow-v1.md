# Spec: dev-flow-agent v1 — intent → spec with human gates

## Status

- Phase: plan
- Approval: spec approved 2026-09-09; design approved 2026-09-09 after review fixes (`yes`)
- Work type: non-Jira tooling and architecture iteration
- Issue tracker: not applicable. Same precedent as
  `docs/spec-agent-core-harness-extraction.md` and agent-core's later specs.
- Canonical docs: this repo `docs/`
- Affected repositories: `../agent-core` (`human-gate-runtime`), this repo
  (`dev-flow-product`)
- Capability map: [`capability-map-dev-workflow-v1.md`](capability-map-dev-workflow-v1.md)
- Design / usage: written; design-review 2026-09-09, Critical/Important
  findings dispositioned *fix now* by the user (`fix`)
- Roadmap: this is Task 7, first iteration — a thin slice, not the full SDLC

Implements the confirmed intent: a fourth agent-core consumer with a **fixed**
pipeline, **human gates** as the missing capability, and a **web + API**
surface. First graph is **intent → gate → spec → gate**. Later phases
(plan / build / test / review / ship) are stubbed.

## Objective

An engineer or product owner submits a development request from a web page or
HTTP API. A remote worker writes `intent.md`, suspends, and releases the
worker. The human reads the artifact, comments, and accepts or sends it back.
On accept, a (possibly different) worker writes `spec.md` and suspends again.
The same review happens for the spec. That is the whole v1 product.

Success is one path that survives a process restart:

1. Create a task from the web form.
2. Watch live, redacted agent progress on the task page while intent is written.
3. The worker exits; the task shows **waiting for you**.
4. Open the rendered `intent.md`, comment, approve.
5. A new worker continues, writes `spec.md`, suspends again.
6. Approve the spec. The run completes. Both artifacts remain readable.

Primary users: the people who already operate UTA / CR / spec-gen, and the
person driving this repo. Not an external SaaS.

## Assumptions

Correct these now or they stand.

1. **Non-Jira tooling.** No Jira key, release-approval flow, or RDC deploy-approval.
2. **Python ≥ 3.11** for the product, matching agent-core's floor. Run on 3.13
   locally (ADR-003). Pin released `agent-core` `0.8.x` (path install is fine
   during development).
3. **Text-only input in v1.** Images are already in agent-core (`AttachmentStore`)
   and stay unused this iteration.
4. **Target is a git repo** identified by local path or clone URL + branch.
   The agent reads that tree when writing intent and spec.
5. **Artifacts persist outside the live checkout.** A reviewer must still see
   `intent.md` / `spec.md` after the worker has released. Workspace files are
   what the agent reads; `SecureArtifactStore` is what the UI reads.
6. **Identity** uses `agent_core.identity`. Answering a gate requires a human
   principal. Service tokens cannot answer gates. Loopback may allow anonymous
   *reads*; it must not allow anonymous *answers* unless an operator flag says
   so for a local demo, and that flag is loud in the UI.
7. **Skill files** from `../plugins/plugins/dev-skills/skills/` are the prompt
   source for `write_intent` (interview-me + intent template) and `write_spec`
   (spec-driven-development). The product does not interpret skills as a
   router; it loads named files into named nodes.
8. **Reject loops.** Reject + comments re-runs the writer with those comments
   and opens a new inbox entry (`attempt` distinguishes it). It does not end
   the task.
9. **No commit back to the target repo in v1.** Artifacts are reviewable in
   our store. Publishing them into the target git history is a later play.

→ Correct me now or I'll proceed with these.

## Scope discovery

Performed 2026-09-09 against `agent-core` 0.8.x, `cragent` templates,
`spec_generator_agent` UI, and `../plugins/plugins/dev-skills`.

| Module / surface | Accepts or produces this work's data? | Decision | Reason |
|---|---|---|---|
| `agent_core.gates.langgraph.human_gate` | yes — interrupt + store | **in** | Primitive exists; products never used it |
| `agent_core.workflow.nodes` (`prepare_workspace`, `render_prompt`, `agent_turn`) | yes — shared steps | **in** | Add `human_gate` as a fourth shared node |
| `agent_core.workflow.execution.invoke_workflow` | yes — start/resume | **in** | Four dispositions today; no `suspended` |
| `agent_core.runtime.daemon.TaskOutcome.SUSPENDED` | yes | **in** | Product execute must return it; daemon already understands it |
| `resume_answered_gates` invoking the graph in a maintenance hook | yes | **change** | Resume must run under a claimed lease, not as a free invoke. Two daemons must not both continue the run. |
| `agent_core.api.create_task_router` inbox + SSE | yes | **in** | Mount it. There is no create-task route; product adds one. |
| `agent_core.ui.inbox.render_prompt` | yes — but dumps JSON | **in** | Unusable for a spec. Inbox must render a markdown artifact. |
| `agent_core.runtime.progress` + SSE | yes — public tool activity | **consume** | Do not invent a second progress channel |
| `cragent` `_agent_progress.html` | pattern only | **adopt pattern** | Live session tabs. Do not copy CR domain (reviewers, findings, feedback). |
| `spec_generator_agent` `tasks.html` create form + list | pattern only | **adopt pattern** | Repo + branch + submit. Do not copy spec-gen quotas / wiki docs. |
| UTA / CR / spec_generator_agent task tables | no shared schema | **out** | Product owns its own task rows (ADR-004) |
| `plugins/dev-skills` full skill catalog | prompt source for two nodes | **subset** | v1 loads interview-me + spec-driven-development only |
| Chat UIs (Chainlit, Gradio, Streamlit, Open WebUI) | no | **out** | This is artifact review, not a chat |
| React / Vue SPA | no | **out** | Three sibling products are server-rendered HTML; v1 is three pages |

## Core features

### F1 — Shared `human_gate` node (`human-gate-runtime`)

A YAML `WorkflowSpec` can name `uses: human_gate`. Config states `kind`
(`input` \| `approve`), which state keys become the prompt, and the response
schema. The node calls the existing `human_gate()` helper. Nothing live
(subprocess, harness) is in graph state.

### F2 — `invoke_workflow` knows about suspension (`human-gate-runtime`)

When the compiled graph is sitting on an interrupt, `invoke_workflow` returns
disposition `suspended` and does not treat that as failure or as completion.
Resume of an answered gate uses `Command(resume=response)` under the same
identity rules as start/resume/reuse/corrupt. A completed lineage still
reuses; a corrupt lineage still fails closed.

### F3 — Resume is claimed work, not a side-effect (`human-gate-runtime`)

Answering a gate does **not** run the next node. It records the answer. A
daemon claims the task (exclusive lease + heartbeat), then
`invoke_workflow(..., resume_value=response)` while that lease is held.
The resume token is “the snapshot is interrupted and that node’s gate has a
response”, not `resumed_at`. `resume_answered_gates` is a test helper, not
the product driver.

### F4 — Inbox renders the artifact (`human-gate-runtime`)

If the gate prompt carries `artifact_markdown` (or `artifact_html` already
sanitized by the product), the inbox and JSON view show that document, not a
JSON dump of the whole prompt dict. Model-produced markdown is untrusted:
escaped / sanitized, never raw HTML from the model.

### F5 — Fixed v1 graph (`dev-flow-product`)

```
prepare_workspace
  → write_intent     (agent_turn, skills: interview-me)
  → review_intent    (human_gate, kind=input)
  → write_spec       (agent_turn, skills: spec-driven-development; reads accepted intent + comments)
  → review_spec      (human_gate, kind=input)
  → __end__
```

Branch on gate decision: `approve` continues; `reject` returns to the writer
with comments and a new `attempt`.

### F6 — Create from web or API (`dev-flow-product`)

`POST /api/v1/tasks` (idempotency-key honoured) accepts:

- `title` (required)
- `request` (required free text — originator's words)
- `repo_url` (required — local path or git URL)
- `branch` (required)
- `operator` is **not** taken from the body; it comes from identity

The web form posts the same fields. Creating a task enqueues it; it does not
run in the request.

### F7 — Task page is the working surface (`dev-flow-product`)

One URL per task, `/tasks/{id}`, shows four things at once:

1. **Pipeline** — the five nodes, which is current, which is waiting.
2. **Live progress** — SSE from `GET /api/v1/tasks/{id}/events`, rendered with
   the public progress projection (tool-level, redacted). Same contract CR
   already consumes.
3. **Current artifact** — rendered `intent.md` or `spec.md`.
4. **Gate actions** — when `waiting_human`, the approve / reject + comments
   form is on this page. Submitting it is `POST /gates/{gate_id}/answer`.

A separate `/gates` inbox lists every waiting gate across tasks and links to
the task page. It is not a second review experience.

### F8 — List + create home (`dev-flow-product`)

`/` lists recent tasks (status, title, repo, waiting-since) and hosts the
create form. Empty, loading, and error states are real pages, not blanks.

## UI decision (requirements, not pixels)

The UI *is* the product for this slice. A wrong library choice either puts a
chat box where an artifact belongs, or spends the iteration on a SPA.

**What a human actually does**

| Job | Surface | Existing code |
|---|---|---|
| Start work | Form: title, request, repo, branch | spec_generator_agent `tasks.html` submit row (pattern) |
| See that it is alive | Task page, live activity list | CR `_agent_progress.html` + agent-core SSE |
| Read what the agent wrote | Rendered markdown of intent/spec | **missing** — inbox dumps JSON |
| Decide | Comments + approve / request changes | agent-core inbox form, too thin |
| Find waiting work | Inbox list | `GET /gates` |

**Libraries considered**

| Option | Verdict |
|---|---|
| Chainlit / Gradio / Streamlit / Open WebUI | Rejected. Chat transcripts. The playbook's unit of review is a committed markdown artifact, not a message list. |
| React / Vite / Next | Rejected for v1. Three sibling products ship HTML with no node build. Three pages do not earn a second toolchain. |
| HTMX as a framework | Not required. SSE is already the live channel. Forms already POST. Adding HTMX would wrap what FastAPI + EventSource already do. |
| Pico.css / Water.css from a CDN | Rejected as a runtime dependency. Corp pages often cannot fetch CDNs. A small local stylesheet, in the CR/agent-core visual language (system font, one accent, no purple-gradient "AI" look), is enough. |
| Client-side `marked` / EasyMDE | Rejected for rendering. Model markdown is untrusted; render and sanitize on the server. A `<textarea>` is enough for comments in v1. |
| Promote CR's entire admin UI | Rejected. Findings, reviewers, feedback sessions are CR domain. |
| **Server-rendered HTML + vanilla JS EventSource, mounting agent-core's router** | **Chosen.** Matches the three siblings. Reuses SSE, identity, inbox POST, progress projection. Product HTML is new and small. |

**Pages (v1, no more)**

- `GET /` — create + list
- `GET /tasks/{id}` — pipeline, progress, artifact, gate form
- `GET /gates` — cross-task inbox (agent-core, with markdown rendering)
- JSON twins already on the shared router (`/api/v1/tasks`, `/gates/data`,
  `/api/v1/tasks/{id}/events`) plus product `POST /api/v1/tasks` and
  `GET /api/v1/tasks/{id}/artifacts/{name}`

**Accessibility bar:** every control is a real `<button>` / `<textarea>` /
`<a>`; gate form is usable with a keyboard; progress region has `aria-live`;
empty and error states have text, not a spinner-only screen.

**Not a chat.** There is no message composer talking to the model. The
composer is the create form and the gate comments.

## Tech stack

- Python ≥ 3.11, run on 3.13
- `agent-core[langgraph,api,yaml]`
- FastAPI (product app mounts `create_task_router` and adds product routes)
- Jinja2 (already an agent-core dependency) for product pages
- SQLite for product task rows + agent-core runtime DB + LangGraph checkpointer
- pytest
- No Node build, no React, no new CSS framework package

## Commands

```
# product
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q
.venv/bin/python -m dev_flow_agent serve --host 127.0.0.1 --port 8080

# agent-core (seam work)
cd ../agent-core
.venv/bin/pytest tests/test_gates_langgraph.py tests/test_workflow_execution.py tests/test_workflow_nodes.py tests/test_ui_inbox.py tests/test_runtime_daemon.py -q
```

Exact package extra names and the serve module path are confirmed in design;
the requirement is one command that serves UI + API, and a separate daemon
command or the same process with a worker thread for local use.

## Project structure

```
../agent-core/src/agent_core/
  workflow/nodes.py          # add human_gate
  workflow/execution.py      # suspended disposition
  gates/langgraph.py         # leased resume helper
  ui/inbox.py                # markdown artifact rendering

./                          # this repo becomes the product
  pyproject.toml
  src/dev_flow_agent/
    app.py                   # FastAPI
    config.py
    tasks/                   # product task table + daemon ports
    workflow/                # YAML graph, product nodes, prompts
    ui/                      # HTML pages + static CSS/JS
  tests/
  docs/                      # this spec, later design/plan/usage
```

## Code style

Follow agent-core and the sibling products: `from __future__ import annotations`,
typed public functions, module loggers, no live objects in graph state.

```python
# Product execute reports suspension; it does not block on the human.
result = invoke_workflow(graph, identity=identity, initial_state=state, recursion_limit=50)
if result.disposition == "suspended":
    return TaskOutcome.SUSPENDED
```

HTML is templates, not Python string soup for whole pages. Every interpolation
of model text goes through escaping. CR and agent-core already do this; copy
that discipline, not their markup.

## Testing strategy

Ported / existing agent-core tests stay the oracle for harness and current
gate primitive behaviour.

New tests, in order:

1. **agent-core:** YAML `uses: human_gate` suspends, records one inbox row,
   does not duplicate on re-execution.
2. **agent-core:** `invoke_workflow` returns `suspended` on interrupt; a second
   start with the same identity does not re-run the writer.
3. **agent-core:** answering a gate without a claim does not run the next node;
   claimed resume runs it once under concurrency.
4. **agent-core:** inbox HTML contains escaped artifact text; a payload with
   `<script>` does not execute.
5. **product:** create-task is idempotent under a reused key with the same body;
   a reused key with a different body is 422.
6. **product:** reject at `review_intent` re-enters `write_intent` with comments;
   approve proceeds to `write_spec`.
7. **product:** after the worker process exits at the first gate, a new process
   resumes from the checkpointer and the human's comments are visible to
   `write_spec`. This is the production-proof (same bar as the 2026-08-06
   durable-gate spike, with the real product graph).
8. **UI:** template tests for empty list, waiting gate, completed run; no
   browser-driver required in v1 if HTML assertions cover the four regions.

No coverage percentage. The suspend/resume proof is the gate.

## Boundaries

**Always**

- Keep gates as their own nodes, between turns.
- Persist artifacts before opening a gate.
- Release the worker on `SUSPENDED`.
- Sanitize model text in every HTML surface.
- Run agent-core tests for any core change.

**Ask first**

- Adding a frontend build step or a JS framework.
- Committing artifacts into the target repository.
- Adding plan/build nodes.
- Changing identity policy (anonymous answers, service-token answers).
- A breaking agent-core release.

**Never**

- Block a worker for the duration of a human wait.
- Let the writer-agent answer its own gate.
- Put `TurnResult` / `Popen` into graph state.
- Skip or weaken an existing agent-core test to land the seam.
- Ship a chat widget as the review UI.

## Success criteria

1. `uses: human_gate` works from a `WorkflowSpec` file with no product Python
   node for the gate itself.
2. `invoke_workflow(...).disposition == "suspended"` when the graph is waiting.
3. Create via UI and via `POST /api/v1/tasks` both enqueue a run.
4. Task page shows live progress during `write_intent` (SSE frames of type
   `agent_progress`).
5. After the first gate, the serving process can be killed; `/tasks/{id}` still
   shows the rendered intent and the form.
6. Approve with comments; a new worker writes a spec that visibly uses those
   comments; second gate appears.
7. Approve spec; task completes; both artifacts remain GET-able.
8. Reject at intent returns to the writer; a second inbox entry exists; the
   first is not still pending.
9. Two concurrent resume attempts: exactly one continues the graph.
10. A gate prompt containing `<script>alert(1)</script>` renders as text.

## Out of scope

- Plan / build / test / review / ship nodes
- Skill-router / dynamic checklists
- Image input
- Committing artifacts to the target repo or opening an MR
- Migrating UTA / CR / spec-gen onto this graph
- Headless production loops, eval CI, Claude Tag-style channel intake
- React, HTMX-as-architecture, chat UIs
- Multi-user org directory beyond agent-core principals
- Promoting CR's findings/report UI

## Open questions

None that block the spec if the assumptions above are accepted. Design will
choose: one-process serve+daemon vs two commands; exact artifact namespace
layout; whether `artifact_markdown` is a reserved prompt key or a sibling
field on the gate record.

## Design-doc / usage-doc status

- Design: not started — waits on spec approval
- Usage: required (serve command, identity flags, inbox) — written in design
  phase, not now
