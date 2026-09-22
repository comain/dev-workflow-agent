"""Whether this run is issue-tracked feature work, and what it is keyed to.

The pipeline serves two kinds of work. issue-tracked feature work starts from a Jira
issue and carries an overlay the org enforces at review and ship: a document
set under `doc/`, a branch named for the issue, test-enforcement evidence.
Tooling work carries none of that, and forcing Jira artifacts onto it would be
inventing paperwork.

The branch is the discriminator because it is the one thing every run already
has and the org already encodes: `TICKET-100-20260910` names its issue.
"""

from __future__ import annotations

import re
from typing import Optional

#: A Jira key at the head of a branch name: TICKET-100-20260910 -> TICKET-100.
JIRA_KEY = re.compile(r"([A-Z][A-Z0-9]*-\d+)")


def jira_key(branch: str) -> Optional[str]:
    """The issue this branch belongs to, or None for non-Jira work."""
    match = JIRA_KEY.search(str(branch or ""))
    return match.group(1) if match else None


def is_blf(branch: str) -> bool:
    return jira_key(branch) is not None


def issue_key(state) -> str:
    """The Jira key for a run, derived rather than remembered.

    `jira` is injected into the initial state, which only merges on a fresh
    invoke -- a run resumed from a checkpoint written before the key existed
    silently loses issue-tracked mode, and its compliance axis, trunk fetch and
    enforcement gate with it. The branch is durable and already carries the
    key, so it is the source of truth and the state is only a shortcut.
    """
    return str(state.get("jira") or jira_key(str(state.get("branch") or "")) or "")
