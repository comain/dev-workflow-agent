"""Intent → spec → design → plan, gated at each stage, with a stub agent_turn."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_core.identity import Principal
from agent_core.runtime import RuntimeStore, TaskOutcome
from agent_core.workflow import NodeRegistry
from agent_core.workflow.nodes import _confine_prompt_file

from dev_flow_agent.artifacts import open_store, read_artifact
from dev_flow_agent.workflow.stages import artifact_names, stages
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


def _fake_turn(*, fail: bool = False, record=None, kind: str = "feature-dev"):
    def agent_turn(state, config, context):
        if record is not None:
            record["prompt_file"] = state.get("prompt_file")
            record["repo_path"] = state.get("repo_path")
            record["turn_dir"] = context.get("turn_dir")
        if state.get("prompt_file"):
            _confine_prompt_file(state["prompt_file"], state.get("repo_path"), context)
        if fail:
            return {"turn_status": "failed", "turn_text": ""}
        prompt = Path(state["prompt_file"]).read_text(encoding="utf-8")
        comments = state.get("comments") or ""
        if "`triage.md`" in prompt:
            return {
                "turn_status": "completed",
                "turn_text": f"# Triage\nfull path\n\nKIND: {kind}\n",
            }
        for stage in ("design-review", "plan", "design", "spec"):
            if f"`{stage}.md`" in prompt:
                return {
                    "turn_status": "completed",
                    "turn_text": f"# {stage.title()}\nfrom upstream\ncomments:{comments}",
                }
        return {
            "turn_status": "completed",
            "turn_text": f"# Turn\n{state.get('request')}",
        }

    return agent_turn


@pytest.fixture
def harness(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    settings = Settings(data_dir=tmp_path / "var")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    tasks = TaskStore(settings.tasks_db)
    runtime = RuntimeStore(settings.runtime_db)
    runtime.init()
    artifacts = open_store(settings.artifacts_root)
    queue = TaskQueue(tasks, runtime)
    overrides = NodeRegistry()
    overrides.add_node("agent_turn", _fake_turn())
    worker = Worker(settings, tasks, runtime, artifacts, overrides=overrides)
    row, _ = tasks.create(
        title="SSE",
        request="Watch progress",
        repo_url=str(repo),
        branch="main",
        created_by=None,
        idempotency_key=None,
    )
    return queue, worker, artifacts, runtime, row


#: The config `flow.yaml` gives the spec stage's persist node.
SPEC_DOC = {
    "artifact": "spec.md",
    "label": "spec",
    "state_key": "spec_md",
    "attempt_key": "spec_attempt",
}


def _pass_triage(queue, worker, runtime):
    """Triage is the first gate now: it decides which path the run takes."""
    ref = queue.claim(daemon_id="d1", lease_seconds=60)
    queue.release(ref, worker.execute(ref))
    assert runtime.pending_gates()[0].node == "review_triage"
    _answer(runtime, "approve", "ok")


def _answer(runtime, decision="approve", comments="ok"):
    gate = runtime.pending_gates()[0]
    runtime.answer_gate(
        gate_id=gate.gate_id,
        response={"decision": decision, "comments": comments},
        principal=Principal(subject="u", kind="user"),
    )
    return gate


def test_first_execute_suspends_at_triage(harness):
    """Triage comes first: what kind of work this is decides the path."""
    queue, worker, artifacts, runtime, row = harness
    ref = queue.claim(daemon_id="d1", lease_seconds=60)
    outcome = worker.execute(ref)
    assert outcome is TaskOutcome.SUSPENDED
    assert runtime.pending_gates()[0].node == "review_triage"
    queue.release(ref, outcome)
    assert read_artifact(artifacts, row["task_id"], "triage.md").decode().strip()


def test_the_spec_follows_the_triage_it_was_given(harness):
    queue, worker, artifacts, runtime, row = harness
    _pass_triage(queue, worker, runtime)
    ref = queue.claim(daemon_id="d1", lease_seconds=60)
    outcome = worker.execute(ref)
    assert outcome is TaskOutcome.SUSPENDED
    queue.release(ref, outcome)
    text = read_artifact(artifacts, row["task_id"], "spec.md").decode()
    assert text.startswith("# Spec")
    assert runtime.pending_gates()[0].node == "review_spec"


def test_reject_reopens_the_spec_gate_with_new_id(harness):
    queue, worker, artifacts, runtime, row = harness
    _pass_triage(queue, worker, runtime)
    ref = queue.claim(daemon_id="d1", lease_seconds=60)
    queue.release(ref, worker.execute(ref))
    first = _answer(runtime, "reject", "needs users")
    ref = queue.claim(daemon_id="d1", lease_seconds=60)
    queue.release(ref, worker.execute(ref))
    pending = runtime.pending_gates()
    assert len(pending) == 1
    assert pending[0].node == "review_spec"
    assert pending[0].gate_id != first.gate_id


def test_approve_the_spec_writes_the_design_using_comments(harness):
    queue, worker, artifacts, runtime, row = harness
    _pass_triage(queue, worker, runtime)
    ref = queue.claim(daemon_id="d1", lease_seconds=60)
    queue.release(ref, worker.execute(ref))
    _answer(runtime, "approve", "must mention SSE")
    ref = queue.claim(daemon_id="d1", lease_seconds=60)
    queue.release(ref, worker.execute(ref))
    design = read_artifact(artifacts, row["task_id"], "design.md").decode()
    assert "must mention SSE" in design
    assert runtime.pending_gates()[0].node == "review_design"


def test_prompt_file_lives_under_turn_dir_not_the_repo(harness):
    queue, worker, artifacts, runtime, row = harness
    _pass_triage(queue, worker, runtime)
    ref = queue.claim(daemon_id="d1", lease_seconds=60)
    queue.release(ref, worker.execute(ref))
    turn_dir = (worker.settings.data_dir / "turns" / row["task_id"]).resolve()
    prompts = list(turn_dir.rglob("prompt.md"))
    assert prompts, f"no prompt.md under {turn_dir}"
    workspace = worker.settings.workspaces_root.resolve()
    leaked = [p for p in workspace.rglob("prompt.md") if not p.resolve().is_relative_to(turn_dir)]
    assert leaked == []


def test_persist_doc_names_snapshot_after_the_feature_title(tmp_path):
    from dev_flow_agent.artifacts import MAX_ARTIFACT_BYTES
    from dev_flow_agent.workflow.nodes import persist_doc

    store = open_store(tmp_path / "art")
    title = "2. drainShard 可提取循环体"
    first = persist_doc(
        {
            "turn_status": "completed",
            "turn_text": "old recap\n",
            "task_ref": "t",
            "title": title,
        },
        SPEC_DOC,
        {"artifacts": store},
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "spec.md").write_text("# New spec\nfrom file\n", encoding="utf-8")
    out = persist_doc(
        {
            "turn_status": "completed",
            "task_ref": "t",
            "title": title,
            "repo_path": str(repo),
            "spec_attempt": first["spec_attempt"],
        },
        SPEC_DOC,
        {"artifacts": store},
    )
    assert "New spec" in out["spec_md"]
    named = store.read_bytes(
        "t/spec-2-drainShard-可提取循环体.md", max_bytes=MAX_ARTIFACT_BYTES
    ).decode()
    assert named == "old recap\n"
    retry = store.read_bytes(
        "t/spec-2-drainShard-可提取循环体-2.md", max_bytes=MAX_ARTIFACT_BYTES
    ).decode()
    assert "New spec" in retry
    latest = store.read_bytes("t/spec.md", max_bytes=MAX_ARTIFACT_BYTES).decode()
    assert "New spec" in latest


def test_persist_uses_workspace_intent_file_not_chat_recap(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    settings = Settings(data_dir=tmp_path / "var")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    tasks = TaskStore(settings.tasks_db)
    runtime = RuntimeStore(settings.runtime_db)
    runtime.init()
    artifacts = open_store(settings.artifacts_root)
    queue = TaskQueue(tasks, runtime)

    def agent_turn(state, config, context):
        prompt = Path(state["prompt_file"]).read_text(encoding="utf-8")
        if "`triage.md`" in prompt:
            return {"turn_status": "completed", "turn_text": "# Triage\n\nKIND: feature-dev\n"}
        Path(state["repo_path"]).joinpath("spec.md").write_text(
            "# Drain shard\n\nExtract the loop body.\n", encoding="utf-8"
        )
        return {
            "turn_status": "completed",
            "turn_text": "I'll inspect the workspace, then write intent.md.",
        }

    overrides = NodeRegistry()
    overrides.add_node("agent_turn", agent_turn)
    worker = Worker(settings, tasks, runtime, artifacts, overrides=overrides)
    row, _ = tasks.create(
        title="drain",
        request="extract loop",
        repo_url=str(repo),
        branch="main",
        created_by=None,
        idempotency_key=None,
    )
    _pass_triage(queue, worker, runtime)
    ref = queue.claim(daemon_id="d1", lease_seconds=60)
    queue.release(ref, worker.execute(ref))
    text = read_artifact(artifacts, row["task_id"], "spec.md").decode()
    assert "Extract the loop body" in text
    assert "I'll inspect" not in text


def test_failed_turn_does_not_open_a_gate(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    settings = Settings(data_dir=tmp_path / "var")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    tasks = TaskStore(settings.tasks_db)
    runtime = RuntimeStore(settings.runtime_db)
    runtime.init()
    artifacts = open_store(settings.artifacts_root)
    queue = TaskQueue(tasks, runtime)
    overrides = NodeRegistry()
    overrides.add_node("agent_turn", _fake_turn(fail=True))
    worker = Worker(settings, tasks, runtime, artifacts, overrides=overrides)
    row, _ = tasks.create(
        title="x",
        request="y",
        repo_url=str(repo),
        branch="main",
        created_by=None,
        idempotency_key=None,
    )
    ref = queue.claim(daemon_id="d1", lease_seconds=60)
    with pytest.raises(RuntimeError, match="triage turn failed"):
        worker.execute(ref)
    queue.release(ref, TaskOutcome.FAILED)
    assert runtime.pending_gates() == []
    assert tasks.get(row["task_id"])["status"] == "failed"


def test_four_gates_run_in_flow_order_to_completion(harness):
    queue, worker, artifacts, runtime, row = harness
    gated = [stage for stage in stages() if stage.gate]
    seen = []
    for _ in gated:
        ref = queue.claim(daemon_id="d1", lease_seconds=60)
        outcome = worker.execute(ref)
        queue.release(ref, outcome)
        assert outcome is TaskOutcome.SUSPENDED
        seen.append(runtime.pending_gates()[0].node)
        _answer(runtime, "approve", "ok")

    ref = queue.claim(daemon_id="d1", lease_seconds=60)
    outcome = worker.execute(ref)
    queue.release(ref, outcome)
    assert outcome is TaskOutcome.COMPLETED
    assert seen == [stage.gate for stage in gated]
    assert runtime.pending_gates() == []
    for stage in stages():
        if stage.turn_label == "enforce":
            # The enforcement gate is issue-tracked work only, and this harness runs on
            # a branch with no issue key.
            continue
        assert read_artifact(artifacts, row["task_id"], stage.artifact).decode().strip()


def test_design_reads_the_approved_spec_and_plan_reads_the_design(harness):
    queue, worker, artifacts, runtime, row = harness
    _pass_triage(queue, worker, runtime)
    for _ in ("spec",):
        ref = queue.claim(daemon_id="d1", lease_seconds=60)
        queue.release(ref, worker.execute(ref))
        _answer(runtime, "approve", "ok")
    ref = queue.claim(daemon_id="d1", lease_seconds=60)
    queue.release(ref, worker.execute(ref))
    _answer(runtime, "approve", "use a queue")

    ref = queue.claim(daemon_id="d1", lease_seconds=60)
    queue.release(ref, worker.execute(ref))
    plan = read_artifact(artifacts, row["task_id"], "plan.md").decode()
    assert plan.startswith("# Plan")
    assert "use a queue" in plan
    assert runtime.pending_gates()[0].node == "review_plan"


def test_reject_at_design_reopens_design_not_the_pipeline(harness):
    queue, worker, artifacts, runtime, row = harness
    _pass_triage(queue, worker, runtime)
    for _ in ("spec",):
        ref = queue.claim(daemon_id="d1", lease_seconds=60)
        queue.release(ref, worker.execute(ref))
        _answer(runtime, "approve", "ok")
    ref = queue.claim(daemon_id="d1", lease_seconds=60)
    queue.release(ref, worker.execute(ref))
    first = _answer(runtime, "reject", "wrong layer")

    ref = queue.claim(daemon_id="d1", lease_seconds=60)
    queue.release(ref, worker.execute(ref))
    pending = runtime.pending_gates()
    assert [g.node for g in pending] == ["review_design"]
    assert pending[0].gate_id != first.gate_id
    design = read_artifact(artifacts, row["task_id"], "design.md").decode()
    assert "wrong layer" in design


def test_every_stage_document_is_servable(harness):
    """The allowlist is derived from the flow, so a new stage cannot 404."""
    assert artifact_names() == {stage.artifact for stage in stages()}
    assert [stage.label for stage in stages()] == [
        "triage",
        "spec",
        "design",
        "auto design review",
        "plan",
        "enforcement",
        "review",
    ]


def _approve_to_design_gate(queue, worker, runtime):
    """Run intent and spec, approving both, then the design and its critique."""
    _pass_triage(queue, worker, runtime)
    for _ in ("spec",):
        ref = queue.claim(daemon_id="d1", lease_seconds=60)
        queue.release(ref, worker.execute(ref))
        _answer(runtime, "approve", "ok")
    ref = queue.claim(daemon_id="d1", lease_seconds=60)
    queue.release(ref, worker.execute(ref))


def test_design_is_critiqued_before_the_human_is_asked(harness):
    queue, worker, artifacts, runtime, row = harness
    _approve_to_design_gate(queue, worker, runtime)

    # One suspension, at the human gate -- the critique does not gate.
    pending = runtime.pending_gates()
    assert [g.node for g in pending] == ["review_design"]
    review = read_artifact(artifacts, row["task_id"], "design-review.md").decode()
    assert review.startswith("# Design-Review")
    design = read_artifact(artifacts, row["task_id"], "design.md").decode()
    assert design.startswith("# Design")


def test_the_gate_carries_the_critique_to_the_reviewer(harness):
    queue, worker, artifacts, runtime, row = harness
    _approve_to_design_gate(queue, worker, runtime)

    gate = runtime.pending_gates()[0]
    assert "design_review_md" in gate.prompt
    assert gate.prompt["design_review_md"].strip()


def test_rejecting_the_design_recritiques_the_next_attempt(harness):
    queue, worker, artifacts, runtime, row = harness
    from dev_flow_agent.artifacts import MAX_ARTIFACT_BYTES

    _approve_to_design_gate(queue, worker, runtime)
    _answer(runtime, "reject", "wrong layer")

    ref = queue.claim(daemon_id="d1", lease_seconds=60)
    queue.release(ref, worker.execute(ref))
    assert [g.node for g in runtime.pending_gates()] == ["review_design"]

    # The redone design is critiqued again rather than the first critique
    # standing: two immutable snapshots, and the reviewer's words in the latest.
    task_id = row["task_id"]
    for name in ("design-review-SSE.md", "design-review-SSE-2.md"):
        assert artifacts.read_bytes(f"{task_id}/{name}", max_bytes=MAX_ARTIFACT_BYTES)
    assert "wrong layer" in read_artifact(artifacts, task_id, "design.md").decode()


def _branch(repo: Path, name: str) -> Path:
    """The feature branch the task runs on, as it would exist upstream."""
    subprocess.run(["git", "-C", str(repo), "branch", name], check=True, capture_output=True)
    return repo


def _remote_file(repo: Path, branch: str, path: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "show", f"{branch}:{path}"],
        capture_output=True, text=True, check=True,
    ).stdout


def test_approved_documents_are_pushed_to_the_task_branch(tmp_path):
    """The spec and design of a change belong beside the change, on its branch."""
    repo = _branch(_git_repo(tmp_path / "repo"), "TICKET-100-20260910")
    settings = Settings(data_dir=tmp_path / "var")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    tasks = TaskStore(settings.tasks_db)
    runtime = RuntimeStore(settings.runtime_db)
    runtime.init()
    artifacts = open_store(settings.artifacts_root)
    queue = TaskQueue(tasks, runtime)
    overrides = NodeRegistry()
    overrides.add_node("agent_turn", _fake_turn())
    worker = Worker(settings, tasks, runtime, artifacts, overrides=overrides)
    tasks.create(
        title="drain", request="extract loop", repo_url=str(repo),
        branch="TICKET-100-20260910", created_by=None, idempotency_key=None,
    )

    _pass_triage(queue, worker, runtime)
    ref = queue.claim(daemon_id="d1", lease_seconds=60)
    queue.release(ref, worker.execute(ref))
    _answer(runtime, "approve", "ok")
    ref = queue.claim(daemon_id="d1", lease_seconds=60)
    queue.release(ref, worker.execute(ref))

    # Named for the Jira key in the branch, under the issue-tracker document directory.
    published = _remote_file(repo, "TICKET-100-20260910", "doc/spec-TICKET-100.md")
    assert "Spec" in published
    assert read_artifact(artifacts, tasks.list()[0]["task_id"], "spec.md").decode() == published


def test_a_rejected_document_is_not_published(tmp_path):
    repo = _branch(_git_repo(tmp_path / "repo"), "TICKET-100-20260910")
    settings = Settings(data_dir=tmp_path / "var")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    tasks = TaskStore(settings.tasks_db)
    runtime = RuntimeStore(settings.runtime_db)
    runtime.init()
    queue = TaskQueue(tasks, runtime)
    overrides = NodeRegistry()
    overrides.add_node("agent_turn", _fake_turn())
    worker = Worker(settings, tasks, runtime, open_store(settings.artifacts_root), overrides=overrides)
    tasks.create(
        title="t", request="r", repo_url=str(repo), branch="TICKET-100-20260910",
        created_by=None, idempotency_key=None,
    )

    ref = queue.claim(daemon_id="d1", lease_seconds=60)
    queue.release(ref, worker.execute(ref))
    _answer(runtime, "reject", "not yet")
    ref = queue.claim(daemon_id="d1", lease_seconds=60)
    queue.release(ref, worker.execute(ref))

    listed = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "-r", "--name-only", "TICKET-100-20260910"],
        capture_output=True, text=True,
    )
    assert "doc/" not in listed.stdout


def test_publishing_refuses_to_carry_source_changes(tmp_path):
    """The guard that keeps a turn's edits out of a docs commit."""
    from agent_core.git import GitWorkspace, PushPolicyError

    from dev_flow_agent.workflow.publish import publish_doc

    repo = _branch(_git_repo(tmp_path / "repo"), "TICKET-100-20260910")
    (repo / "Main.java").write_text("changed by the agent\n", encoding="utf-8")
    workspace = GitWorkspace(tmp_path / "ws")
    with pytest.raises(PushPolicyError):
        publish_doc(
            {
                "decision": "approve",
                "repo_path": str(repo),
                "branch": "TICKET-100-20260910",
                "intent_md": "# Intent\n",
                "task_ref": "t",
            },
            {"artifact": "spec.md", "state_key": "intent_md"},
            {"workspace": workspace},
        )


