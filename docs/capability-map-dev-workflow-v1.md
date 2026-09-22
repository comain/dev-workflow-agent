# Capability map: dev-flow-agent v1

Status: approved 2026-09-09
Date: 2026-09-09

Two independently shippable capabilities. Either can exist without the other's
product code; the product cannot ship a working gate without the core seam.

```
human-gate-runtime  ──provides──►  dev-flow-product
     (agent-core)                      (dev-flow-agent)
```

| Module id | Repo | Responsibility | Does not own |
|---|---|---|---|
| `human-gate-runtime` | `../agent-core` | YAML-addressable `human_gate` node; `invoke_workflow` reports `suspended`; resume is leased, not a free-running maintenance invoke; inbox can render a markdown artifact | Feature pipeline, prompts, task table, create-task UX, skill files |
| `dev-flow-product` | this repo | Fixed v1 graph (`prepare` → `write_intent` → `review_intent` → `write_spec` → `review_spec`); task rows; web + API; pages for create, watch, review | LangGraph interrupt mechanics, SSE wire format, identity, harness |

**Build order:** `human-gate-runtime` then `dev-flow-product`.

**Contract owner:** `human-gate-runtime` owns the gate node config shape, the
`suspended` disposition, and the inbox artifact-rendering keys. The product
owns `intent.md` / `spec.md` templates and the create-task payload.

**Acyclic:** product imports agent-core. agent-core does not import the product.
