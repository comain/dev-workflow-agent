"""Exclusive claim: queued/running expired, or waiting_human with an answered gate."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_core.identity import Principal
from agent_core.runtime import RuntimeStore, TaskOutcome

from dev_flow_agent.db import TaskStore
from dev_flow_agent.tasks import TaskQueue


@pytest.fixture
def queue(tmp_path):
    tasks = TaskStore(tmp_path / "tasks.db")
    runtime = RuntimeStore(tmp_path / "runtime.db")
    runtime.init()
    return TaskQueue(tasks, runtime, lease_seconds=60)


def _insert(queue: TaskQueue, **over):
    row, _ = queue.tasks.create(
        title="t",
        request="r",
        repo_url="/repo",
        branch="main",
        created_by=None,
        idempotency_key=None,
    )
    if over:
        queue.tasks._conn.execute(
            "UPDATE df_tasks SET "
            + ", ".join(f"{k} = ?" for k in over)
            + " WHERE task_id = ?",
            [*over.values(), row["task_id"]],
        )
        queue.tasks._conn.commit()
        row = queue.tasks.get(row["task_id"])
    return row


def test_claims_a_queued_task(queue):
    row = _insert(queue)
    claimed = queue.claim(daemon_id="d1", lease_seconds=60)
    assert claimed == row["task_id"]
    assert queue.tasks.get(claimed)["status"] == "running"


def test_second_claimer_loses_while_lease_holds(queue):
    _insert(queue)
    first = queue.claim(daemon_id="d1", lease_seconds=60)
    second = queue.claim(daemon_id="d2", lease_seconds=60)
    assert first is not None
    assert second is None


def test_expired_running_is_reclaimable(queue):
    past = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    row = _insert(queue, status="running", lease_owner="old", lease_until=past)
    claimed = queue.claim(daemon_id="d2", lease_seconds=60)
    assert claimed == row["task_id"]
    got = queue.tasks.get(claimed)
    assert got["lease_owner"] == "d2"
    assert got["status"] == "running"


def test_waiting_human_without_answered_gate_is_not_claimed(queue):
    _insert(queue, status="waiting_human")
    assert queue.claim(daemon_id="d1", lease_seconds=60) is None


def test_waiting_human_with_answered_gate_is_claimed(queue):
    row = _insert(queue, status="waiting_human")
    gate = queue.runtime.open_gate(
        task_ref=row["task_id"], node="review_triage", kind="input", prompt={}
    )
    queue.runtime.answer_gate(
        gate_id=gate.gate_id,
        response={"decision": "approve"},
        principal=Principal(subject="u", kind="user"),
    )
    assert queue.claim(daemon_id="d1", lease_seconds=60) == row["task_id"]


def test_release_suspended_sets_waiting_human(queue):
    row = _insert(queue)
    queue.claim(daemon_id="d1", lease_seconds=60)
    queue.release(row["task_id"], TaskOutcome.SUSPENDED)
    got = queue.tasks.get(row["task_id"])
    assert got["status"] == "waiting_human"
    assert got["lease_owner"] is None


def test_release_failed_cancels_pending_gates(queue):
    row = _insert(queue)
    queue.runtime.open_gate(
        task_ref=row["task_id"], node="review_triage", kind="input", prompt={}
    )
    queue.release(row["task_id"], TaskOutcome.FAILED)
    assert queue.runtime.pending_gates() == []
    assert queue.tasks.get(row["task_id"])["status"] == "failed"


def test_requeue_failed_sets_queued_with_a_new_run_id(queue):
    row = _insert(queue)
    queue.release(row["task_id"], TaskOutcome.FAILED)
    retried = queue.tasks.requeue(row["task_id"])
    assert retried["status"] == "queued"
    assert retried["lease_owner"] is None
    assert retried["workflow_run_id"] != row["workflow_run_id"]
    assert queue.claim(daemon_id="d1", lease_seconds=60) == row["task_id"]


def test_requeue_rejects_non_failed_and_unknown(queue):
    row = _insert(queue)
    with pytest.raises(ValueError, match="queued"):
        queue.tasks.requeue(row["task_id"])
    with pytest.raises(KeyError):
        queue.tasks.requeue("missing")


def test_fail_waiting_human_cancels_gates(queue):
    row = _insert(queue, status="waiting_human")
    queue.runtime.open_gate(
        task_ref=row["task_id"], node="review_triage", kind="input", prompt={}
    )
    failed = queue.fail(row["task_id"])
    assert failed["status"] == "failed"
    assert failed["lease_owner"] is None
    assert queue.runtime.pending_gates() == []
    assert queue.claim(daemon_id="d1", lease_seconds=60) is None


def test_fail_rejects_terminal_and_unknown(queue):
    row = _insert(queue)
    queue.release(row["task_id"], TaskOutcome.COMPLETED)
    with pytest.raises(ValueError, match="completed"):
        queue.fail(row["task_id"])
    with pytest.raises(KeyError):
        queue.fail("missing")