def test_lean_work_skips_design_and_plan(harness):
    """A bug-fix is understood and small -- that is what triage established --
    so it goes from an approved spec straight to building."""
    queue, worker, artifacts, runtime, row = harness
    overrides = NodeRegistry()
    overrides.add_node("agent_turn", _fake_turn(kind="bug-fix"))
    worker = Worker(worker.settings, worker.tasks, runtime, artifacts, overrides=overrides)

    seen = []
    for _ in range(2):  # triage, then the spec
        ref = queue.claim(daemon_id="d1", lease_seconds=60)
        queue.release(ref, worker.execute(ref))
        seen.append(runtime.pending_gates()[0].node)
        _answer(runtime, "approve", "ok")

    assert seen == ["review_triage", "review_spec"]

    ref = queue.claim(daemon_id="d1", lease_seconds=60)
    queue.release(ref, worker.execute(ref))
    # No design gate, no plan gate: the next thing that happens is building.
    assert [g.node for g in runtime.pending_gates()] == []
    assert worker.tasks.plan_tasks(row["task_id"]), "the build still has a unit of work"


def test_feature_work_keeps_the_full_path(harness):
    queue, worker, artifacts, runtime, row = harness
    seen = []
    for _ in range(4):
        ref = queue.claim(daemon_id="d1", lease_seconds=60)
        queue.release(ref, worker.execute(ref))
        seen.append(runtime.pending_gates()[0].node)
        _answer(runtime, "approve", "ok")
    assert seen == ["review_triage", "review_spec", "review_design", "review_plan"]


