"""Building the plan: one task at a time, reviewed, then simplified.

The stages above this one produce documents for a human to approve. These
produce code, and run unattended -- a person is asked only when the review
cannot get itself to clean. Two things make that safe enough to leave alone:
every task is its own commit, so a run that stops leaves finished work
durable and legible; and the review's own Critical findings are what escalate,
so the pipeline cannot quietly publish something it judged unsound.

The plan's tasks are the unit of progress. A build that ran as one long turn
would be a black box for an hour; task by task, the page can say where it is.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from dev_flow_agent.declarations import declaration
from dev_flow_agent.enforcement import disagreement, read_evidence
from dev_flow_agent.workflow.git import commit_and_push, run_base
from dev_flow_agent.config import Settings
from dev_flow_agent.workflow.maven import settings_for

#: `## Task 3: Add the retry button` -- the shape write_plan.md.j2 asks for.
TASK_HEADING = re.compile(r"^##\s+Task\s+(\d+)\s*:\s*(.+?)\s*$", re.MULTILINE)

#: How many times the reviewer may send findings back before a human is asked.
MAX_REVIEW_ROUNDS = 3

#: How many times one task may fail before the run stops for a human.
MAX_TASK_ATTEMPTS = 2

#: How many times the enforcement gate may send work back before a human is
#: asked. A gate that never gives up is a loop, not a gate.
MAX_ENFORCE_ROUNDS = 3



#: What a reviewer may declare, and what the enforcement gate may.
VERDICTS = ("clean", "critical")

#: `blocked` is not a worse `fail`. A fail is a gap in the work, which the next
#: build turn can close. A block is the gate never running -- a dependency that
#: resolves nowhere, the plugin absent, no toolchain -- and no amount of
#: building closes that, so it goes to a person on the first round rather than
#: after three full Maven attempts prove the same thing three times.
ENFORCEMENTS = ("pass", "fail", "blocked")

#: Fallback when the verdict line is missing: a severity *marker*, not the bare
#: word. "No Critical findings" contains "critical" and means the opposite.
CRITICAL_MARKER = re.compile(
    r"(\*\*critical\*\*|severity:\s*critical|^\s*[-*]\s*critical\b)",
    re.IGNORECASE | re.MULTILINE,
)


def parse_tasks(plan_md: str) -> List[Dict[str, str]]:
    """Split a plan into its tasks, each with the text under its heading."""
    text = plan_md or ""
    # Any heading ends a task's body, not just the next task's: a plan puts
    # checkpoints and a coverage matrix between and after them.
    boundaries = [m.start() for m in re.finditer(r"^##\s", text, re.MULTILINE)]
    tasks = []
    for match in TASK_HEADING.finditer(text):
        end = next((b for b in boundaries if b > match.end()), len(text))
        tasks.append({"title": match.group(2), "body": text[match.end() : end].strip()})
    return tasks


def _announce(context, task_ref: str, *, seq: int, status: str, title: str = "", sha=None) -> None:
    """Say a task changed, so the page does not need reloading to find out.

    The tree is the progress view for a build that runs for an hour, and it was
    rendered with the page: every status change was invisible until someone
    reloaded.
    """
    runtime = context.get("runtime_store")
    if runtime is None:
        return
    runtime.append_event(
        task_ref=task_ref,
        event_type="plan_task",
        severity="error" if status == "failed" else "info",
        # The build's step owns these, so the page can file them under it.
        stage="build",
        message=f"task {seq}: {status}",
        payload={"seq": seq, "status": status, "title": title, "commit_sha": sha},
    )


def _store(context: Mapping[str, Any]):
    store = context.get("task_store")
    if store is None:
        raise RuntimeError("build nodes need context['task_store']")
    return store


def plan_tasks(state, config, context) -> Dict[str, Any]:
    """Turn the approved plan into the task tree the build walks.

    Lean work arrives here with no plan at all, and a plan can also come back
    without task headings. Either way the build needs one unit of work, or the
    loop finds nothing to do and the run reviews a branch nobody changed.
    """
    task_id = str(state.get("task_ref") or "")
    tasks = parse_tasks(str(state.get("plan_md") or ""))
    if not tasks:
        tasks = [
            {
                "title": str(state.get("title") or "the change"),
                "body": str(state.get("spec_md") or ""),
            }
        ]
    _store(context).replace_plan_tasks(task_id, tasks)
    return {"task_count": len(tasks), "review_round": 0}


def next_task(state, config, context) -> Dict[str, Any]:
    """Load the next unfinished task, or report that the build is done.

    Also writes the issue key back into the state. The routing derives it from
    the branch, so it is always right; the *prompts* read it from state, and a
    run resumed from a checkpoint older than the key would fire the gate with
    a prompt that had lost its rubric.
    """
    from dev_flow_agent.issue import issue_key

    task_id = str(state.get("task_ref") or "")
    jira = issue_key(state)
    # The enforcement prompt asks for the range under test, and reads it from
    # the state: without this it rendered `git diff None..HEAD`, and the gate
    # spent its run explaining that this is not a revision range.
    base = run_base(state)
    # Same reason as `jira`: written every pass, because a resumed run never
    # re-runs the nodes above it and would build without the mirror.
    mirror = settings_for(state, context)
    guide = Settings().enforcement_guide
    # Where the gate has to run: the repositories this run actually changed.
    from dev_flow_agent.workflow.repos import with_changes

    enforce_in = [
        {"name": repo["name"], "path": repo["path"], "changed": repo["changed"][:40]}
        for repo in with_changes(state, context)
    ]
    store = _store(context)
    row = store.next_plan_task(task_id)
    if row is None:
        return {
            "build_done": True,
            "task_title": "",
            "task_body": "",
            "jira": jira,
            "base": base,
            "maven_settings": mirror,
            "enforce_in": enforce_in,
            "enforcement_guide": guide,
        }
    if int(row["attempts"] or 0) >= MAX_TASK_ATTEMPTS:
        # Tried and failed enough: this is the "something special" case.
        return {
            "build_done": True,
            "build_blocked": row["title"],
            "task_title": "",
            "task_body": "",
            "jira": jira,
            "base": base,
            "maven_settings": mirror,
            "enforce_in": enforce_in,
            "enforcement_guide": guide,
        }
    store.set_plan_task(task_id, int(row["seq"]), status="running", bump_attempts=True)
    _announce(context, task_id, seq=int(row["seq"]), status="running", title=row["title"])
    return {
        "build_done": False,
        "build_blocked": "",
        "task_seq": int(row["seq"]),
        "task_title": row["title"],
        "task_body": row["body"],
        # Repair work is told to verify itself; ordinary work is not, because
        # there is nothing yet to re-run.
        "task_origin": str(row["origin"] or "plan"),
        "jira": jira,
        "base": base,
        "maven_settings": mirror,
        "enforce_in": enforce_in,
        "enforcement_guide": guide,
    }


def _recorded(state, sha: str) -> Dict[str, Any]:
    """Add one commit to the run's own record of what it wrote.

    The review needs to read this run's work and nothing else. Neither the
    checkout commit nor the trunk fork point says that: the first includes
    whatever trunk merged since, the second includes whatever a person put on
    the branch before the run started. The commits the run made are the only
    thing that says it exactly, so the run writes them down as it makes them.
    """
    if not sha:
        return {}
    made = [c for c in (state.get("run_commits") or ()) if c]
    if sha in made:
        return {"run_commits": made}
    return {"run_commits": [*made, sha]}


def commit_task(state, config, context) -> Dict[str, Any]:
    """Commit and push what the turn wrote for one task."""
    task_id = str(state.get("task_ref") or "")
    seq = int(state.get("task_seq") or 0)
    store = _store(context)
    ok = str(state.get("turn_status") or "") in ("ok", "completed")
    title = str(state.get("task_title") or "")
    if not ok:
        store.set_plan_task(task_id, seq, status="failed")
        _announce(context, task_id, seq=seq, status="failed", title=title)
        return {}

    sha = commit_and_push(state, context, message=f"{title or 'task'} (task {seq})")
    store.set_plan_task(task_id, seq, status="done", commit_sha=sha)
    _announce(context, task_id, seq=seq, status="done", title=title, sha=sha)
    return {"last_commit": sha or "", **_recorded(state, sha or "")}


def commit_changes(state, config, context) -> Dict[str, Any]:
    """Commit whatever a review fix or a simplification pass changed."""
    label = str(config.get("message") or "changes")
    sha = commit_and_push(state, context, message=label) or ""
    return {"last_commit": sha, **_recorded(state, sha)}


def queue_repair(state, config, context) -> Dict[str, Any]:
    """Turn a rejection into the next task the build will pick up.

    Repair is work like any other, so it joins the task list rather than
    running beside it -- which means a fix is built, committed, and put back
    through the same enforcement gate and the same reviewers that rejected it.
    A fix that skipped the gate would be a fix nobody checked.
    """
    origin = str(config.get("origin") or "review")
    source = str(state.get(str(config.get("body_key") or "review_md")) or "")
    round_number = int(state.get(f"{origin}_round") or 0)
    title = str(config.get("title") or "Address findings")
    seq = _store(context).append_plan_task(
        str(state.get("task_ref") or ""),
        title=f"{title} (round {round_number})" if round_number > 1 else title,
        body=source,
        origin=origin,
    )
    _announce(
        context,
        str(state.get("task_ref") or ""),
        seq=seq,
        status="pending",
        title=title,
    )
    return {"build_done": False}


#: What makes a POM line part of the gate rather than part of the build. A turn
#: that changes any of these is changing what it is measured by.
GATE_MARKERS = (
    "test.enforcement",
    "test-enforcer",
    "pitest",
    "targetclasses",
    "targettests",
    "jacoco",
    "skiptests",
    "maven.test.skip",
    "surefire",
    "failifnotests",
)


def gate_config_changed(state, context) -> List[str]:
    """POM lines this run changed that belong to the enforcement gate.

    A build turn may add tests and change production code. It may not change
    what the gate selects, what it requires, or whether it runs -- and one did:
    it replaced PIT's `${targetClasses}`, which `filter-diff` computes from the
    changed code, with a literal class name, then added its own `targetTests`
    and a Surefire include. That is not passing the gate, it is narrowing it,
    and the prose rule against it is only prose.
    """
    workspace = context.get("workspace")
    repo_path = state.get("repo_path")
    if workspace is None or not repo_path:
        return []
    base = run_base(state)
    if not base:
        return []
    repo = Path(str(repo_path))
    try:
        names = workspace.output(repo, "diff", "--name-only", f"{base}..HEAD", "--", "*pom.xml")
    except Exception:  # noqa: BLE001 - a diff we cannot read is not a verdict
        return []

    touched = []
    for name in (line.strip() for line in names.splitlines()):
        if not name:
            continue
        # Installing the gate is not narrowing it. A project that never had
        # enforcement has to get it from somewhere, and the usage guide's
        # answer is a parent upgrade -- which is a POM change full of gate
        # words. Only a POM that *already* answered to the gate is protected.
        try:
            before = workspace.output(repo, "show", f"{base}:{name}")
        except Exception:  # noqa: BLE001 - a new POM had no gate to weaken
            continue
        lowered_before = before.lower()
        if not any(marker in lowered_before for marker in GATE_MARKERS):
            continue
        try:
            diff = workspace.output(repo, "diff", f"{base}..HEAD", "--", name)
        except Exception:  # noqa: BLE001
            continue
        for line in diff.splitlines():
            if line[:1] not in ("+", "-") or line[:3] in ("+++", "---"):
                continue
            if any(marker in line.lower() for marker in GATE_MARKERS):
                touched.append(f"{name}: {line.strip()}")
    return touched


#: A path that holds tests, in the layouts this pipeline meets.
TEST_PATHS = ("src/test/", "/test/", "tests/", "test_", "_test.", "Test.java", "Tests.java", "IT.java")


def deleted_tests(state, context) -> List[str]:
    """Test files this run removed that it did not write.

    A gate that fails can be answered by fixing the work or by deleting what
    noticed. One run took the second route: four live-environment harnesses,
    present on `origin/master` and invisible only because the repo defaults to
    `skipTests=true`, were deleted with the message "so the enforcement gate
    can pass". The gate then reported nothing to measure, and the report said
    pass.

    Removing a test is sometimes right -- but it is a decision for a person,
    not a way past a gate, so it stops the run for one.
    """
    workspace = context.get("workspace")
    repo_path = state.get("repo_path")
    if workspace is None or not repo_path:
        return []
    base = run_base(state)
    if not base:
        return []
    try:
        diff = workspace.output(
            Path(str(repo_path)), "diff", "--diff-filter=D", "--name-only", f"{base}..HEAD"
        )
    except Exception:  # noqa: BLE001 - a diff we cannot read is not a verdict
        return []
    gone = []
    for line in diff.splitlines():
        path = line.strip()
        if path and any(mark in path for mark in TEST_PATHS):
            gone.append(path)
    return gone


def enforcement_verdict(state, config, context) -> Dict[str, Any]:
    """What the enforcement gate's own report means for the run."""
    round_number = int(state.get("enforcement_round") or 0) + 1
    report = str(state.get("enforcement_md") or "")
    # A gate that reported nothing has not passed.
    declared = declaration(report, "ENFORCEMENT", ENFORCEMENTS)
    # What the gate found is not a matter of opinion. Where its own output
    # settles the question, that is the verdict -- a turn read a gate that
    # passed at 100% as blocked, three rounds running, and the other direction
    # would have shipped a build whose tests never ran.
    evidence = read_evidence(report)
    found = evidence.verdict()
    note = ""
    if found is not None and found != declared:
        note = disagreement(declared, evidence)
        declared = found
    elif found is None and declared == "pass" and evidence.from_maven:
        # The output does not show a pass -- no test ran, or no mutant did --
        # and a pass nobody can point at is the one outcome this gate exists to
        # prevent. The parent POM skips tests by default, so this is the shape
        # a vacuous green arrives in.
        note = "report said pass; its own output does not show tests and mutants run"
        declared = "fail"
    runtime = context.get("runtime_store")
    if runtime is not None and note:
        runtime.append_event(
            task_ref=str(state.get("task_ref") or ""),
            event_type="agent_progress",
            severity="warning",
            stage="enforce",
            message=f"enforcement {note}",
        )
    # Before believing any verdict: a gate the run edited is not a gate. This
    # escalates rather than failing, because narrowing the gate is a judgement
    # call a person should see, not work to hand back to the next build turn.
    tampered = gate_config_changed(state, context) + [
        f"deleted {path}" for path in deleted_tests(state, context)
    ]
    if tampered:
        return {
            "enforcement_round": round_number,
            "enforcement_outcome": "escalate",
            "enforcement_tampered": tampered[:20],
        }
    if declared == "pass":
        return {"enforcement_round": round_number, "enforcement_outcome": "pass"}
    # Sending a blocked gate back to the build would spend a full Maven run
    # rediscovering something the build cannot fix.
    if declared == "blocked" or round_number >= MAX_ENFORCE_ROUNDS:
        return {"enforcement_round": round_number, "enforcement_outcome": "escalate"}
    return {"enforcement_round": round_number, "enforcement_outcome": "fix"}


