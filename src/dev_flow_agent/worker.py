"""Run one claimed task through the document pipeline."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

from agent_core.git import GitWorkspace
from agent_core.prompts import PromptLibrary
from agent_core.runtime import RuntimeStore, TaskOutcome
from agent_core.workflow.checkpoints import WorkflowRunIdentity, open_checkpointer
from agent_core.workflow.execution import invoke_workflow, is_interrupted

from dev_flow_agent.issue import jira_key
from dev_flow_agent.config import Settings
from dev_flow_agent.db import TaskStore
from dev_flow_agent.progress import turn_progress_port
from dev_flow_agent.workflow import PROMPT_DIR
from dev_flow_agent.workflow.compile import compile_flow


class Worker:
    def __init__(
        self,
        settings: Settings,
        tasks: TaskStore,
        runtime: RuntimeStore,
        artifacts: Any,
        *,
        overrides=None,
        extra_context: Optional[Mapping[str, Any]] = None,
    ):
        self.settings = settings
        self.tasks = tasks
        self.runtime = runtime
        self.artifacts = artifacts
        self.overrides = overrides
        self.extra_context = dict(extra_context or {})

    def execute(self, task_ref: str) -> TaskOutcome:
        task = self.tasks.get(task_ref)
        if task is None:
            raise KeyError(f"unknown task: {task_ref}")
        identity = WorkflowRunIdentity(
            product="dev-flow",
            task_id=task_ref,
            unit_id="main",
            workflow_run_id=str(task["workflow_run_id"]),
            cycle="intent-spec",
            version="v1",
        )
        self.settings.workspaces_root.mkdir(parents=True, exist_ok=True)
        turn_dir = self.settings.data_dir / "turns" / task_ref
        turn_dir.mkdir(parents=True, exist_ok=True)
        context = {
            "workspace": GitWorkspace(self.settings.workspaces_root),
            # This pipeline writes code onto a work branch, and issue-tracked work
            # branches are cut from trunk as `$jira-$date`. A task naming one
            # that does not exist yet is the normal way a feature starts, not
            # a failure.
            "create_branch": True,
            "prompts": PromptLibrary(PROMPT_DIR),
            "runtime_store": self.runtime,
            "artifacts": self.artifacts,
            "task_store": self.tasks,
            "turn_dir": str(turn_dir),
            **self.extra_context,
        }
        if self.overrides is None and "turn_context" not in context:
            from agent_core.harness import (
                AgentTurnContext,
                HarnessBinding,
                HarnessSpec,
                create_configured_harness,
            )

            class _NoopCost:
                def before_paid_attempt(self, **kw):
                    return None

                def after_paid_attempt(self, **kw):
                    return None

            harness = create_configured_harness(HarnessSpec(name="opencode"))
            context["turn_context"] = AgentTurnContext(
                binding=HarnessBinding(name="opencode", harness=harness),
                cost=_NoopCost(),
                progress=turn_progress_port(self.runtime, task_ref=task_ref),
            )
        cp_path = self.settings.checkpoints_root / f"{task_ref}.sqlite"
        forbidden = (self.settings.workspaces_root,)
        with open_checkpointer(cp_path, forbidden_roots=forbidden) as saver:
            graph = compile_flow(context, saver, overrides=self.overrides)
            resume_value = self._resume_value(graph, identity, task_ref)
            result = invoke_workflow(
                graph,
                identity=identity,
                initial_state={
                    "task_ref": task_ref,
                    "title": task["title"],
                    "request": task["request"],
                    "repo_url": task["repo_url"],
                    "branch": task["branch"],
                    # issue-tracked work is keyed by the issue its branch names; tooling
                    # work has none, and the prompts ask for neither then.
                    "jira": jira_key(task["branch"]) or "",
                    "turn_dir": str(turn_dir),
                },
                recursion_limit=50,
                resume_value=resume_value,
            )
        if result.disposition == "suspended":
            return TaskOutcome.SUSPENDED
        return TaskOutcome.COMPLETED

    def _resume_value(self, graph, identity: WorkflowRunIdentity, task_ref: str) -> Any:
        """The answer this run is waiting on, claimed before it is used.

        Two orderings matter here, and getting either wrong strands the task.

        The claim comes *before* the invoke, as ADR-005 has it. Marking the gate
        resumed afterwards leaves a window -- a deploy, a crash -- in which the
        answer has been applied to the graph but the gate still reads as
        awaiting resume.

        And a gate that no longer matches where the graph is waiting is an
        orphan, so it is retired here. Left alone it keeps the task claimable
        forever while no claim can advance the graph: claim, suspend, release,
        claim, about once a second. That is not a theoretical failure -- it
        burned 112 minutes of CPU on beta before anyone noticed, and because the
        task reads as `running` almost continuously, the page never shows the
        gate form, so the run cannot be answered either.
        """
        snapshot = graph.get_state(identity.invoke_config(recursion_limit=50))
        if not is_interrupted(snapshot):
            return None
        node = (snapshot.next or (None,))[0]
        thread_id = identity.thread_id

        match = None
        orphans = []
        for gate in self.runtime.answered_gates_awaiting_resume(limit=100):
            if gate.task_ref != task_ref:
                continue
            if gate.thread_id == thread_id and gate.node == node and gate.response is not None:
                match = gate
            else:
                orphans.append(gate)
        for gate in orphans:
            self.runtime.mark_resumed(gate.gate_id)

        if match is None:
            return None
        if not self.runtime.mark_resumed(match.gate_id):
            # Another worker claimed this resumption first.
            return None
        return match.response
