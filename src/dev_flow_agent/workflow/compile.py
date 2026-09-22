"""Build the workflow graph from YAML."""

from __future__ import annotations

from typing import Any, Mapping, Optional

from agent_core.workflow import NodeRegistry, WorkflowSpec, shared_registry
from agent_core.workflow.graph import build_graph

from dev_flow_agent.workflow import FLOW_PATH
from dev_flow_agent.workflow.nodes import persist_doc
from dev_flow_agent.workflow.build import (
    abandon_build,
    commit_changes,
    enforcement_outcome,
    enforcement_verdict,
    commit_task,
    more_tasks,
    next_task,
    plan_tasks,
    review_outcome,
    queue_repair,
    review_verdict,
    skip_task,
    workflow_kind,
)
from dev_flow_agent.workflow.publish import publish_doc
from dev_flow_agent.workflow.repos import prepare_repos
from dev_flow_agent.workflow.review import merge_reviews, review_axes, review_axis


def product_registry() -> NodeRegistry:
    reg = NodeRegistry()
    reg.add_node("persist_doc", persist_doc)
    reg.add_node("prepare_repos", prepare_repos)
    reg.add_node("publish_doc", publish_doc)
    reg.add_node("plan_tasks", plan_tasks)
    reg.add_node("next_task", next_task)
    reg.add_node("commit_task", commit_task)
    reg.add_node("commit_changes", commit_changes)
    reg.add_node("review_verdict", review_verdict)
    reg.add_node("enforcement_verdict", enforcement_verdict)
    reg.add_node("queue_repair", queue_repair)
    reg.add_node("review_axes", review_axes)
    reg.add_node("review_axis", review_axis)
    reg.add_node("merge_reviews", merge_reviews)
    reg.add_selector("more_tasks", more_tasks)
    reg.add_selector("review_outcome", review_outcome)
    reg.add_selector("workflow_kind", workflow_kind)
    reg.add_selector("enforcement_outcome", enforcement_outcome)
    reg.add_node("skip_task", skip_task)
    reg.add_node("abandon_build", abandon_build)
    return reg


def compile_flow(
    context: Mapping[str, Any],
    checkpointer: Any,
    *,
    overrides: Optional[NodeRegistry] = None,
):
    spec = WorkflowSpec.from_file(FLOW_PATH)
    registry = NodeRegistry().extend(shared_registry)
    if overrides is not None:
        registry = registry.extend(overrides)
    registry = registry.extend(product_registry())
    # A node that runs a turn itself -- the per-axis reviewers -- has to use the
    # registry's `agent_turn`, not the imported one, or a product override
    # (a test's stub) is bypassed and the node reaches for a live harness.
    context = {**context, "agent_turn": registry.get_node("agent_turn")}
    return build_graph(spec, registry, context=context, checkpointer=checkpointer)
