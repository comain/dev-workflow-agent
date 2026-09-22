"""Process A suspends; process B resumes with the human's comments."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from agent_core.identity import Principal
from agent_core.runtime import RuntimeStore

from dev_flow_agent.artifacts import open_store, read_artifact
from dev_flow_agent.config import Settings
from dev_flow_agent.db import TaskStore
from test_flow import _git_repo


CHILD = Path(__file__).resolve().parent / "durable_child.py"


def _run_child(data_dir: Path) -> str:
    import os

    root = Path(__file__).resolve().parents[1]
    tests = Path(__file__).resolve().parent
    env = dict(os.environ)
    env["DFA_DATA_DIR"] = str(data_dir)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(root / "src"), str(tests), env.get("PYTHONPATH", "")]
    )
    proc = subprocess.run(
        [sys.executable, str(CHILD)],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(root),
    )
    return proc.stdout.strip()


def test_two_processes_straddle_the_first_gates(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    data_dir = tmp_path / "var"
    data_dir.mkdir()
    settings = Settings(data_dir=data_dir)
    tasks = TaskStore(settings.tasks_db)
    runtime = RuntimeStore(settings.runtime_db)
    runtime.init()
    artifacts = open_store(settings.artifacts_root)
    row, _ = tasks.create(
        title="SSE",
        request="Watch progress",
        repo_url=str(repo),
        branch="main",
        created_by=None,
        idempotency_key=None,
    )

    def answer(node, comments):
        gate = runtime.pending_gates()[0]
        assert gate.node == node, f"expected {node}, got {gate.node}"
        runtime.answer_gate(
            gate_id=gate.gate_id,
            response={"decision": "approve", "comments": comments},
            principal=Principal(subject="u", kind="user"),
        )

    # Triage is the first gate, and it too is answered by a different process
    # than the one that opened it.
    line_a = _run_child(data_dir)
    pid_a, outcome_a, ref = line_a.split()
    assert outcome_a == "suspended"
    assert ref == row["task_id"]
    assert read_artifact(artifacts, row["task_id"], "triage.md").decode().strip()
    answer("review_triage", "full path please")

    line_b = _run_child(data_dir)
    pid_b, outcome_b, ref_b = line_b.split()
    assert (ref_b, outcome_b) == (ref, "suspended")
    text = read_artifact(artifacts, row["task_id"], "spec.md").decode()
    assert "full path please" in text, "the triage gate's comments reached the spec"
    answer("review_spec", "must survive a worker restart")

    # The proof: a human's words, given to a process that has since exited,
    # reach the document a later process writes.
    line_c = _run_child(data_dir)
    pid_c, outcome_c, ref_c = line_c.split()
    assert (ref_c, outcome_c) == (ref, "suspended")
    assert len({pid_a, pid_b, pid_c}) == 3, "each leg ran in its own process"
    design = read_artifact(artifacts, row["task_id"], "design.md").decode()
    assert "must survive a worker restart" in design
