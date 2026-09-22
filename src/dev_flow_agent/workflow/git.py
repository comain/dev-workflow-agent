"""What every node that writes to the repository shares.

Publishing a document and committing a task are the same operation with a
different policy, and the parts that must not disagree -- who the commits come
from, and what is kept out of them -- live here rather than in each caller.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from agent_core.git import BotIdentity

#: Who the pipeline's commits come from.
IDENTITY = BotIdentity(name="dev-flow-agent", email="dev-workflow-agent@example.com")


def run_base(state: Mapping[str, Any]) -> str:
    """The commit this run started from.

    `prepare_workspace` records it at checkout, so it is the exact base of
    everything the pipeline has since committed -- and it is local, which
    matters in a shallow single-branch clone where nothing else is.
    """
    return str(state.get("base_commit") or state.get("commit_id") or "")


def exclude_agent_noise(repo: Path) -> None:
    """Keep the harness's scratch directory out of every commit.

    `.agent_cache/` is written into the checkout by the agent and contains
    symlink loops that git cannot even stat. It is untracked, so it reaches the
    commit -- and any path policy -- unless it is excluded locally. The exclude
    file is inside `.git`, so nothing about the repository's own contents
    changes.
    """
    exclude = repo / ".git" / "info" / "exclude"
    try:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        for name in (".agent_cache/", ".dfa_cache/"):
            if name not in existing:
                existing += f"\n{name}\n"
        exclude.write_text(existing, encoding="utf-8")
    except OSError:
        # Best effort: a repo we cannot write to will fail later, and louder.
        pass


def commit_and_push(
    state: Mapping[str, Any],
    context: Mapping[str, Any],
    *,
    message: str,
    allow_path: Optional[Callable[[str], bool]] = None,
) -> Optional[str]:
    """Commit the working tree to the task's branch. None when nothing changed.

    A wiring without git is a test fixture rather than a failure, so a missing
    workspace, checkout or branch returns None instead of raising.
    """
    from dev_flow_agent.workflow.repos import repo_list

    workspace = context.get("workspace")
    repo_path = state.get("repo_path")
    branch = str(state.get("branch") or "")
    if workspace is None or not repo_path or not branch:
        return None

    # Every checkout the run has, not only the one it started in: a change
    # that spans repositories is not committed until all of it is, and a turn
    # that edited the second one would otherwise have its work thrown away by
    # the next `clean -fd`.
    primary = None
    elsewhere = []
    for repo in repo_list(state):
        path = Path(str(repo["path"]))
        exclude_agent_noise(path)
        sha = workspace.commit_all_and_push(
            path, branch=branch, message=message, identity=IDENTITY, allow_path=allow_path
        )
        if repo.get("primary"):
            primary = sha
        elif sha:
            elsewhere.append(sha)
    # The task's own repository is the one whose sha identifies the work; a
    # commit only elsewhere still has to be reported as a commit.
    return primary or (elsewhere[0] if elsewhere else None)