def enforcement_outcome(state) -> str:
    outcome = str((state or {}).get("enforcement_outcome") or "")
    if not outcome:
        raise KeyError("enforcement_outcome: the gate has not reported")
    return outcome


def review_verdict(state, config, context) -> Dict[str, Any]:
    """Decide what the review's own findings mean for the run.

    The verdict is a consequence of the findings, not a line the reviewer
    writes beside them. One axis reported a single **Important** finding and
    declared `VERDICT: critical`, and the run spent its rounds on it; the
    finding was about a null check in a file the branch never touched, which
    no build turn could have fixed.
    """
    from dev_flow_agent.findings import disagreement as review_disagreement
    from dev_flow_agent.findings import verdict as findings_verdict
    from dev_flow_agent.workflow.review import changed_paths

    round_number = int(state.get("review_round") or 0) + 1
    review = str(state.get("review_md") or "")
    declared = declaration(review, "VERDICT", VERDICTS)
    changed = changed_paths(state, context)
    found = findings_verdict(review, changed)
    note = review_disagreement(declared, review, changed)
    if note:
        runtime = context.get("runtime_store")
        if runtime is not None:
            runtime.append_event(
                task_ref=str(state.get("task_ref") or ""),
                event_type="agent_progress",
                severity="warning",
                stage="review",
                message=f"review {note}",
            )
    critical = found == "critical"
    if not critical:
        return {"review_round": round_number, "review_outcome": "clean"}
    if round_number >= MAX_REVIEW_ROUNDS:
        return {"review_round": round_number, "review_outcome": "escalate"}
    return {"review_round": round_number, "review_outcome": "fix"}


