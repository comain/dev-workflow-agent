"""Claiming a gate's answer, and retiring one that no longer applies.

The failure these cover cost 112 minutes of CPU on beta: a gate answered and
applied to the graph, but never marked resumed, keeps the task claimable while
no claim can advance it -- claim, suspend, release, about once a second, with
the task reading as `running` throughout so the page never offers the gate form.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from agent_core.identity import Principal
from agent_core.runtime import RuntimeStore, TaskOutcome
from agent_core.workflow import NodeRegistry

from dev_flow_agent.artifacts import open_store
from dev_flow_agent.config import Settings
from dev_flow_agent.db import TaskStore
from dev_flow_agent.tasks import TaskQueue
from dev_flow_agent.worker import Worker


def _git_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True, capture_output=True)
    for k, v in (
        ("user.email", "t@t"),
        ("user.name", "t"),
        # The fixture doubles as the remote the agent pushes docs back to, and
        # git refuses to update the checked-out branch of a non-bare repo.
        ("receive.denyCurrentBranch", "updateInstead"),
    ):
        subprocess.run(["git", "-C", str(path), "config", k, v], check=True)
    (path / "README.md").write_text("repo\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True, capture_output=True)
    return path


def _turn(state, config, context):
    return {"turn_status": "completed", "turn_text": f"# Doc\n{state.get('comments') or ''}"}


@pytest.fixture
def harness(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    settings = Settings(data_dir=tmp_path / "var")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    tasks = TaskStore(settings.tasks_db)
    runtime = RuntimeStore(settings.runtime_db)
    runtime.init()
    overrides = NodeRegistry()
    overrides.add_node("agent_turn", _turn)
    worker = Worker(settings, tasks, runtime, open_store(settings.artifacts_root), overrides=overrides)
    row, _ = tasks.create(
        title="t", request="r", repo_url=str(repo), branch="main",
        created_by=None, idempotency_key=None,
    )
    return TaskQueue(tasks, runtime), worker, runtime, row["task_id"]


def _answer(runtime, decision="approve"):
    gate = runtime.pending_gates()[0]
    runtime.answer_gate(
        gate_id=gate.gate_id,
        response={"decision": decision, "comments": "ok"},
        principal=Principal(subject="u", kind="user"),
    )
    return gate


def test_the_answer_is_claimed_before_the_graph_is_resumed(harness):
    queue, worker, runtime, task_id = harness
    ref = queue.claim(daemon_id="d1", lease_seconds=60)
    queue.release(ref, worker.execute(ref))
    gate = _answer(runtime)

    ref = queue.claim(daemon_id="d1", lease_seconds=60)
    queue.release(ref, worker.execute(ref))
    resumed = [g for g in runtime.answered_gates_awaiting_resume(limit=50) if g.gate_id == gate.gate_id]
    assert resumed == [], "the gate was applied to the graph but still reads as awaiting resume"


def test_a_gate_for_a_node_the_graph_has_left_is_retired(harness):
    """The exact beta failure: an answered gate stranded at an earlier node."""
    queue, worker, runtime, task_id = harness
    ref = queue.claim(daemon_id="d1", lease_seconds=60)
    queue.release(ref, worker.execute(ref))
    first = _answer(runtime)

    ref = queue.claim(daemon_id="d1", lease_seconds=60)
    queue.release(ref, worker.execute(ref))
    assert runtime.pending_gates()[0].node != first.node

    # Strand it the way a crash between resume and mark_resumed would.
    with runtime.transaction() as conn:
        conn.execute(
            "UPDATE ac_human_gates SET resumed_at = NULL WHERE gate_id = ?", (first.gate_id,)
        )
    assert [g.gate_id for g in runtime.answered_gates_awaiting_resume(limit=50)] == [first.gate_id]

    ref = queue.claim(daemon_id="d1", lease_seconds=60)
    assert ref == task_id, "a stranded answer keeps the task claimable"
    queue.release(ref, worker.execute(ref))

    # Retired, so the next claim finds nothing to do rather than spinning.
    assert runtime.answered_gates_awaiting_resume(limit=50) == []
    assert queue.claim(daemon_id="d1", lease_seconds=60) is None


def test_a_second_worker_does_not_resume_the_same_answer(harness):
    queue, worker, runtime, task_id = harness
    ref = queue.claim(daemon_id="d1", lease_seconds=60)
    queue.release(ref, worker.execute(ref))
    _answer(runtime)

    ref = queue.claim(daemon_id="d1", lease_seconds=60)
    queue.release(ref, worker.execute(ref))
    # The answer is spent; a second pass has nothing to apply and must not
    # re-run the stage on a stale response.
    assert runtime.answered_gates_awaiting_resume(limit=50) == []
    assert queue.claim(daemon_id="d1", lease_seconds=60) is None
