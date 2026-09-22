"""The issue-tracker overlay: applied to Jira work, and not invented for tooling work."""

from __future__ import annotations

from pathlib import Path

import pytest
from agent_core.prompts import PromptLibrary

from dev_flow_agent.blf import is_blf, jira_key
from dev_flow_agent.workflow import PROMPT_DIR

BRANCH = "TICKET-100-20260910"

VALUES = {
    "title": "t",
    "request": "r",
    "comments": "",
    "triage_md": "i",
    "spec_md": "s",
    "design_md": "d",
    "plan_md": "p",
    "review_md": "v",
    "task_seq": 1,
    "task_title": "Add it",
    "task_body": "body",
    "base": "3d99d97",
    "trunk": "origin/master",
    "task_origin": "plan",
}


@pytest.fixture
def library():
    return PromptLibrary(PROMPT_DIR)


def test_the_branch_says_whether_this_is_blf_work():
    assert jira_key(BRANCH) == "TICKET-100"
    assert is_blf(BRANCH)
    assert jira_key("main") is None
    assert not is_blf("feature/cleanup")


@pytest.mark.parametrize("template", ["write_spec.md.j2", "write_design.md.j2", "write_plan.md.j2"])
def test_a_document_prompt_carries_the_document_set(library, template):
    """The paths, and that an inapplicable document is still created saying
    N/A -- review and ship gates need a stable place to look."""
    out = library.render(template, {**VALUES, "jira": "TICKET-100"})
    assert "doc/spec-TICKET-100.md" in out
    assert "doc/design-TICKET-100.md" in out
    assert "doc/usage-TICKET-100.md" in out
    assert "N/A" in out
    assert "$jira-$date" in out
    assert "TICKET-100-SNAPSHOT" in out


@pytest.mark.parametrize("template", ["write_spec.md.j2", "write_design.md.j2", "write_plan.md.j2"])
def test_tooling_work_is_not_given_jira_paperwork(library, template):
    """The skill is explicit: do not force Jira or release-approval artifacts onto
    confirmed non-Jira work."""
    out = library.render(template, {**VALUES, "jira": ""})
    assert "doc/spec-" not in out
    assert "release-approval" not in out
    assert "Non-Jira tooling work" in out
    assert "docs/spec-<topic>.md" in out


def test_the_build_prompt_carries_test_enforcement(library):
    out = library.render("build_task.md.j2", {**VALUES, "jira": "TICKET-100"})
    assert "dev_gate.py pre-review --jira TICKET-100" in out
    assert "doc/test-enforce-usage.md" in out
    assert "blocker" in out
    # The Java specifics that make an enforcement result trustworthy.
    assert "test-enforcer" in out and "effective-pom" in out
    assert "-DtargetTests" in out
    assert "Java 8 `JAVA_HOME`" in out
    # Mutation results are a survivor gate, not a coverage gate.
    assert "NO_COVERAGE" in out and "SURVIVED" in out
    assert "Alibaba" in out


def test_the_build_prompt_stays_quiet_for_tooling_work(library):
    out = library.render("build_task.md.j2", {**VALUES, "jira": ""})
    assert "blf_dev_gate" not in out
    assert "NO_COVERAGE" not in out


def test_the_plan_schedules_the_documentation_work(library):
    out = library.render("write_plan.md.j2", {**VALUES, "jira": "TICKET-100"})
    assert "doc/usage-TICKET-100.md" in out
    assert "release-approval-TICKET-100.evidence.json" in out


def test_blf_work_gets_a_compliance_reviewer():
    from dev_flow_agent.workflow.review import review_axes

    blf = review_axes({"task_ref": "t", "jira": "TICKET-100", "design_md": ""}, {}, {})
    assert [a["name"] for a in blf["review_axes"]][-1] == "issue"

    plain = review_axes({"task_ref": "t", "jira": "", "design_md": ""}, {}, {})
    assert "issue" not in [a["name"] for a in plain["review_axes"]]


def test_the_compliance_axis_asks_for_the_evidence(library):
    from dev_flow_agent.workflow.review import BLF_AXIS

    out = library.render(
        "review_axis.md.j2",
        {**VALUES, "jira": "TICKET-100", "axis_title": BLF_AXIS["title"], "axis_focus": BLF_AXIS["focus"]},
    )
    assert "Issue: TICKET-100" in out
    for asked in ("test-enforcer", "doc/usage-<JIRA>.md", "$jira-$date", "Alibaba"):
        assert asked in out, asked
    assert "absent evidence is a finding" in out


