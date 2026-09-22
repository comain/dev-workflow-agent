"""Put an approved document in the repository it describes.

Until now the documents lived only in the artifact store, on the host that ran
the task. That is the wrong home for them: the spec and design of a change
belong beside the change, on its branch, where a reviewer reading the diff
finds them and where they survive the agent host entirely.

Only an *approved* document is published. A draft a human rejected has no
business in the repository, and publishing on write rather than on approval
would put one there.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from dev_flow_agent.issue import jira_key
from dev_flow_agent.workflow.git import commit_and_push

#: Where the documents go, following the issue-tracker document set: `doc/<kind>-<key>.md`.
DOC_DIR = "doc"

def document_key(state: Mapping[str, Any]) -> str:
    """What the published files are named after.

    The branch is the best available name: it is what a reviewer is looking at,
    and in this organisation it carries the Jira key. Without one the task id
    still separates two runs in the same repository.
    """
    return jira_key(str(state.get("branch") or "")) or str(state.get("task_ref") or "task")


def document_path(artifact: str, key: str) -> str:
    return f"{DOC_DIR}/{Path(artifact).stem}-{key}.md"


def _allow(path: str) -> bool:
    """Only the documents. A turn that edited source cannot publish it here."""
    return path.startswith(f"{DOC_DIR}/") and path.endswith(".md")


def publish_doc(state, config, context) -> Dict[str, Any]:
    """Commit one approved document to the task's branch and push it."""
    if str(state.get("decision") or "") != "approve":
        return {}

    artifact = str(config.get("artifact") or "")
    if not artifact:
        raise RuntimeError("publish_doc needs config['artifact']")
    text = str(state.get(str(config.get("state_key") or "")) or "")
    if not text.strip():
        return {}

    workspace = context.get("workspace")
    repo_path = state.get("repo_path")
    branch = str(state.get("branch") or "")
    if workspace is None or not repo_path or not branch:
        # A product wiring without git is a test fixture, not a failure.
        return {}

    repo = Path(str(repo_path))
    relative = document_path(artifact, document_key(state))
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")

    # Stage it, so `git status` names the file rather than reporting the whole
    # untracked `doc/` directory -- which the path guard would refuse, and
    # which would hide anything else written into that directory.
    workspace.execute(repo, "add", "--", relative, check=True)
    sha = commit_and_push(
        state,
        context,
        message=f"docs: {Path(artifact).stem} for {document_key(state)}",
        allow_path=_allow,
    )
    return {"published": relative, "published_commit": sha} if sha else {}
