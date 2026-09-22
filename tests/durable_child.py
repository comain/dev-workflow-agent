"""One-shot worker process for the two-process durable-gate proof."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from agent_core.runtime import RuntimeStore
from agent_core.workflow import NodeRegistry

from dev_flow_agent.artifacts import open_store
from dev_flow_agent.config import Settings
from dev_flow_agent.db import TaskStore
from dev_flow_agent.tasks import TaskQueue
from dev_flow_agent.worker import Worker
from test_flow import _fake_turn


def main() -> None:
    settings = Settings(data_dir=Path(os.environ["DFA_DATA_DIR"]))
    tasks = TaskStore(settings.tasks_db)
    runtime = RuntimeStore(settings.runtime_db)
    runtime.init()
    artifacts = open_store(settings.artifacts_root)
    queue = TaskQueue(tasks, runtime)
    overrides = NodeRegistry()
    overrides.add_node("agent_turn", _fake_turn())
    worker = Worker(settings, tasks, runtime, artifacts, overrides=overrides)
    ref = queue.claim(daemon_id=f"pid-{os.getpid()}", lease_seconds=60)
    if ref is None:
        print("NO_CLAIM", file=sys.stderr)
        sys.exit(2)
    outcome = worker.execute(ref)
    queue.release(ref, outcome)
    print(f"{os.getpid()} {outcome.value} {ref}")


if __name__ == "__main__":
    main()