def test_enforcement_instructions_are_runnable_here(library):
    """The skill's text points at dev-skills scripts, which are no more
    installed than the skills are. The repo's own command is the authority,
    and a gate that could not run is not reported as passed."""
    import re

    # Prose wraps, so match on the words rather than on the line breaks.
    out = re.sub(r"\s+", " ", library.render("build_task.md.j2", {**VALUES, "jira": "TICKET-100"}))
    assert "doc/test-enforce-usage.md" in out
    # ...but that document is not in every repo, and the profile in the POM is
    # the gate whether or not someone wrote the document describing it.
    assert "Its absence is not a finding." in out
    assert "The POM's `test-enforcement` profile, which is the gate itself" in out
    assert "Then run the command yourself" in out
    assert "Do not report a gate you could not run as passed." in out


def _repo(path):
    import subprocess

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


def _clone(origin, into):
    """A working copy with an origin, as the flow prepares one."""
    import subprocess

    subprocess.run(
        ["git", "clone", "-q", str(origin), str(into)], check=True, capture_output=True
    )
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(into), "config", k, v], check=True)
    return into


def test_a_usage_document_written_by_the_turn_is_published_with_the_design(tmp_path):
    """Folded into the design phase rather than given a stage of its own, so it
    has to ride along with the design's own commit."""
    import subprocess

    from agent_core.git import GitWorkspace

    from dev_flow_agent.workflow.publish import publish_doc

    origin = _repo(tmp_path / "origin")
    repo = _clone(origin, tmp_path / "work")
    # What the design turn does: the design comes back as text, the usage
    # document is written into the checkout.
    (repo / "doc").mkdir()
    (repo / "doc" / f"usage-{jira_key(BRANCH)}.md").write_text("# Usage\nrun it\n", encoding="utf-8")

    publish_doc(
        {
            "decision": "approve",
            "repo_path": str(repo),
            "branch": BRANCH,
            "design_md": "# Design\nthe design\n",
            "task_ref": "t",
        },
        {"artifact": "design.md", "state_key": "design_md"},
        {"workspace": GitWorkspace(tmp_path / "ws")},
    )

    listed = subprocess.run(
        ["git", "-C", str(origin), "ls-tree", "-r", "--name-only", BRANCH],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "doc/design-TICKET-100.md" in listed
    assert "doc/usage-TICKET-100.md" in listed, "the usage document must ride with the design"


def test_source_changes_still_cannot_ride_along_with_a_document(tmp_path):
    """The guard that makes the above safe: `doc/*.md` and nothing else."""
    from agent_core.git import GitWorkspace, PushPolicyError

    from dev_flow_agent.workflow.publish import publish_doc

    repo = _clone(_repo(tmp_path / "origin2"), tmp_path / "work2")
    (repo / "Main.java").write_text("changed by the agent\n", encoding="utf-8")

    with pytest.raises(PushPolicyError):
        publish_doc(
            {
                "decision": "approve",
                "repo_path": str(repo),
                "branch": BRANCH,
                "design_md": "# Design\n",
                "task_ref": "t",
            },
            {"artifact": "design.md", "state_key": "design_md"},
            {"workspace": GitWorkspace(tmp_path / "ws2")},
        )


def test_review_does_not_gate_on_a_ship_time_approval(library):
    """Release approval and the RDC `rdcDeployAllowed` flag gate shipping, not
    merging. A branch under review legitimately has neither, so asking for them
    would block work on a gate it has not reached."""
    from dev_flow_agent.workflow.review import BLF_AXIS

    out = library.render(
        "review_axis.md.j2",
        {**VALUES, "jira": "TICKET-100", "axis_title": BLF_AXIS["title"], "axis_focus": BLF_AXIS["focus"]},
    )
    assert "doc/release-approval-TICKET-100.evidence.json" not in out
    assert "Say nothing about them." in out
    # It is named only to rule it out, so a reviewer does not invent the check.
    assert "rdcDeployAllowed" in out


def test_the_writing_stages_are_told_the_approval_is_not_theirs(library):
    for template in ("write_spec.md.j2", "write_design.md.j2", "write_plan.md.j2"):
        out = library.render(template, {**VALUES, "jira": "TICKET-100"})
        assert "ship-time gate" in out
        assert "do not report it missing" in out


def test_a_gate_repair_is_told_to_verify_before_finishing(library):
    """One repair task should resolve the gate. A turn that hands back
    unverified work only produces another task saying the same thing."""
    import re

    def rendered(origin):
        return re.sub(
            r"\s+", " ",
            library.render(
                "build_task.md.j2",
                {**VALUES, "jira": "TICKET-100", "task_origin": origin},
            ),
        )

    gate = rendered("enforcement")
    assert "run the enforcement command yourself and read its result" in gate
    assert "Keep going until you have seen it pass" in gate
    assert "stop and say exactly that, with the last output you saw" in gate

    review = rendered("review")
    assert "Fix those, and only those" in review
    assert "Keep going until you have seen it pass" not in review

    # Ordinary planned work has nothing to re-run and is not told to.
    plan = rendered("plan")
    assert "Keep going until you have seen it pass" not in plan
    assert "Fix those, and only those" not in plan


def test_a_resumed_run_recovers_its_issue_key(tmp_path):
    """Routing derives the key from the branch, so it is always right; the
    prompts read it from state. A run resumed from a checkpoint older than the
    key would fire the gate with a prompt that had lost its rubric."""
    from dev_flow_agent.db import TaskStore
    from dev_flow_agent.workflow.build import next_task

    store = TaskStore(tmp_path / "t.db")
    store.replace_plan_tasks("t", [{"title": "A"}])
    # A checkpoint written before `jira` existed: only the branch carries it.
    out = next_task({"task_ref": "t", "branch": BRANCH}, {}, {"task_store": store})
    assert out["jira"] == "TICKET-100"

    store.set_plan_task("t", 1, status="done")
    done = next_task({"task_ref": "t", "branch": BRANCH}, {}, {"task_store": store})
    assert done["build_done"] and done["jira"] == "TICKET-100"


def test_the_gate_is_told_the_range_under_test(tmp_path, library):
    """The prompt reads the range from the state and nothing set it, so the
    gate was told to diff `None..HEAD` and spent its run explaining that this
    is not a revision range."""
    from dev_flow_agent.db import TaskStore
    from dev_flow_agent.workflow.build import next_task

    store = TaskStore(tmp_path / "t.db")
    store.replace_plan_tasks("t", [{"title": "A"}])
    store.set_plan_task("t", 1, status="done")
    out = next_task(
        {"task_ref": "t", "branch": BRANCH, "commit_id": "3d99d97"}, {}, {"task_store": store}
    )
    assert out["base"] == "3d99d97"

    rendered = library.render(
        "enforce.md.j2", {"title": "t", "jira": "TICKET-100", "base": out["base"]}
    )
    assert "git diff 3d99d97..HEAD" in rendered
    assert "None..HEAD" not in rendered


def test_maven_settings_are_written_into_the_checkout(tmp_path):
    """When Central is unreachable, a mirror of `central` is what lets the
    build resolve dependencies. A repository entry does not displace `central`."""
    from dev_flow_agent.workflow.maven import settings_for, write_settings

    repo = tmp_path / "repo"
    repo.mkdir()
    turn = tmp_path / "turn"
    turn.mkdir()

    # Outside the checkout when there is somewhere to put it: a file inside the
    # repository is a file a build turn can commit, and one did.
    outside = write_settings(str(repo), "https://nexus.example.com/public/", str(turn))
    assert str(repo) not in outside
    assert "<mirrorOf>central</mirrorOf>" in Path(outside).read_text()

    path = write_settings(str(repo), "https://nexus.example.com/public/")
    assert "<mirrorOf>central</mirrorOf>" in Path(path).read_text()
    assert "https://nexus.example.com/public/" in Path(path).read_text()

    # No mirror configured: the turn is told nothing rather than handed a
    # `-gs` pointing at a file that mirrors nowhere.
    assert write_settings(str(repo), "") == ""
    assert settings_for({"repo_path": str(repo)}, {"maven_central_mirror_url": ""}) == ""


def test_the_enforcement_prompt_passes_the_mirror_on(library):
    import re

    values = {**VALUES, "jira": "TICKET-101", "base": "origin/master"}
    out = re.sub(r"\s+", " ", library.render("enforce.md.j2", {**values, "maven_settings": "/w/gs.xml"}))
    assert "-gs /w/gs.xml" in out
    # A missing POM is a warning Maven carries on from, not a blocker.
    assert "is a warning" in out

    # Nothing configured, nothing claimed -- and no `-gs` pointing at a file
    # that mirrors nowhere.
    plain = re.sub(r"\s+", " ", library.render("enforce.md.j2", {**values, "maven_settings": ""}))
    assert "/w/gs.xml" not in plain
    assert "Pass `-gs" not in plain


def test_the_gate_prompt_says_how_to_read_a_reactor(library):
    """The gate passed -- 100% diff coverage on 11 changed lines in `provider`
    -- and the turn reported it blocked: it read the four unchanged modules'
    "no changed Java source lines" and generalised them, never seeing the line
    that carried the verdict."""
    import re

    out = re.sub(
        r"\s+", " ",
        library.render("enforce.md.j2", {**VALUES, "jira": "TICKET-101", "base": "origin/master"}),
    )
    assert "is the normal, correct output for every module your change does not touch" in out
    assert "The verdict comes from the module that owns the changed code" in out
    # ...and the gate is not something the run may edit to suit itself.
    assert "You may not change what the gate measures you by" in out
    # ...and a timed-out minion is how PIT kills a hanging mutant, not a gap:
    # judging by that warning is the same misreading in a new place.
    assert "is *how that mutant is killed*" in out
    assert "Judge by the statistics block" in out


def test_the_review_prompt_keeps_findings_to_the_change(library):
    """A reviewer flagged a null check in a file the branch never touched, and
    called the review critical over it: three repair rounds the build could not
    finish, and then a human gate."""
    import re

    out = re.sub(
        r"\s+", " ",
        library.render(
            "review_axis.md.j2",
            {
                "axis_title": "Correctness",
                "axis_focus": "does it work",
                "base": "abc123",
                "trunk": "origin/master",
                "jira": "TICKET-101",
                "title": "t",
                "plan_md": "p",
            },
        ),
    )
    assert "Review the change, not the file it lives in" in out
    assert "belong under a `## Pre-existing` heading and are never Critical" in out
    assert "a review whose worst finding is Important is `clean`" in out


def test_the_gate_is_told_which_repositories_changed(library):
    """The gate must run where the code is. One run declared a second
    repository, built nothing in it, and enforced the first -- which had no
    production change, reported `pitest.targets=0`, and passed on the strength
    of measuring nothing."""
    import re

    values = {**VALUES, "jira": "TICKET-101", "base": "abc123", "maven_settings": ""}
    out = re.sub(
        r"\s+", " ",
        library.render(
            "enforce.md.j2",
            {
                **values,
                "enforce_in": [
                    {"name": "baseinfo_core", "path": "/w/baseinfo",
                     "changed": ["service/src/main/java/A.java"]}
                ],
            },
        ),
    )
    assert "/w/baseinfo" in out
    assert "The verdict is the worst of them" in out
    assert "one repository passing does not cover another that was never gated" in out

    # One repository, nothing to say about where.
    alone = re.sub(r"\s+", " ", library.render("enforce.md.j2", {**values, "enforce_in": []}))
    assert "Where to run it" not in alone


def test_the_gate_scopes_itself_to_the_change_not_the_repository(library):
    """A repo defaulting to skipTests=true has tests nobody has run in a long
    time, and some fail. One run spent its rounds on them and then deleted
    four, producing a green gate over an unwritten fix."""
    import re

    out = re.sub(
        r"\s+", " ",
        library.render("build_task.md.j2", {**VALUES, "jira": "TICKET-101", "maven_settings": ""}),
    )
    assert "You answer for your own tests, not for the repository's" in out
    assert "-Dsurefire.failIfNoSpecifiedTests=false -DfailIfNoTests=false" in out
    # Selecting your own tests is not narrowing the gate; editing the POM is.
    assert "Selecting your own tests on the command line is not narrowing the gate" in out
    # A failure outside the scope has to be proven pre-existing, then recorded.
    assert "run that same test at the base ref and show it failing there too" in out
    assert "Never delete, `@Ignore`, or comment out a test to make a gate pass" in out


def test_the_prompt_says_to_install_the_gate_rather_than_report_blocked(library):
    import re

    out = re.sub(
        r"\s+", " ",
        library.render("enforce.md.j2", {**VALUES, "jira": "TICKET-1", "base": "abc", "maven_settings": ""}),
    )
    assert "has not opted out of enforcement" in out
    assert "This is the one POM change the gate welcomes" in out
    # The guide is fetched, not copied: a prompt that pins a parent version is
    # wrong the day after it is written.
    guided = re.sub(
        r"\s+", " ",
        library.render(
            "enforce.md.j2",
            {**VALUES, "jira": "TICKET-1", "base": "abc", "maven_settings": "",
             "enforcement_guide": "https://git.example.com/raw/guide.md"},
        ),
    )
    assert "https://git.example.com/raw/guide.md" in guided
    assert "do not copy a version number out of this prompt" in guided
    assert "1.0.21" not in guided and "1.3.96" not in guided
