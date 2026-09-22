# Dev Workflow: dev-flow-agent v1 (intent → spec with human gates)

Iteration: 1
Status: approved
Work-type: non-Jira tooling
Approved-by: user 2026-09-09

This is the process *we* follow to build the product. It is not the product's
own pipeline.

## Steps

- [x] spec-driven-development — docs/capability-map-dev-workflow-v1.md + docs/spec-dev-workflow-v1.md
- [x] design-driven-development — docs/design-dev-workflow-v1.md (overview) + per-repo detail (agent-core, dev-flow-agent)
- [x] design-review — human-decided review of spec/design before freezing scope
- [x] planning-and-task-breakdown — docs/plan-dev-workflow-v1.md + docs/todo-dev-workflow-v1.md
- [x] incremental-implementation — agent-core human-gate seam first, then product graph, then UI
- [x] test-driven-development — prove each slice; gate suspend/resume is the production-proof
- [x] frontend-ui-engineering — create / run / inbox pages; keyboard and empty/error states
- [x] api-and-interface-design — create-task and artifact contracts stay additive
- [ ] code-review-and-quality — five-axis review
- [ ] code-simplification — reduce product-side glue once the core seam holds
- [ ] shipping-and-launch — local runnable product; no release-approval workflow this iteration
