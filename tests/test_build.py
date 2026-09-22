"""The unattended half: build every plan task, review, fix, simplify.

A human is asked only when the review cannot get itself to clean, or a task
fails its attempts. Everything else runs through.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from agent_core.identity import Principal
from agent_core.runtime import RuntimeStore, TaskOutcome
from agent_core.workflow import NodeRegistry

from dev_flow_agent.artifacts import open_store, read_artifact
from dev_flow_agent.config import Settings
from dev_flow_agent.db import TaskStore
from dev_flow_agent.tasks import TaskQueue
from dev_flow_agent.worker import Worker
from dev_flow_agent.workflow.build import MAX_REVIEW_ROUNDS, parse_tasks

BRANCH = "TICKET-100-20260910"

PLAN = """# Plan

## Task 1: Add the queue

**Acceptance criteria:**
- [ ] it queues

## Task 2: Drain the queue

**Dependencies:** Task 1

## Checkpoint: After Tasks 1-2
- [ ] tests pass
"""


def _repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True, capture_output=True)
    for k, v in (
        ("user.email", "t@t"),
        ("user.name", "t"),
        ("receive.denyCurrentBranch", "updateInstead"),
    ):
        subprocess.run(["git", "-C", str(path), "config", k, v], check=True)
    (path / "README.md").write_text("repo\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "branch", BRANCH], check=True, capture_output=True)
    return path


CLEAN_REVIEW = "# Review\nNo Critical findings. The code is sound.\n\nVERDICT: clean\n"
CRITICAL_REVIEW = "# Review\n**Critical** \u2014 this is wrong.\n\nVERDICT: critical\n"


def _turn(*, review_text=CLEAN_REVIEW, record=None, kind="feature-dev", enforcement="pass"):
    """A stub agent: writes the document asked for, or a file for a build task."""

    def agent_turn(state, config, context):
        prompt = Path(state["prompt_file"]).read_text(encoding="utf-8")
        if record is not None:
            record.setdefault("prompts", []).append(prompt)
        repo = Path(state["repo_path"])
        if "Implement exactly one task" in prompt:
            seq = state.get("task_seq")
            (repo / f"task{seq}.txt").write_text(f"task {seq}\n", encoding="utf-8")
            return {"turn_status": "completed", "turn_text": f"did task {seq}"}
        if "Simplify the code" in prompt:
            (repo / "simplified.txt").write_text("tidied\n", encoding="utf-8")
            return {"turn_status": "completed", "turn_text": "simplified"}
        if "Fix the Critical findings" in prompt:
            (repo / "fixed.txt").write_text("fixed\n", encoding="utf-8")
            return {"turn_status": "completed", "turn_text": "fixed"}
        if "`triage.md`" in prompt:
            return {"turn_status": "completed", "turn_text": f"# Triage\n\nKIND: {kind}\n"}
        if "`enforcement.md`" in prompt:
            return {
                "turn_status": "completed",
                "turn_text": f"# Enforcement\nran it\n\nENFORCEMENT: {enforcement}\n",
            }
        if "Review this branch on one axis only" in prompt:
            # One agent per axis now; each returns its own section and verdict.
            return {"turn_status": "completed", "turn_text": review_text}
        for stage in ("design-review", "plan", "design", "spec"):
            if f"`{stage}.md`" in prompt:
                body = PLAN if stage == "plan" else f"# {stage.title()}\nbody\n"
                return {"turn_status": "completed", "turn_text": body}
        return {"turn_status": "completed", "turn_text": "# Intent\nbody\n"}

    return agent_turn


@pytest.fixture
def harness(tmp_path):
    repo = _repo(tmp_path / "repo")
    settings = Settings(data_dir=tmp_path / "var")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    tasks = TaskStore(settings.tasks_db)
    runtime = RuntimeStore(settings.runtime_db)
    runtime.init()
    artifacts = open_store(settings.artifacts_root)
    queue = TaskQueue(tasks, runtime)
    row, _ = tasks.create(
        title="drain", request="extract loop", repo_url=str(repo), branch=BRANCH,
        created_by=None, idempotency_key=None,
    )
    return {
        "queue": queue, "tasks": tasks, "runtime": runtime, "artifacts": artifacts,
        "settings": settings, "repo": repo, "task_id": row["task_id"],
    }


def _worker(h, **kw):
    overrides = NodeRegistry()
    overrides.add_node("agent_turn", _turn(**kw))
    return Worker(h["settings"], h["tasks"], h["runtime"], h["artifacts"], overrides=overrides)


def _approve(h, worker, gates=4):
    """Walk the document gates, approving each: triage, spec, design, plan."""
    for _ in range(gates):
        ref = h["queue"].claim(daemon_id="d1", lease_seconds=60)
        h["queue"].release(ref, worker.execute(ref))
        gate = h["runtime"].pending_gates()[0]
        h["runtime"].answer_gate(
            gate_id=gate.gate_id,
            response={"decision": "approve", "comments": "ok"},
            principal=Principal(subject="u", kind="user"),
        )


def test_the_plan_becomes_a_task_tree_and_every_task_is_built(harness):
    worker = _worker(harness)
    _approve(harness, worker)

    ref = harness["queue"].claim(daemon_id="d1", lease_seconds=60)
    outcome = worker.execute(ref)
    harness["queue"].release(ref, outcome)

    rows = harness["tasks"].plan_tasks(harness["task_id"])
    assert [r["title"] for r in rows] == ["Add the queue", "Drain the queue"]
    assert [r["status"] for r in rows] == ["done", "done"]
    assert all(r["commit_sha"] for r in rows), "each task is its own commit"
    assert outcome is TaskOutcome.COMPLETED


def test_the_run_finishes_without_asking_a_human(harness):
    worker = _worker(harness)
    _approve(harness, worker)
    ref = harness["queue"].claim(daemon_id="d1", lease_seconds=60)
    harness["queue"].release(ref, worker.execute(ref))

    assert harness["runtime"].pending_gates() == []
    assert harness["tasks"].get(harness["task_id"])["status"] == "completed"


def test_each_task_is_pushed_to_the_branch_as_its_own_commit(harness):
    worker = _worker(harness)
    _approve(harness, worker)
    ref = harness["queue"].claim(daemon_id="d1", lease_seconds=60)
    harness["queue"].release(ref, worker.execute(ref))

    log = subprocess.run(
        ["git", "-C", str(harness["repo"]), "log", "--format=%s", BRANCH],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "Add the queue (task 1)" in log
    assert "Drain the queue (task 2)" in log
    assert "simplify" in log
    files = subprocess.run(
        ["git", "-C", str(harness["repo"]), "ls-tree", "-r", "--name-only", BRANCH],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "task1.txt" in files and "task2.txt" in files


def test_a_critical_finding_goes_back_through_the_build(harness):
    """Repair is work like any other: it joins the task list, so a fix is
    built, committed, and put back through the gate and the reviewers that
    rejected it."""
    worker = _worker(harness, review_text=CRITICAL_REVIEW)
    _approve(harness, worker)
    ref = harness["queue"].claim(daemon_id="d1", lease_seconds=60)
    harness["queue"].release(ref, worker.execute(ref))

    rows = harness["tasks"].plan_tasks(harness["task_id"])
    repairs = [r for r in rows if r["origin"] == "review"]
    assert repairs, "the findings became work"
    assert all(r["status"] == "done" for r in repairs), "and the work was built"

    # Only after the loop is exhausted is a human asked.
    assert [g.node for g in harness["runtime"].pending_gates()] == ["escalate_review"]


def test_the_review_document_is_kept_and_served(harness):
    worker = _worker(harness)
    _approve(harness, worker)
    ref = harness["queue"].claim(daemon_id="d1", lease_seconds=60)
    harness["queue"].release(ref, worker.execute(ref))
    assert "sound" in read_artifact(harness["artifacts"], harness["task_id"], "review.md").decode()


def test_a_plan_with_no_tasks_does_not_hang(harness):
    """An empty tree must fall through to review, not loop on nothing."""
    from dev_flow_agent.workflow.build import more_tasks

    assert parse_tasks("# Plan\n\nNo tasks here.\n") == []
    assert more_tasks({"build_done": True}) == "review"


def test_a_review_that_says_no_critical_findings_is_clean():
    """The verdict is read from the reviewer's own line. Matching the bare word
    made "No Critical findings" mean the opposite of what it says."""
    from dev_flow_agent.workflow.build import review_verdict

    clean = review_verdict({"review_md": CLEAN_REVIEW, "review_round": 0}, {}, {})
    assert clean["review_outcome"] == "clean"

    critical = review_verdict({"review_md": CRITICAL_REVIEW, "review_round": 0}, {}, {})
    assert critical["review_outcome"] == "fix"


def test_a_review_with_no_verdict_line_falls_back_to_severity_markers():
    from dev_flow_agent.workflow.build import review_verdict

    assert review_verdict(
        {"review_md": "No Critical findings at all.", "review_round": 0}, {}, {}
    )["review_outcome"] == "clean"
    assert review_verdict(
        {"review_md": "- Critical: SQL injection", "review_round": 0}, {}, {}
    )["review_outcome"] == "fix"


def test_the_fix_loop_is_bounded():
    from dev_flow_agent.workflow.build import review_verdict

    state = {"review_md": CRITICAL_REVIEW, "review_round": MAX_REVIEW_ROUNDS - 1}
    assert review_verdict(state, {}, {})["review_outcome"] == "escalate"


def test_a_second_review_round_does_not_carry_the_first_round_findings(harness):
    """`axis_reviews` is a fan-out collect key, so its reducer appends. A round
    that could not clear it merged the previous round's Critical alongside its
    own, and the fix loop could never reach clean."""
    from dev_flow_agent.workflow.review import merge_reviews, review_axes

    opened = review_axes({"review_pass": 1}, {}, {})
    assert opened["review_pass"] == 2

    merged = merge_reviews(
        {
            "title": "x",
            "review_pass": 2,
            "review_axes": [{"name": "security"}],
            "axis_reviews": [
                {"pass": 1, "axis": "Security", "name": "security",
                 "text": "**Critical** stale\n\nVERDICT: critical"},
                {"pass": 2, "axis": "Security", "name": "security",
                 "text": "Fixed.\n\nVERDICT: clean"},
            ],
        },
        {},
        {},
    )
    assert "stale" not in merged["review_md"]
    assert merged["review_md"].rstrip().endswith("VERDICT: clean")


def test_a_failed_task_is_retried_and_then_stops_the_run(harness):
    """A failed task was skipped, so the attempt limit could never be reached
    and the run reviewed a plan it had not finished building."""
    from dev_flow_agent.workflow.build import MAX_TASK_ATTEMPTS, more_tasks, next_task

    tasks = harness["tasks"]
    task_id = harness["task_id"]
    tasks.replace_plan_tasks(task_id, [{"title": "A"}, {"title": "B"}])
    context = {"task_store": tasks}

    first = next_task({"task_ref": task_id}, {}, context)
    assert first["task_title"] == "A"
    tasks.set_plan_task(task_id, 1, status="failed")

    again = next_task({"task_ref": task_id}, {}, context)
    assert again["task_title"] == "A", "a failed task is retried, not skipped"
    tasks.set_plan_task(task_id, 1, status="failed")

    assert tasks.plan_tasks(task_id)[0]["attempts"] == MAX_TASK_ATTEMPTS
    blocked = next_task({"task_ref": task_id}, {}, context)
    assert blocked["build_blocked"] == "A"
    assert more_tasks(blocked) == "blocked"


def test_approving_at_the_build_gate_skips_the_task_and_continues(harness):
    from dev_flow_agent.workflow.build import skip_task

    tasks = harness["tasks"]
    task_id = harness["task_id"]
    tasks.replace_plan_tasks(task_id, [{"title": "A"}, {"title": "B"}])
    tasks.set_plan_task(task_id, 1, status="failed")

    skip_task({"task_ref": task_id}, {}, {"task_store": tasks})
    assert tasks.plan_tasks(task_id)[0]["status"] == "skipped"
    assert tasks.next_plan_task(task_id)["title"] == "B"


def test_rejecting_at_the_build_gate_ends_the_run_as_failed(harness):
    from dev_flow_agent.workflow.build import abandon_build

    with pytest.raises(RuntimeError, match="build abandoned"):
        abandon_build({"build_blocked": "A"}, {}, {})


def test_a_task_changing_status_is_announced(harness):
    """The tree was rendered with the page, so a task finishing was invisible
    until someone reloaded -- a working build looked stalled."""
    from dev_flow_agent.workflow.build import commit_task, next_task

    tasks, runtime, task_id = harness["tasks"], harness["runtime"], harness["task_id"]
    tasks.replace_plan_tasks(task_id, [{"title": "Add the queue"}])
    context = {"task_store": tasks, "runtime_store": runtime}
    mark = runtime.latest_event_id(task_ref=task_id)

    state = next_task({"task_ref": task_id}, {}, context)
    commit_task({**state, "task_ref": task_id, "turn_status": "completed"}, {}, context)

    announced = [
        json.loads(e["payload_json"])
        for e in runtime.events_since(task_ref=task_id, after_id=mark)
        if e["event_type"] == "plan_task"
    ]
    assert [a["status"] for a in announced] == ["running", "done"]
    assert announced[0]["seq"] == 1 and announced[0]["title"] == "Add the queue"


def test_a_failed_task_is_announced_as_an_error(harness):
    from dev_flow_agent.workflow.build import commit_task, next_task

    tasks, runtime, task_id = harness["tasks"], harness["runtime"], harness["task_id"]
    tasks.replace_plan_tasks(task_id, [{"title": "Add the queue"}])
    context = {"task_store": tasks, "runtime_store": runtime}
    mark = runtime.latest_event_id(task_ref=task_id)

    state = next_task({"task_ref": task_id}, {}, context)
    commit_task({**state, "task_ref": task_id, "turn_status": "failed"}, {}, context)

    rows = [
        e for e in runtime.events_since(task_ref=task_id, after_id=mark)
        if e["event_type"] == "plan_task"
    ]
    assert rows[-1]["severity"] == "error"


def test_each_review_axis_announces_itself(harness):
    """The agents are announced before they start, so the page shows what is
    coming rather than growing a list as results trickle in."""
    from dev_flow_agent.workflow.review import review_axes

    runtime, task_id = harness["runtime"], harness["task_id"]
    context = {"runtime_store": runtime, "workspace": None}
    mark = runtime.latest_event_id(task_ref=task_id)

    review_axes({"task_ref": task_id, "design_md": ""}, {}, context)
    announced = [
        json.loads(e["payload_json"])
        for e in runtime.events_since(task_ref=task_id, after_id=mark)
        if e["event_type"] == "review_axis"
    ]
    # No diff to read is the lightest case: one reader, not a thin six.
    assert [a["name"] for a in announced] == ["correctness"]
    assert {a["status"] for a in announced} == {"pending"}


def test_the_panel_is_summoned_by_the_size_of_the_change():
    """Ported from cragent's `classify_risk`, thresholds included: two
    reviewers that disagree about what "small" means are two standards."""
    from dev_flow_agent.workflow.review import axes_for, review_tier

    small = review_tier(["src/one.py"], changed_lines=10)
    assert small == "light"
    assert [a["name"] for a in axes_for(small)] == ["correctness"]

    sprawling = review_tier([f"src/f{i}.py" for i in range(40)], changed_lines=900)
    assert sprawling == "full"
    assert len(axes_for(sprawling)) == 5


def test_a_sensitive_path_summons_its_specialist_whatever_the_size():
    from dev_flow_agent.workflow.review import _specialists, axes_for, review_tier

    files = ["src/main/java/com/x/auth/TokenStore.java"]
    tier = review_tier(files, changed_lines=5)
    assert tier == "standard", "a one-line change to auth is not a light review"
    names = [a["name"] for a in axes_for(tier, specialists=_specialists(files))]
    assert "security" in names


def _clone_with_trunk(tmp_path):
    """An origin with a trunk and a feature branch, cloned the way the
    workspace clones: one branch, shallow."""
    import subprocess

    origin = tmp_path / "origin"
    origin.mkdir()
    run = lambda *a: subprocess.run(a, check=True, capture_output=True)
    run("git", "init", "-q", "-b", "master", str(origin))
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        run("git", "-C", str(origin), "config", k, v)
    (origin / "README.md").write_text("trunk\n")
    run("git", "-C", str(origin), "add", "-A")
    run("git", "-C", str(origin), "commit", "-qm", "on trunk")
    run("git", "-C", str(origin), "checkout", "-q", "-b", BRANCH)
    (origin / "feature.txt").write_text("work\n")
    run("git", "-C", str(origin), "add", "-A")
    run("git", "-C", str(origin), "commit", "-qm", "on the branch")

    work = tmp_path / "work"
    run("git", "clone", "-q", "--no-tags", "--branch", BRANCH, "--depth", "1",
        str(origin), str(work))
    return origin, work


def test_the_checkout_has_no_trunk_until_it_is_fetched(tmp_path):
    """The workspace clones one branch at depth 1, so `origin/master` does not
    exist -- and every reviewer was told to diff against it."""
    import subprocess

    from agent_core.git import GitWorkspace

    from dev_flow_agent.workflow.review import ensure_trunk_ref

    _, work = _clone_with_trunk(tmp_path)
    refs = subprocess.run(
        ["git", "-C", str(work), "for-each-ref", "--format=%(refname)"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "origin/master" not in refs, "the clone starts without a trunk"

    ref = ensure_trunk_ref(
        {"repo_path": str(work)}, {"workspace": GitWorkspace(tmp_path / "ws")}
    )
    assert ref == "origin/master"
    merge_base = subprocess.run(
        ["git", "-C", str(work), "merge-base", "origin/master", "HEAD"],
        capture_output=True, text=True,
    )
    assert merge_base.returncode == 0, "a pending-deploy diff needs a shared commit"


def test_a_repo_with_no_trunk_reports_rather_than_guessing(tmp_path):
    from agent_core.git import GitWorkspace

    from dev_flow_agent.workflow.review import ensure_trunk_ref

    origin, work = _clone_with_trunk(tmp_path)
    import subprocess

    subprocess.run(["git", "-C", str(origin), "branch", "-D", "master"],
                   check=False, capture_output=True)
    assert ensure_trunk_ref(
        {"repo_path": str(work)}, {"workspace": GitWorkspace(tmp_path / "ws2")}
    ) == ""


def test_the_reviewers_are_given_the_run_base(tmp_path):
    from dev_flow_agent.workflow.review import run_base

    assert run_base({"commit_id": "abc123"}) == "abc123"
    assert run_base({"base_commit": "def456", "commit_id": "abc123"}) == "def456"
    assert run_base({}) == ""


def test_the_two_escalations_do_not_share_a_gate():
    """Approving an exhausted enforcement gate means "review it anyway"; the
    build escalation's approve skips a task. One gate cannot mean both, and
    after an enforcement failure there may be no pending task to skip."""
    from agent_core.workflow import WorkflowSpec

    from dev_flow_agent.workflow import FLOW_PATH

    routes = {b.source: b.routes for b in WorkflowSpec.from_file(FLOW_PATH).branches}
    assert routes["enforcement_verdict"]["escalate"] == "escalate_enforcement"
    assert routes["escalate_enforcement"] == {
        "approve": "review_axes",
        "reject": "abandon_build",
    }
    assert routes["escalate_build"]["approve"] == "skip_task"


def test_the_enforcement_gate_only_stands_for_blf_work():
    """A repo with no issue key has no enforcement gate to answer to."""
    from dev_flow_agent.workflow.build import more_tasks

    done = {"build_done": True}
    assert more_tasks({**done, "branch": "TICKET-100-20260910"}) == "enforce"
    assert more_tasks({**done, "branch": "main"}) == "review"
    assert more_tasks({**done, "jira": "ABC-1", "branch": "main"}) == "enforce"


def test_a_failed_gate_sends_work_back_and_then_stops(harness):
    """Bounded: the gate may send work back, but a gate that never gives up is
    a loop, not a gate."""
    from dev_flow_agent.workflow.build import MAX_ENFORCE_ROUNDS, enforcement_verdict

    failing = {"enforcement_md": "ENFORCEMENT: fail"}
    assert enforcement_verdict({**failing, "enforcement_round": 0}, {}, {})["enforcement_outcome"] == "fix"
    assert enforcement_verdict(
        {**failing, "enforcement_round": MAX_ENFORCE_ROUNDS - 1}, {}, {}
    )["enforcement_outcome"] == "escalate"

    # A gate that reports nothing has not passed.
    assert enforcement_verdict({"enforcement_md": "it went fine"}, {}, {})["enforcement_outcome"] == "fix"


def test_a_gate_that_could_not_run_stops_on_the_first_round(harness):
    """A block is not a worse fail. `wmq-api:2.1.5` is published nowhere, so
    the build cannot resolve, and sending that back to the build spends a full
    Maven run per round proving the same thing three times."""
    from dev_flow_agent.workflow.build import enforcement_verdict

    blocked = {"enforcement_md": "no repository serves it\n\nENFORCEMENT: blocked"}
    assert enforcement_verdict({**blocked, "enforcement_round": 0}, {}, {})[
        "enforcement_outcome"
    ] == "escalate"


def test_repair_work_joins_the_task_tree_with_its_origin(harness):
    from dev_flow_agent.workflow.build import queue_repair

    tasks, task_id = harness["tasks"], harness["task_id"]
    tasks.replace_plan_tasks(task_id, [{"title": "A"}])
    context = {"task_store": tasks, "runtime_store": harness["runtime"]}

    queue_repair(
        {"task_ref": task_id, "enforcement_md": "the gate said no", "enforcement_round": 2},
        {"origin": "enforcement", "title": "Fix the test-enforcement gate", "body_key": "enforcement_md"},
        context,
    )
    added = tasks.plan_tasks(task_id)[-1]
    assert added["origin"] == "enforcement"
    assert "round 2" in added["title"]
    assert added["body"] == "the gate said no"


def test_a_document_that_quotes_its_own_instructions_is_read_from_its_answer():
    """All three declarations share this: the prompt asks for a verdict line
    and shows an example, so the document usually contains the example above
    its answer. The last one wins."""
    from dev_flow_agent.declarations import declaration
    from dev_flow_agent.triage import parse_kind

    quoted = (
        "End with one of:\n\n```\nVERDICT: clean\nVERDICT: critical\n```\n\n"
        "## Findings\n\n**Critical** a real one.\n\nVERDICT: critical\n"
    )
    assert declaration(quoted, "VERDICT", ("clean", "critical")) == "critical"

    triage = "Choose one:\nKIND: feature-dev\n\n...analysis...\n\nKIND: bug-fix\n"
    assert parse_kind(triage) == "bug-fix"

    assert declaration("no line at all", "ENFORCEMENT", ("pass", "fail")) is None


def test_a_resumed_run_still_gets_the_mirror(harness, tmp_path):
    """Written on every pass, not once after the checkout. A run sent back to
    the build resumes below the nodes that ran at the start, so a key written
    only there is missing for exactly the runs that loop -- which is how the
    gate came to run without the mirror and call a resolvable branch blocked."""
    from dev_flow_agent.workflow.build import next_task

    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    tasks, task_id = harness["tasks"], harness["task_id"]
    tasks.replace_plan_tasks(task_id, [{"title": "A", "body": "b"}])
    context = {"task_store": tasks, "maven_central_mirror_url": "https://nexus.example/pub/"}

    state = {"task_ref": task_id, "repo_path": str(repo), "branch": "TICKET-1-20260101"}
    assert next_task(state, {}, context)["maven_settings"].endswith(
        "central-mirror-settings.xml"
    )

    # ...and on the pass that finds no work left, which is the one that runs
    # the enforcement gate.
    tasks.set_plan_task(task_id, 1, status="done")
    done = next_task(state, {}, context)
    assert done["build_done"] is True
    assert done["maven_settings"].endswith("central-mirror-settings.xml")


def test_the_mirror_settings_are_kept_out_of_the_branch(tmp_path):
    """A repair turn committed `.dfa_cache/maven/central-mirror-settings.xml`
    to the branch it was building, and it then showed up in the branch's own
    diff. The file belongs beside the turn, not inside the checkout."""
    from dev_flow_agent.workflow.git import exclude_agent_noise
    from dev_flow_agent.workflow.maven import settings_for

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    turn = tmp_path / "turn"
    turn.mkdir()

    path = settings_for(
        {"repo_path": str(repo), "turn_dir": str(turn)},
        {"maven_central_mirror_url": "https://nexus.example/pub/"},
    )
    assert str(repo) not in path

    # ...and a checkout that already has one does not carry it into a commit.
    exclude_agent_noise(repo)
    excluded = (repo / ".git" / "info" / "exclude").read_text()
    assert ".dfa_cache/" in excluded
    assert ".agent_cache/" in excluded


def test_a_run_that_edits_the_gate_stops_for_a_person(harness, tmp_path, monkeypatch):
    """A repair turn replaced PIT's `${targetClasses}` -- the value filter-diff
    computes from the changed code -- with a class name it chose, and added its
    own targetTests and Surefire include. That is narrowing the gate, not
    passing it, and no verdict the gate then reports means anything."""
    from dev_flow_agent.workflow import build

    edited = (
        "--- a/pom.xml\n"
        "+++ b/pom.xml\n"
        "-<targetClasses><param>${targetClasses}</param></targetClasses>\n"
        "+<targetClasses><param>com.example.TheOneIPass</param></targetClasses>\n"
        "+<skipTests>false</skipTests>\n"
    )

    class _Workspace:
        def output(self, repo, *args):
            return edited

    state = {"repo_path": str(tmp_path), "commit_id": "base1", "enforcement_md": "ENFORCEMENT: pass"}
    context = {"workspace": _Workspace()}

    out = build.enforcement_verdict(state, {}, context)
    assert out["enforcement_outcome"] == "escalate"
    assert any("targetClasses" in line for line in out["enforcement_tampered"])

    # Ordinary work is left alone: a pass is still a pass.
    class _Clean(_Workspace):
        def output(self, repo, *args):
            return (
                "--- a/provider/pom.xml\n+++ b/provider/pom.xml\n"
                "+<dependency><artifactId>guava</artifactId></dependency>\n"
            )

    clean = build.enforcement_verdict(state, {}, {"workspace": _Clean()})
    assert clean["enforcement_outcome"] == "pass"


def test_the_gates_own_output_settles_the_verdict(harness):
    """A turn read a gate that passed at 100% as blocked, three rounds running.
    The lines that carry the verdict are printed by the tools, so the pipeline
    reads those rather than the prose about them."""
    from dev_flow_agent.workflow.build import enforcement_verdict

    passing = (
        "[test-enforcer] no changed Java source lines for ripple-inspection-core.common\n"
        "[test-enforcer] diff line coverage 100.00% passed for core.provider (11/11)\n"
        "Tests run: 26, Failures: 0, Errors: 0, Skipped: 0\n"
        ">> Generated 28 mutations Killed 28 (100%)\n"
        "BUILD SUCCESS\n\n"
        "ENFORCEMENT: blocked\n"
    )
    out = enforcement_verdict({"enforcement_md": passing}, {}, {})
    assert out["enforcement_outcome"] == "pass"

    # ...and the other direction: tests skipped by the parent POM's default,
    # coverage read off a stale jacoco.exec. Maven says BUILD SUCCESS and the
    # gate measured nothing, so a declared pass does not stand.
    vacuous = (
        "[INFO] Skipping project because: Test execution should be skipped (-DskipTests).\n"
        "[test-enforcer] diff line coverage 100.00% passed for core.provider (11/11)\n"
        "BUILD SUCCESS\n\n"
        "ENFORCEMENT: pass\n"
    )
    stands = enforcement_verdict({"enforcement_md": vacuous}, {}, {})
    assert stands["enforcement_outcome"] == "fix"


def test_a_review_gates_on_its_findings_not_on_its_verdict_line(harness):
    """The verdict is a consequence of the findings, in both directions: a
    review whose worst finding is a nice-to-have is clean however it signs
    off, and one that found something in the change is not clean however it
    signs off."""
    from dev_flow_agent.workflow.build import review_verdict

    def review(severity, verdict):
        return (
            "## Correctness\n\n## Findings\n\n"
            f"- **{severity}** `provider/src/main/java/Thing.java:174`"
            " dereferences every element without a null check.\n\n"
            f"VERDICT: {verdict}\n"
        )

    context = {"workspace": None}
    # Signed off as critical with nothing above a nice-to-have behind it.
    assert review_verdict({"review_md": review("Nice-to-have", "critical")}, {}, context)[
        "review_outcome"
    ] == "clean"
    # Signed off as clean with an Important finding in the change behind it.
    assert review_verdict({"review_md": review("Important", "clean")}, {}, context)[
        "review_outcome"
    ] == "fix"


def test_a_critical_outside_the_change_does_not_hold_the_run(harness, tmp_path):
    """A real problem in code this branch never touched is a note for a person,
    not work to hand the build -- it cannot fix what it did not write."""
    from dev_flow_agent.workflow.build import review_verdict

    class _Workspace:
        def output(self, repo, *args):
            return "provider/src/main/java/com/example/Touched.java\n"

    state = {"repo_path": str(tmp_path), "commit_id": "base1"}
    context = {"workspace": _Workspace()}

    elsewhere = (
        "## Findings\n\n"
        "- **Critical** `service/src/main/java/com/example/Untouched.java:88` leaks a handle.\n\n"
        "VERDICT: critical\n"
    )
    assert review_verdict({**state, "review_md": elsewhere}, {}, context)["review_outcome"] == "clean"

    inside = (
        "## Findings\n\n"
        "- **Critical** `provider/src/main/java/com/example/Touched.java:12` drops the error.\n\n"
        "VERDICT: critical\n"
    )
    assert review_verdict({**state, "review_md": inside}, {}, context)["review_outcome"] == "fix"


def test_the_review_reads_the_branch_not_the_stale_checkout(harness, tmp_path):
    """`base..HEAD` is two dots from the commit this checkout started at, so
    everything merged into trunk since then reads as this branch's work. One
    cr-fix job reviewed 36 files that way -- a service and thirty test classes
    it never touched -- where the branch adds 5."""
    from dev_flow_agent.workflow.review import changed_paths, review_range

    seen = []

    class _Workspace:
        def output(self, repo, *args):
            seen.append(args)
            return "provider/src/main/java/Touched.java\n"

    state = {"repo_path": str(tmp_path), "commit_id": "stale1", "review_trunk": "origin/master"}
    assert review_range(state, {}) == "origin/master...HEAD"
    changed_paths(state, {"workspace": _Workspace()})
    assert seen[-1] == ("diff", "--name-only", "origin/master...HEAD")

    # With no trunk to fork from, the run range stands in -- and the prompt
    # says out loud that it may hold more than this run wrote.
    no_trunk = {"repo_path": str(tmp_path), "commit_id": "stale1"}
    assert review_range(no_trunk, {}) == "stale1..HEAD"


def test_the_review_reads_the_runs_own_commits(harness, tmp_path):
    """Not the branch and not the checkout: a person may have committed to the
    branch before the run started, and trunk moves under the checkout. The run
    records what it commits, and that is what gets reviewed."""
    from dev_flow_agent.workflow.build import _recorded
    from dev_flow_agent.workflow.review import changed_paths, review_range

    state = {"repo_path": str(tmp_path), "commit_id": "stale1", "review_trunk": "origin/master"}
    # Nothing committed yet: the branch-versus-trunk range stands in.
    assert review_range(state, {}) == "origin/master...HEAD"

    state.update(_recorded(state, "aaa111"))
    state.update(_recorded(state, "bbb222"))
    assert state["run_commits"] == ["aaa111", "bbb222"]
    assert review_range(state, {}) == "aaa111^..bbb222"

    seen = []

    class _Workspace:
        def output(self, repo, *args):
            seen.append(args)
            return "provider/src/main/java/Touched.java\n"

    changed_paths(state, {"workspace": _Workspace()})
    assert seen[-1] == ("diff", "--name-only", "aaa111^..bbb222")

    # Recording is idempotent: a node that runs twice on resume does not
    # record the same commit twice and shift the range.
    again = _recorded(state, "bbb222")
    assert again["run_commits"] == ["aaa111", "bbb222"]


def test_a_commit_covers_every_repository_the_run_has(tmp_path):
    """A change that only makes sense together lands together -- and a turn's
    work in the second repository would otherwise be thrown away by the next
    `clean -fd`."""
    from dev_flow_agent.workflow.git import commit_and_push

    committed = []

    class _Workspace:
        def commit_all_and_push(self, repo, **kw):
            committed.append((str(repo), kw["message"]))
            return "sha-primary" if repo.name == "first" else "sha-other"

    first = tmp_path / "first"
    (first / ".git").mkdir(parents=True)
    other = tmp_path / "other"
    (other / ".git").mkdir(parents=True)

    state = {
        "repo_path": str(first),
        "branch": "TICKET-1-20260915",
        "repos": [
            {"name": "first", "path": str(first), "primary": True},
            {"name": "other", "path": str(other), "primary": False},
        ],
    }
    sha = commit_and_push(state, {"workspace": _Workspace()}, message="task 1")
    assert [path for path, _ in committed] == [str(first), str(other)]
    # The task's own repository is the one whose sha identifies the work.
    assert sha == "sha-primary"


def test_the_work_prompts_say_which_repositories_are_checked_out(tmp_path):
    from agent_core.prompts import PromptLibrary

    from dev_flow_agent.workflow import PROMPT_DIR

    library = PromptLibrary(PROMPT_DIR)
    values = {
        "title": "t",
        "spec_md": "s",
        "design_md": "d",
        "task_seq": 1,
        "task_title": "Add it",
        "task_body": "body",
        "task_origin": "plan",
        "jira": "TICKET-1",
        "maven_settings": "",
        "repos": [
            {"name": "first", "path": "/w/first", "primary": True},
            {"name": "sample_service", "path": "/w/sample", "primary": False},
        ],
    }
    out = library.render("build_task.md.j2", values)
    assert "This change spans more than one repository" in out
    assert "/w/sample" in out

    # One repository, no paragraph about repositories.
    alone = library.render("build_task.md.j2", {**values, "repos": [values["repos"][0]]})
    assert "This change spans more than one repository" not in alone


def test_deleting_tests_to_pass_the_gate_stops_for_a_person(tmp_path):
    """A gate that fails can be answered by fixing the work or by deleting
    what noticed. One run took the second route -- four harnesses present on
    origin/master, removed "so the enforcement gate can pass" -- and the gate
    then had nothing left to measure."""
    from dev_flow_agent.workflow.build import deleted_tests, enforcement_verdict

    class _Workspace:
        def __init__(self, out):
            self.out = out

        def output(self, repo, *args):
            return self.out if "--diff-filter=D" in args else ""

    removed = (
        "provider/src/test/java/com/example/LiveHarnessTest.java\n"
        "provider/src/test/java/com/example/OtherIT.java\n"
    )
    state = {"repo_path": str(tmp_path), "commit_id": "base1", "enforcement_md": "ENFORCEMENT: pass"}

    assert len(deleted_tests(state, {"workspace": _Workspace(removed)})) == 2
    out = enforcement_verdict(state, {}, {"workspace": _Workspace(removed)})
    assert out["enforcement_outcome"] == "escalate"
    assert any("LiveHarnessTest" in line for line in out["enforcement_tampered"])

    # Deleting production code is the work's business, not the gate's.
    production = "provider/src/main/java/com/example/Thing.java\n"
    assert deleted_tests(state, {"workspace": _Workspace(production)}) == []


def test_installing_the_gate_is_allowed_but_narrowing_it_is_not(tmp_path):
    """A project with no `test-enforcement` profile has not opted out; it has
    not been onboarded. The usage guide's answer is a parent upgrade, which is
    a POM change full of gate words -- and the tamper check refused it, so the
    run reported blocked instead of enforcing the code it had just changed."""
    from dev_flow_agent.workflow.build import gate_config_changed

    class _Workspace:
        def __init__(self, before):
            self.before = before

        def output(self, repo, *args):
            if args[0] == "diff" and "--name-only" in args:
                return "pom.xml\n"
            if args[0] == "show":
                return self.before
            return (
                "--- a/pom.xml\n+++ b/pom.xml\n"
                "-    <version>1.3.90</version>\n"
                "+    <version>1.3.96</version>\n"
                "+    <test.enforcement.enabled>true</test.enforcement.enabled>\n"
            )

    state = {"repo_path": str(tmp_path), "commit_id": "base1"}

    # The POM had no enforcement at all: this run is onboarding the project.
    onboarding = gate_config_changed(state, {"workspace": _Workspace("<project><artifactId>x</artifactId></project>")})
    assert onboarding == []

    # The POM already answered to the gate: changing it is another matter.
    existing = "<project><profile><id>test-enforcement</id><artifactId>test-enforcer</artifactId></profile></project>"
    narrowing = gate_config_changed(state, {"workspace": _Workspace(existing)})
    assert narrowing and any("test.enforcement" in line for line in narrowing)


def test_uncovered_mutants_are_not_survivors(harness):
    """The real numbers from a run that passed and was failed anyway:

        >> Generated 23 mutations Killed 6 (26%)
        >> Mutations with no coverage 17. Test strength 100%

    Nothing survived. PIT mutates the whole targeted class, so those 17 sit on
    pre-existing lines this change never touched -- the repository's problem,
    which the rubric says is not this run's to answer for."""
    from dev_flow_agent.enforcement import read_evidence
    from dev_flow_agent.workflow.build import enforcement_verdict

    passing = (
        "[test-enforcer] diff line coverage 100.00% passed for core.service (12/12)\n"
        "Tests run: 8, Failures: 0, Errors: 0, Skipped: 0\n"
        ">> Generated 23 mutations Killed 6 (26%)\n"
        ">> Mutations with no coverage 17. Test strength 100%\n"
        "BUILD SUCCESS\n\nENFORCEMENT: pass\n"
    )
    assert read_evidence(passing).survivors() == 0
    assert enforcement_verdict({"enforcement_md": passing}, {}, {})["enforcement_outcome"] == "pass"

    # A mutant a test reached and failed to kill is the real gap.
    surviving = passing.replace(
        ">> Generated 23 mutations Killed 6 (26%)\n>> Mutations with no coverage 17.",
        ">> Generated 23 mutations Killed 6 (26%)\n>> Mutations with no coverage 5.",
    )
    assert read_evidence(surviving).survivors() == 12
    assert enforcement_verdict({"enforcement_md": surviving}, {}, {})["enforcement_outcome"] == "fix"