def test_a_build_without_a_plan_still_has_one_task(harness):
    """Lean work reaches the build with no plan at all. An empty tree would
    review a branch nobody changed."""
    from dev_flow_agent.workflow.build import plan_tasks

    tasks = harness[1].tasks
    out = plan_tasks(
        {"task_ref": harness[4]["task_id"], "plan_md": "", "title": "Fix the NPE", "spec_md": "why"},
        {},
        {"task_store": tasks},
    )
    assert out["task_count"] == 1
    assert tasks.plan_tasks(harness[4]["task_id"])[0]["title"] == "Fix the NPE"


def test_a_branch_that_does_not_exist_yet_is_started_from_trunk(tmp_path):
    """The normal way issue-tracked work starts: a task names `$jira-$date`, which
    nobody has created. It failed at the clone with `Remote branch ... not
    found` before the run had done anything."""
    import subprocess

    from agent_core.git import GitWorkspace

    origin = _git_repo(tmp_path / "origin")
    ws = GitWorkspace(tmp_path / "cache")
    from dev_flow_agent.worker import Worker  # noqa: F401  (context is built there)

    path = ws.prepare(str(origin), branch="TICKET-101-20260915", create_missing=True, local_branch=True)
    head = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert head == "TICKET-101-20260915"


def test_triage_names_the_other_repositories_and_they_are_checked_out(tmp_path):
    """A task names one repository because someone had to type something. A
    contract and its callers, or a Jira issue that spans services, need more
    than that -- and triage is where it becomes known."""
    from dev_flow_agent.workflow.repos import declared_repos, prepare_repos, secondary_paths

    triage = (
        "# Triage\n\nIt changes the outbound contract and the caller.\n\n"
        "KIND: feature-dev\n"
        "REPOS: sample_service, https://git.example.com/team/caller.git\n"
    )
    assert declared_repos(triage) == [
        "sample_service",
        "https://git.example.com/team/caller.git",
    ]
    # The example above an answer is not the answer: last declaration wins.
    assert declared_repos("REPOS: none\n\nREPOS: one_app\n") == ["one_app"]
    assert declared_repos("REPOS: none") == []

    prepared = []

    class _Workspace:
        def prepare(self, url, **kw):
            prepared.append((url, kw.get("branch"), kw.get("create_missing")))
            return tmp_path / url.rsplit("/", 1)[-1]

    class _Applications:
        def repo_url(self, name):
            return f"https://git.example.com/apps/{name}.git"

    state = {
        "task_ref": "t1",
        "branch": "TICKET-1-20260915",
        "repo_url": "https://git.example.com/team/first.git",
        "repo_path": str(tmp_path / "first"),
        "triage_md": triage,
    }
    out = prepare_repos(state, {}, {"workspace": _Workspace(), "applications": _Applications()})

    assert [repo["name"] for repo in out["repos"]] == [
        "https://git.example.com/team/first.git",
        "sample_service",
        "https://git.example.com/team/caller.git",
    ]
    assert out["repos"][0]["primary"] is True
    # Each is cut from trunk on the same branch, like the first one.
    assert prepared == [
        ("https://git.example.com/apps/sample_service.git", "TICKET-1-20260915", True),
        ("https://git.example.com/team/caller.git", "TICKET-1-20260915", True),
    ]
    assert len(secondary_paths({**state, **out})) == 2


def test_a_repository_that_cannot_be_reached_does_not_stop_the_run(tmp_path):
    """The work in the repository we do have is still worth doing; the run
    says which one it could not reach rather than failing at it."""
    from dev_flow_agent.workflow.repos import prepare_repos

    class _Workspace:
        def prepare(self, url, **kw):
            raise RuntimeError("no such repository")

    class _Applications:
        def repo_url(self, name):
            return "https://git.example.com/apps/gone.git"

    state = {
        "task_ref": "t1",
        "branch": "b",
        "repo_url": "https://git.example.com/team/first.git",
        "repo_path": str(tmp_path / "first"),
        "triage_md": "REPOS: gone\n",
    }
    out = prepare_repos(state, {}, {"workspace": _Workspace(), "applications": _Applications()})
    assert [repo["primary"] for repo in out["repos"]] == [True]
    assert out["repos_unreachable"] == ["gone (RuntimeError)"]
