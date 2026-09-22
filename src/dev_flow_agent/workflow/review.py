"""The five-axis review, one agent per axis, run in parallel.

A single turn covering correctness, readability, architecture, security and
performance is one context window doing five jobs, and it goes shallow in the
predictable way: the axis the diff most obviously implicates gets the attention
and the others get a sentence. An agent per axis reads the same diff with one
question, so security is not competing with naming for room.

A sixth axis is added when the change touches an interface. Whether an API
change is compatible is not a correctness question -- the code is correct and
the consumer still breaks -- so it needs its own reader or it is not asked.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Mapping

from dev_flow_agent.issue import issue_key
from dev_flow_agent.declarations import declaration
from dev_flow_agent.workflow.git import run_base
from dev_flow_agent.workflow.build import VERDICTS

#: The standing axes, from the dev-skills `code-review-and-quality` rubric.
AXES: List[Dict[str, str]] = [
    {
        "name": "correctness",
        "title": "Correctness",
        "focus": """Does it do what the task specified? Edge cases (null, empty,
boundary), error paths and not only the happy path, off-by-one errors, race
conditions, state inconsistency. For anything touching callbacks, async work,
locks, retries, idempotency, state machines, batch jobs, downstream writes or
SQL, say what happens when it is retried or interleaved. Read the tests first:
do they test behaviour rather than implementation, and would they catch a
regression if the code changed?""",
    },
    {
        "name": "readability",
        "title": "Readability and simplicity",
        "focus": """Could another engineer follow this without the author?
Descriptive names (no bare `temp`, `data`, `result`), straightforward control
flow, no clever tricks. Could this be done in fewer lines -- 1000 where 100
suffice is a failure? Are abstractions earning their complexity, or generalising
before the third use case? Dead code, no-op variables, compatibility shims left
behind. A new conditional bolted onto an unrelated flow is a design smell, not a
nit; repeated conditionals on the same shape signal a missing model.""",
    },
    {
        "name": "architecture",
        "title": "Architecture",
        "focus": """Does it follow the existing patterns or introduce a parallel
mechanism, and if new, is that justified? Clean module boundaries, dependencies
flowing one way, duplication that wants one home. Does a refactor reduce
complexity or relocate it -- count the concepts a reader must hold. Is
feature-specific logic leaking into a shared module, and is each change made in
the module that owns the problem rather than the convenient one? When you flag a
structural problem, name the restructuring you would make.""",
    },
    {
        "name": "security",
        "title": "Security",
        "focus": """Untrusted input validated at the boundary, secrets kept out
of code and logs, authorization checked where needed, queries parameterised,
output encoded. Treat anything from an API, a log, a config file or a user as
untrusted. Does the change widen what a caller can reach? Are new dependencies
from trusted sources?""",
    },
    {
        "name": "performance",
        "title": "Performance",
        "focus": """N+1 queries, unbounded loops or fetches, missing pagination,
work repeated per item that could be done once, large objects in hot paths,
synchronous work that should not block. Estimate volume x cost rather than
asserting something is slow, and say which index a query relies on.""",
    },
]

#: Added only when the change touches an interface, from `api-and-interface-design`.
API_AXIS: Dict[str, str] = {
    "name": "api",
    "title": "API and interface design",
    "focus": """This change touches an interface, so review it as a contract.

Prefer addition over modification: a new optional field is safe, a changed type
or a removed field breaks existing consumers. If a change is incompatible, say
what the migration, compatibility window, rollback and reader/writer behaviour
are -- an incompatible change with none of those named is a Critical finding.

Remember Hyrum's Law: every observable behaviour, including error text, ordering
and undocumented quirks, is depended on by somebody. Say what this change newly
exposes and therefore commits to.

Check the contract itself: validation at the boundary rather than deep inside;
consistent error semantics with the rest of the surface; predictable naming
(plural resource nouns, camelCase fields, is/has prefixes on booleans); one
version of the contract rather than a fork. If it accepts an idempotency key,
is the key derived from the intent so a retry is genuinely safe?""",
}

#: Added for issue-tracked feature work, from the overlay enforced at review.
ISSUE_AXIS: Dict[str, str] = {
    "name": "issue",
    "title": "Issue-tracker compliance",
    "focus": """This is issue-tracked feature work, so the issue-tracker overlay is part of the
review rather than paperwork someone checks later. Report each of these, and
say plainly when evidence is absent -- absent evidence is a finding, not a
silence.

