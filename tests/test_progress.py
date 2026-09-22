"""Harness progress reaches the event log and the SSE stream."""

from __future__ import annotations

import json

import pytest
from agent_core.harness.registry import TurnProgress
from agent_core.runtime import RuntimeStore, TaskOutcome

from dev_flow_agent.config import Settings
from dev_flow_agent.db import TaskStore
from dev_flow_agent.progress import turn_progress_port
from dev_flow_agent.tasks import TaskQueue


class _Request:
    name = "write_intent"
    progress_detail = "summary"


@pytest.fixture
def runtime(tmp_path):
    settings = Settings(data_dir=tmp_path / "var")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    store = RuntimeStore(settings.runtime_db)
    store.init()
    return store


def _progress_rows(runtime, task_ref):
    return [
        row
        for row in runtime.events_since(task_ref=task_ref, after_id=0, limit=500)
        if row["event_type"] == "agent_progress"
    ]


def test_a_turn_update_is_recorded_as_an_agent_progress_event(runtime):
    port = turn_progress_port(runtime, task_ref="t1")
    publish = port.callback(request=_Request(), session_id="s1")
    publish(TurnProgress(kind="tool", message="reading nodes.py", tool="read", status="completed"))
    port.flush()

    rows = _progress_rows(runtime, "t1")
    assert len(rows) == 1
    assert rows[0]["stage"] == "write_intent"
    assert json.loads(rows[0]["payload_json"])["tool"] == "read"


def test_progress_is_projected_not_stored_raw(runtime):
    """Model text must not reach a page anyone who can open it may read."""
    port = turn_progress_port(runtime, task_ref="t2")
    publish = port.callback(request=_Request(), session_id="s1")
    publish(
        TurnProgress(
            kind="tool",
            message="secret-token-abc123 in /home/u/.netrc",
            tool="read",
            status="completed",
        )
    )
    port.flush()

    stored = json.dumps([dict(r) for r in _progress_rows(runtime, "t2")])
    assert "secret-token-abc123" not in stored
    assert "/home/u/.netrc" not in stored


def test_a_failing_publish_never_raises_into_the_turn(runtime):
    class _Broken:
        def append_event(self, **kw):
            raise RuntimeError("db gone")

    port = turn_progress_port(_Broken(), task_ref="t3")
    publish = port.callback(request=_Request(), session_id="s1")
    publish(TurnProgress(kind="tool", message="x", tool="read"))
    port.flush()


def _queue(tmp_path):
    settings = Settings(data_dir=tmp_path / "var2")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    tasks = TaskStore(settings.tasks_db)
    runtime = RuntimeStore(settings.runtime_db)
    runtime.init()
    row, _ = tasks.create(
        title="t",
        request="r",
        repo_url="/r",
        branch="main",
        created_by=None,
        idempotency_key=None,
    )
    return TaskQueue(tasks, runtime), runtime, row["task_id"]


def test_release_writes_the_terminal_event_the_stream_ends_on(tmp_path):
    queue, runtime, task_id = _queue(tmp_path)
    queue.claim(daemon_id="d1", lease_seconds=60)
    queue.release(task_id, TaskOutcome.COMPLETED)
    types = [r["event_type"] for r in runtime.events_since(task_ref=task_id, after_id=0)]
    assert "task_completed" in types


def test_suspending_is_not_terminal(tmp_path):
    queue, runtime, task_id = _queue(tmp_path)
    queue.claim(daemon_id="d1", lease_seconds=60)
    queue.release(task_id, TaskOutcome.SUSPENDED)
    types = [r["event_type"] for r in runtime.events_since(task_ref=task_id, after_id=0)]
    assert "task_completed" not in types and "task_failed" not in types


def test_public_detail_keeps_the_file_and_redacts_the_secret(runtime):
    """`summary` alone renders a ticker of categories; detail is the content."""

    class _Detailed:
        name = "write_spec"
        progress_detail = "public_detail"

    port = turn_progress_port(runtime, task_ref="t4")
    publish = port.callback(request=_Detailed(), session_id="s1")
    publish(
        TurnProgress(
            kind="tool", message="read", tool="read", detail="src/dev_flow_agent/app.py"
        )
    )
    publish(TurnProgress(kind="error", message="boom", detail="provider 500: key sk-abc123456"))
    port.flush()

    details = [json.loads(r["payload_json"])["detail"] for r in _progress_rows(runtime, "t4")]
    assert "src/dev_flow_agent/app.py" in details
    assert not any(d and "sk-abc123456" in d for d in details)


def test_every_turn_in_the_flow_asks_for_detail(tmp_path):
    """A turn left on the default policy reports categories and nothing else."""
    from agent_core.workflow import WorkflowSpec

    from dev_flow_agent.workflow import FLOW_PATH

    turns = [n for n in WorkflowSpec.from_file(FLOW_PATH).nodes if n.uses == "agent_turn"]
    assert turns
    assert all(n.config.get("progress_detail") == "public_detail" for n in turns)


