# Spike: durable human gates

Answers the Task 2 question: **can a workflow suspend on a human decision,
release its worker process, and resume later in a different process?**

Findings are recorded in [`ADR-003`](../../docs/decisions/ADR-003-workflow-engine-and-durable-gates.md).
This directory is the executed evidence behind it.

## Part 1 — cross-process suspend/resume (no model calls, free)

```bash
rm -f /tmp/spike_gate.db
.venv/bin/python spikes/durable_gate/step_start.py  /tmp/spike_gate.db flow-1
# process exits here, gated
.venv/bin/python spikes/durable_gate/step_resume.py /tmp/spike_gate.db flow-1 approve "tighten the gate timeout"
```

`worker_trace` in the output straddles two pids — that is the proof.

## Part 2 — the same, with a real OpenCode turn on each side of the gate

Needs `agent-core/.env.local` with a provider key, and costs two live model
calls.

```bash
rm -f /tmp/spike_harness.db
.venv/bin/python spikes/durable_gate/harness_start.py
.venv/bin/python spikes/durable_gate/harness_resume.py
```

The post-gate turn visibly incorporates the reviewer's comment, which is what
makes the gate worth having.

## The one rule a node author must not break

Only serializable values may enter graph state. A `TurnResult` may not — extract
its text at the node boundary. LangGraph enforces this at the gate with
`TypeError: Type is not msgpack serializable`, so the mistake fails loudly
rather than corrupting a checkpoint.