**Test enforcement.** Is there evidence the enforcement gate ran, with the command and
its result recorded? Missing, skipped or failed enforcement is a Critical
finding. For Java, was the `test-enforcer` plugin shown active in the effective
POM rather than assumed from a parent POM? Were fully qualified class names used
in `-DtargetTests`? Were surviving mutants on changed lines resolved by
strengthening assertions rather than by excluding classes -- and if anything was
excluded, is the reason recorded? A gate made green by a POM, profile or
`excludedMethods` change with no proof the changed code was still selected is a
Critical finding.

**The document set.** Do `doc/spec-<JIRA>.md`, `doc/design-<JIRA>.md` and
`doc/usage-<JIRA>.md` exist and match what was built -- or exist saying `N/A`
with a reason? A document that silently disagrees with the code is worse than a
missing one. Is the Jira key and original issue context recorded in the spec?

**Branch and version.** Is the branch named `$jira-$date` and based on
`origin/master`? For an API repo, is the development version
`<JIRA>-SNAPSHOT`, and is nothing shipping a snapshot?

Ship-time approval is **not** your business here. The
`rdcDeployAllowed` flag gates shipping, not merging, and a branch under review
legitimately has neither yet -- reporting their absence as a finding would
block work on a gate that has not been reached. Say nothing about them.

