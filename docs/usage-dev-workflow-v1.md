# Usage: dev-flow-agent v1

Operator-facing. Design: [`design-dev-workflow-v1.md`](design-dev-workflow-v1.md).

## Setup

```bash
cd /path/to/dev-flow-agent
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
# during development, path-install the sibling core that has `suspended`:
.venv/bin/pip install -e "../agent-core[langgraph,api,yaml]"
```

Data directory (tasks DB, runtime DB, artifacts, checkpoints):

```bash
export DFA_DATA_DIR="$PWD/var"
mkdir -p "$DFA_DATA_DIR"
```

Harness / model: same `AGENT_` / product-prefixed env as other consumers.
Subclass env prefix is `DFA_`. Provider keys are not logged.

Skills: set `DFA_SKILLS_ROOT` to
`../plugins/plugins/dev-skills/skills` when that tree is present. Otherwise
packaged copies under the wheel are used.

## Commands

| Command | What it does |
|---|---|
| `.venv/bin/python -m dev_flow_agent dev --host 127.0.0.1 --port 8080` | API + UI + worker in one process. Local default. |
| `.venv/bin/python -m dev_flow_agent serve --host 127.0.0.1 --port 8080` | API + UI only |
| `.venv/bin/python -m dev_flow_agent worker` | Daemon only. Same `DFA_DATA_DIR` as serve. |
| `.venv/bin/pytest -q` | Product tests |

`serve` / `dev` refuse to bind a non-loopback address unless the identity
deployment flag says a proxy is the sole ingress (`assert_trusted_deployment`).

## Identity

- Pages and `GET` APIs may be anonymous on loopback.
- `POST /gates/{id}/answer` requires a human principal (header as in
  agent-core: corp SSO proxy). Service tokens cannot answer.
- `--allow-anonymous-gates` is a demo flag. It sets
  `Policy.allow_anonymous_gates`, which mints a **human** anonymous
  principal for `ANSWER_GATE` only. It is not `allow_anonymous` (that flag
  mints a service principal and cannot answer gates) and not
  `allow_service_gate_answers` (that would let tokens approve). Every HTML
  page shows a banner when it is on. Do not use off-loopback.
- `GET /gates` is a list of links into `/tasks/{id}`. Approve / request
  changes only on the task page.

## User-visible behaviour

1. Open `/`. Fill title, request, repo URL or local path, branch. Submit.
2. You land on `/tasks/{id}`. While the worker writes intent, the progress
   list moves. Tool names are redacted public activities, not raw commands.
3. Status becomes **waiting for you**. Progress stops. The intent document
   is in the artifact pane. Comment and **Approve** or **Request changes**.
4. Approve → spec is written → second wait. Request changes → intent is
   rewritten with your comments.
5. Approve spec → status **completed**. Both documents stay on the page.
6. `/gates` lists every waiting run; each row links to its task page.

Create via API:

```bash
curl -sS -X POST http://127.0.0.1:8080/api/v1/tasks \
  -H 'content-type: application/json' \
  -H 'Idempotency-Key: demo-1' \
  -d '{"title":"SSE inbox","request":"Watch agent progress on a task page","repo_url":"/path/to/repo","branch":"main"}'
```

Same key + same body replays the task. Same key + different body is 422.

## Operations

- **Worker died at a gate:** start `worker` or `dev` again. The UI did not
  need it to show the artifact. Resume happens on the next claim after an
  answer.
- **Create succeeded, nothing runs:** serve and worker must share
  `DFA_DATA_DIR`. Check worker logs for claim errors.
- **Progress looks frozen behind nginx:** the router already sends
  `X-Accel-Buffering: no`. Confirm the proxy does not buffer SSE.
- **Cancel a waiting gate:** `POST /gates/{id}/cancel` then the task is
  failed. There is no auto-timeout (gates wait indefinitely).
- **Retention:** v1 has none. Operators delete `DFA_DATA_DIR` subtrees by
  hand. Do not point that dir at a git checkout.

## Rollout

Local / single-host only this iteration. No RDC, no release-approval. Pin agent-core
to the first release that documents disposition `suspended`.