def test_worker_gives_the_turn_a_progress_port(tmp_path, monkeypatch):
    """The bug this file exists for: a turn context built without `progress`
    leaves the SSE stream well-formed and permanently empty."""
    import agent_core.harness as harness_mod

    from dev_flow_agent.artifacts import open_store
    from dev_flow_agent.worker import Worker

    settings = Settings(data_dir=tmp_path / "var3")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    tasks = TaskStore(settings.tasks_db)
    store = RuntimeStore(settings.runtime_db)
    store.init()
    row, _ = tasks.create(
        title="t",
        request="r",
        repo_url="/r",
        branch="main",
        created_by=None,
        idempotency_key=None,
    )

    monkeypatch.setattr(harness_mod, "create_configured_harness", lambda spec: object())
    captured = {}

    def capture(*, context, checkpointer, overrides=None):
        captured["turn_context"] = context.get("turn_context")
        raise RuntimeError("stop before invoking")

    monkeypatch.setattr(
        "dev_flow_agent.worker.compile_flow",
        lambda context, checkpointer, overrides=None: capture(
            context=context, checkpointer=checkpointer, overrides=overrides
        ),
    )
    worker = Worker(settings, tasks, store, open_store(settings.artifacts_root))
    with pytest.raises(RuntimeError, match="stop before invoking"):
        worker.execute(row["task_id"])

    assert captured["turn_context"].progress is not None


def test_every_turn_is_labelled_so_progress_names_its_stage():
    """`agent_turn` takes the progress phase from config['label']. Unlabelled,
    every stage reports as "agent_turn" and a five-stage ticker is unreadable.
    """
    from agent_core.workflow import WorkflowSpec

    from dev_flow_agent.workflow import FLOW_PATH

    turns = [n for n in WorkflowSpec.from_file(FLOW_PATH).nodes if n.uses == "agent_turn"]
    assert turns
    labels = [n.config.get("label") for n in turns]
    assert all(labels), "an unlabelled turn reports its stage as agent_turn"
    assert len(set(labels)) == len(labels), "two stages would report the same phase"


def test_the_turn_label_becomes_the_event_stage(runtime):
    """End of the chain: the YAML label is what the page shows as the stage."""

    class _Labelled:
        name = "design_review"
        progress_detail = "public_detail"

    port = turn_progress_port(runtime, task_ref="t5")
    publish = port.callback(request=_Labelled(), session_id="s1")
    publish(TurnProgress(kind="tool", message="read", tool="read", detail="design.md"))
    port.flush()

    assert [r["stage"] for r in _progress_rows(runtime, "t5")] == ["design_review"]


def test_claiming_a_task_says_so_on_the_stream(tmp_path):
    """Answering a gate emits nothing, and the status change it causes is
    invisible to a page tailing the log. Without this the approved page went on
    showing the gate it had already answered."""
    queue, runtime, task_id = _queue(tmp_path)
    before = runtime.latest_event_id(task_ref=task_id)

    assert queue.claim(daemon_id="d1", lease_seconds=60) == task_id
    types = [
        row["event_type"]
        for row in runtime.events_since(task_ref=task_id, after_id=before)
    ]
    assert types == ["run_started"]


def test_a_claim_that_wins_nothing_is_silent(tmp_path):
    queue, runtime, task_id = _queue(tmp_path)
    queue.claim(daemon_id="d1", lease_seconds=60)
    mark = runtime.latest_event_id(task_ref=task_id)

    # Leased to the first daemon, so there is nothing to claim and nothing to say.
    assert queue.claim(daemon_id="d2", lease_seconds=60) is None
    assert runtime.events_since(task_ref=task_id, after_id=mark) == []


def test_a_failed_tool_is_an_error_not_ordinary_progress(runtime):
    """A failing `mvn` arrives as an ordinary tool update, so reading only the
    kind made a broken build render exactly like a passing one."""

    class _Request:
        name = "build"
        progress_detail = "public_detail"

    port = turn_progress_port(runtime, task_ref="t6")
    publish = port.callback(request=_Request(), session_id="s1")
    publish(TurnProgress(kind="tool", message="mvn", tool="bash", status="failed"))
    publish(TurnProgress(kind="tool", message="mvn", tool="bash", status="completed"))
    port.flush()

    rows = _progress_rows(runtime, "t6")
    assert [r["severity"] for r in rows] == ["error", "info"]
    assert json.loads(rows[0]["payload_json"])["status"] == "issue"


def test_a_provider_error_is_still_an_error(runtime):
    class _Request:
        name = "build"
        progress_detail = "public_detail"

    port = turn_progress_port(runtime, task_ref="t7")
    publish = port.callback(request=_Request(), session_id="s1")
    publish(TurnProgress(kind="error", message="provider 500"))
    port.flush()
    assert [r["severity"] for r in _progress_rows(runtime, "t7")] == ["error"]