def test_a_gate_with_nothing_to_mutate_can_still_pass(harness):
    """`pitest.targets=0` from a healthy gate is a no-op, not a gap: having
    nothing to measure is a different thing from measuring nothing."""
    from dev_flow_agent.workflow.build import enforcement_verdict

    no_op = (
        "[test-enforcer] refs/remotes/origin/master -> target/filtered.diff, pitest.targets=0\n"
        "[test-enforcer] diff line coverage 100.00% passed for core.service (3/3)\n"
        "Tests run: 4, Failures: 0, Errors: 0, Skipped: 0\n"
        "BUILD SUCCESS\n\nENFORCEMENT: pass\n"
    )
    assert enforcement_verdict({"enforcement_md": no_op}, {}, {})["enforcement_outcome"] == "pass"


def test_the_reviewers_own_format_is_read(harness, tmp_path):
    """The reviewers write the severity and the headline inside one bold run.
    Requiring a standalone `**Important**` found nothing in a review holding
    five findings, and a review with no findings reads as clean -- so a run
    with a silent-partial-update defect in its own new code finished."""
    from dev_flow_agent.findings import read_findings
    from dev_flow_agent.workflow.build import review_verdict

    review = (
        "## Findings\n\n"
        "**Important — silent partial update if a deadlock hits mid-partition** — "
        "`service/src/main/java/com/example/ReplenishPoolServiceImpl.java:230-235` "
        "mutates the shared param while iterating partitions.\n\n"
        "**Nice-to-have — doc overstates the WARN logging** — `doc/usage.md:9`.\n\n"
        "## Pre-existing\n\n"
        "**Important — unrelated NPE** — `other/src/main/java/Old.java:12` predates this.\n\n"
        "VERDICT: clean\n"
    )
    severities = [(f.severity, f.pre_existing) for f in read_findings(review)]
    assert severities == [("important", False), ("nice-to-have", False), ("important", True)]

    class _Workspace:
        def output(self, repo, *args):
            return "service/src/main/java/com/example/ReplenishPoolServiceImpl.java\n"

    state = {"repo_path": str(tmp_path), "commit_id": "base1", "review_md": review}
    out = review_verdict(state, {}, {"workspace": _Workspace()})
    # The Important on the run's own new code sends the work back...
    assert out["review_outcome"] == "fix"

    # ...while the nice-to-have and the pre-existing one never would have.
    only_minor = review.replace("**Important — silent partial", "**Nice-to-have — silent partial")
    clean = review_verdict({**state, "review_md": only_minor}, {}, {"workspace": _Workspace()})
    assert clean["review_outcome"] == "clean"