**Java guidelines.** Are there Alibaba Java Coding Guidelines *Mandatory*
violations in the changed code -- naming, formatting, collections, concurrency,
exception handling, logging, MySQL/ORM, security? Those are blockers. Is
style-only cleanup mixed into the feature change?""",
}

#: Thresholds ported from cragent's `classify_risk`, which already answers
#: this question for the same organisation's code. Keeping the numbers
#: identical matters more than choosing better ones: two reviewers that
#: disagree about what "small" means are two standards.
LIGHT_MAX_FILES = 5
LIGHT_MAX_LINES = 120
LIGHT_MAX_BYTES = 80 * 1024
FULL_MIN_FILES = 30
FULL_MIN_LINES = 800
FULL_MIN_BYTES = 500 * 1024

#: Path tokens that summon a specialist whatever the size of the change.
SPECIALIST_PATHS = {
    "security": ("token", "secret", "password", "auth", "permission", "api_key"),
}


def review_tier(changed_files, changed_lines: int = 0, diff_bytes: int = 0) -> str:
    """How much review this change has earned: light, standard or full.

    A small, local diff with nothing sensitive in its paths gets one reader; a
    sprawling one gets the panel. Deterministic on purpose -- an agent asked to
    judge how much review it deserves will answer differently on Tuesday.
    """
    files = [str(path) for path in changed_files or ()]
    if not files:
        return "light"
    if (
        len(files) > FULL_MIN_FILES
        or changed_lines > FULL_MIN_LINES
        or diff_bytes > FULL_MIN_BYTES
    ):
        return "full"
    if _specialists(files):
        return "standard"
    if (
        len(files) <= LIGHT_MAX_FILES
        and changed_lines <= LIGHT_MAX_LINES
        and diff_bytes <= LIGHT_MAX_BYTES
    ):
        return "light"
    return "standard"


def _specialists(paths) -> list:
    found = []
    for path in paths:
        lowered = str(path).lower()
        for name, tokens in SPECIALIST_PATHS.items():
            if name not in found and any(token in lowered for token in tokens):
                found.append(name)
    return found


def axes_for(tier: str, *, specialists=()) -> list:
    """The readers a tier earns.

    Light is one reader, not a thin version of six: the simplification pass
    runs regardless, and a small diff does not need architecture and
    performance opinions to be safe to merge.
    """
    by_name = {axis["name"]: axis for axis in AXES}
    if tier == "light":
        chosen = [by_name["correctness"]]
        chosen += [by_name[name] for name in specialists if name in by_name]
        return chosen
    if tier == "standard":
        names = ["correctness", "architecture"] + [n for n in specialists if n in by_name]
        return [by_name[n] for n in dict.fromkeys(names)]
    return list(AXES)


#: Paths that usually mean a contract other code depends on.
API_PATHS = re.compile(
    r"(^|/)(api|apis|rest|routes?|controllers?|handlers?|endpoints?|schema|schemas|"
    r"proto|openapi|swagger|graphql|dto|dtos|contracts?)(/|$)|"
    r"\.(proto|graphql|avsc)$|openapi\.(ya?ml|json)$",
    re.IGNORECASE,
)

#: What a design says when it changes a contract.
API_WORDS = re.compile(
    r"\b(api|endpoint|schema|interface|contract|public method|rest|rpc|grpc|"
    r"request body|response body|payload|migration)\b",
    re.IGNORECASE,
)


def touches_interface(changed_paths, design_md: str = "") -> bool:
    """Whether this change should also be read as a contract change.

    Deliberately generous: a false positive costs one more reviewer, a false
    negative means nobody asks whether the change breaks its consumers.
    """
    if any(API_PATHS.search(str(path)) for path in changed_paths or ()):
        return True
    return bool(API_WORDS.search(design_md or ""))


#: Where a repository's trunk lives, in the order worth trying. The workspace
#: clones one branch shallowly, so nothing else is fetched unless asked for.
TRUNK_CANDIDATES = ("master", "main")


def ensure_trunk_ref(state: Mapping[str, Any], context: Mapping[str, Any]) -> str:
    """Fetch the repository's trunk so a pending-deploy diff is possible.

    The workspace clones a single branch at depth 1, so `origin/master` does
    not exist and `git diff origin/master...HEAD` -- which the issue-tracker overlay asks
    for, and which every reviewer was told to take as "the base" -- cannot be
    computed. Returns the usable ref, or "" when the history does not reach far
    enough to share a commit, which a reviewer must be told rather than left to
    approximate.
    """
    workspace = context.get("workspace")
    repo_path = state.get("repo_path")
    if workspace is None or not repo_path:
        return ""
    repo = Path(str(repo_path))
    for name in TRUNK_CANDIDATES:
        ref = f"origin/{name}"
        for deepen in ((), ("--deepen", "500")):
            try:
                workspace.execute(
                    repo, "fetch", "--no-tags", *deepen, "origin",
                    f"+refs/heads/{name}:refs/remotes/{ref}", check=True,
                )
            except Exception:  # noqa: BLE001 - the repo may not use this name
                break
            try:
                if workspace.output(repo, "merge-base", ref, "HEAD").strip():
                    return ref
            except Exception:  # noqa: BLE001 - shallow histories may not meet
                continue
    return ""


def review_range(state: Mapping[str, Any], context: Mapping[str, Any]) -> str:
    """The range this run wrote, and nobody else's.

    Three candidates, in order of how exactly they answer the question.

    The run's own commits are exact: it made them, it recorded them. Failing
    that, `trunk...HEAD` is the branch against its fork point -- right about
    trunk, but it still carries anything a person put on the branch before the
    run started. Failing that, `base..HEAD`, which is the checkout tip and the
    weakest: two dots from a stale base include every commit merged into trunk
    since. On one cr-fix job that made the review read 36 files -- a service
    and thirty test classes this run never touched -- where its own commits
    changed 5.
    """
    made = [c for c in (state.get("run_commits") or ()) if c]
    if made:
        # Exactly what this run committed: from the parent of its first commit
        # to its last. Not the branch, which may carry a person's work from
        # before the run; not the checkout, which carries trunk's.
        return f"{made[0]}^..{made[-1]}"
    trunk = str(state.get("review_trunk") or "")
    if trunk:
        return f"{trunk}...HEAD"
    base = run_base(state)
    return f"{base}..HEAD" if base else "HEAD~1..HEAD"


def changed_paths(state: Mapping[str, Any], context: Mapping[str, Any]) -> List[str]:
    workspace = context.get("workspace")
    repo_path = state.get("repo_path")
    if workspace is None or not repo_path:
        return []
    try:
        out = workspace.output(
            Path(str(repo_path)), "diff", "--name-only", review_range(state, context)
        )
    except Exception:  # noqa: BLE001 - a diff we cannot read is not a review failure
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def _announce(context, task_ref: str, *, name: str, title: str, status: str, verdict: str = "") -> None:
    """Say what one axis is doing, so six agents at once are legible.

    The axes run concurrently and write into one progress log, where their
    lines interleave into something no one can follow. This is the per-agent
    record the page groups by -- the same job the task tree does for the build.
    """
    runtime = context.get("runtime_store")
    if runtime is None:
        return
    runtime.append_event(
        task_ref=task_ref,
        event_type="review_axis",
        severity="error" if status == "failed" else "info",
        stage=f"review_{name}",
        message=f"{title}: {status}",
        payload={"name": name, "title": title, "status": status, "verdict": verdict},
    )


def review_axes(state, config, context) -> Dict[str, Any]:
    """Decide which axes read this change, and open a new review pass.

    The pass number is what separates one round from the next. `axis_reviews`
    is a fan-out collect key, so its reducer *appends* -- returning an empty
    list does not clear it, and a second review would merge the first round's
    findings alongside its own. A Critical the fix pass resolved would still be
    in the document, the verdict would stay critical, and the loop could never
    reach clean.
    """
    jira = issue_key(state)
    trunk = ensure_trunk_ref(state, context) if jira else ""
    # Pass the trunk through, so the paths come from `trunk...HEAD` rather than
    # from a base that trunk has since moved past.
    changed = changed_paths({**state, "review_trunk": trunk}, context)
    tier = review_tier(changed, int(state.get("changed_lines") or 0))
    axes = axes_for(tier, specialists=_specialists(changed))
    if touches_interface(changed, str(state.get("design_md") or "")):
        axes.append(API_AXIS)
    # Compliance is not a depth question: issue-tracked work is issue-tracked work at any size.
    if jira:
        axes.append(ISSUE_AXIS)
    # Announce the whole set before any of them start: the page can then show
    # what is coming rather than growing a list as results trickle in.
    task_ref = str(state.get("task_ref") or "")
    for axis in axes:
        _announce(context, task_ref, name=_slug(axis["name"]), title=axis["title"], status="pending")
    return {
        "review_axes": axes,
        "jira": jira,
        "review_base": run_base(state),
        "review_trunk": trunk,
        "review_commits": [c for c in (state.get("run_commits") or ()) if c],
        "review_changed": len(changed),
        "review_tier": tier,
        "review_pass": int(state.get("review_pass") or 0) + 1,
    }


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-") or "axis"


def review_axis(state, config, context) -> Dict[str, Any]:
    """One axis, one agent, one prompt of its own.

    Each branch renders into its own directory: the axes run at the same time,
    and a shared prompt path would have them overwriting each other's prompt
    between the render and the turn.
    """
    turn = context.get("agent_turn")
    if turn is None:
        from agent_core.workflow.nodes import agent_turn as turn

    axis = state.get(str(config.get("item_key") or "axis")) or {}
    name = _slug(axis.get("name") or "axis")
    prompts = context.get("prompts")
    if prompts is None:
        raise RuntimeError("review_axis needs context['prompts']")

    task_ref = str(state.get("task_ref") or "")
    title = str(axis.get("title") or name)
    _announce(context, task_ref, name=name, title=title, status="running")

    directory = Path(str(state.get("turn_dir") or state.get("repo_path") or ".")) / f"review-{name}"
    artifact = prompts.render_to_file(
        "review_axis.md.j2",
        directory=directory,
        values={
            "axis_title": axis.get("title") or name,
            "axis_focus": axis.get("focus") or "",
            "title": state.get("title"),
            "plan_md": state.get("plan_md"),
            "jira": issue_key(state),
            "base": run_base(state),
            "trunk": state.get("review_trunk") or "",
            # What the run actually wrote, which is what the axis reviews.
            "commits": [c for c in (state.get("run_commits") or ()) if c],
            "repos": state.get("repos") or [],
        },
    )
    result = turn(
        {**state, "prompt_file": str(artifact.path)},
        {
            "result_mode": "normalized",
            "progress_detail": "public_detail",
            "label": f"review_{name}",
        },
        context,
    )
    text = str(result.get("turn_text") or "").strip()
    ok = str(result.get("turn_status") or "") in ("ok", "completed")

    verdict = (declaration(text, "VERDICT", VERDICTS) or "") if ok else ""
    _announce(
        context,
        task_ref,
        name=name,
        title=title,
        status="done" if ok else "failed",
        verdict=verdict,
    )
    return {
        "axis_reviews": {
            "pass": int(state.get("review_pass") or 1),
            "axis": axis.get("title") or name,
            "name": name,
            "text": text if ok else "",
            "failed": not ok,
        }
    }


def merge_reviews(state, config, context) -> Dict[str, Any]:
    """Assemble one review document from the axes, and one verdict from theirs.

    An axis that failed to run is reported rather than dropped: a review missing
    its security reader is not a clean review, and silence would read as one.
    """
    current = int(state.get("review_pass") or 1)
    reviews = [r for r in (state.get("axis_reviews") or []) if int(r.get("pass") or 1) == current]
    order = {str(a.get("name")): i for i, a in enumerate(state.get("review_axes") or [])}
    reviews.sort(key=lambda r: order.get(str(r.get("name")), 99))

    sections = [f"# Code review: {state.get('title') or ''}".rstrip()]
    critical = False
    failed = []
    for review in reviews:
        sections.append(f"\n## {review.get('axis')}\n")
        if review.get("failed") or not review.get("text"):
            failed.append(str(review.get("axis")))
            sections.append("_This axis did not complete; it has not been reviewed._\n")
            continue
        body = str(review["text"])
        if declaration(body, "VERDICT", VERDICTS) == "critical":
            critical = True
        sections.append(body.rstrip() + "\n")

    if failed:
        sections.append(
            "\n## Axes that did not complete\n\n"
            + "".join(f"- {name}\n" for name in failed)
            + "\nAn unreviewed axis is not a clean axis.\n"
        )
    sections.append(f"\nVERDICT: {'critical' if critical or failed else 'clean'}\n")
    text = "\n".join(sections)
    return {"turn_status": "completed", "turn_text": text, "review_md": text}