def workflow_kind(state) -> str:
    """Which path this run takes, from the kind triage settled on.

    Two routes, not three: bug-fix and cr-fix differ in where the analysis came
    from, not in how much of the pipeline they need.
    """
    from dev_flow_agent.triage import is_lean

    return "lean" if is_lean(str(state.get("workflow_kind") or "")) else "feature-dev"


def more_tasks(state) -> str:
    """Where the build goes next: another task, a human, or the review.

    The keys are words rather than yes/no because YAML reads those as booleans,
    and the route map would be keyed `True`/`False` while the selector returned
    a string.
    """
    from dev_flow_agent.issue import issue_key

    if state.get("build_blocked"):
        return "blocked"
    if not state.get("build_done"):
        return "build"
    # issue-tracked work answers to the enforcement gate before anyone reviews it: a
    # reviewer reading code that never passed its own tests is reading a draft.
    return "enforce" if issue_key(state) else "review"


def review_outcome(state) -> str:
    outcome = str((state or {}).get("review_outcome") or "")
    if not outcome:
        raise KeyError("review_outcome: the verdict node has not run")
    return outcome


def skip_task(state, config, context) -> Dict[str, Any]:
    """Set aside the task a human agreed to leave unbuilt, and carry on.

    Approving at the build gate means "go on without it", so the task is
    recorded as skipped rather than quietly retried into the same failure.
    """
    task_id = str(state.get("task_ref") or "")
    store = _store(context)
    row = store.next_plan_task(task_id)
    if row is not None:
        store.set_plan_task(task_id, int(row["seq"]), status="skipped")
        _announce(context, task_id, seq=int(row["seq"]), status="skipped", title=row["title"])
    return {"build_blocked": ""}


def abandon_build(state, config, context) -> Dict[str, Any]:
    """Stop the run: a human looked at a failing task and said no.

    Raising is how a node ends a run as failed; there is no disposition for
    "stop here successfully", and reporting completion for a plan that was not
    built would be the wrong record.
    """
    raise RuntimeError(f"build abandoned at task: {state.get('build_blocked') or 'unknown'}")
