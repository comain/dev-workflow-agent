"""The repositories a change touches, which is not always the one it started in.

A task names one repository because a person had to type something, and that
is where the work begins. It is often not where the work ends: a contract
lives in the API repo and its callers elsewhere, a fix to a shared service
needs the consumer updated in the same breath, a Jira issue spans three
services by design.

Triage is where that becomes known -- it has read the request and enough of the
code to say what the change touches -- so triage declares the list, and the
pipeline prepares a checkout for each. From then on every turn can read and
change all of them, and every commit covers all of them.

The names are application names, the same ones a person uses; the catalog
turns each into a repository. A URL is taken as itself, so a repository with
no application record is still reachable.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

#: `REPOS: sample_service, sample-web`, or `REPOS: none`.
DECLARATION = re.compile(r"^\s*REPOS:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)

#: What a turn writes when the change stays where it started.
NONE_WORDS = frozenset({"none", "n/a", "-", "only this one", "this repository"})


def declared_repos(text: str) -> List[str]:
    """The extra repositories a document names, in the order it names them.

    Last declaration wins, as everywhere else: a document quoting the example
    above its answer would otherwise have the example read back to it.
    """
    matches = DECLARATION.findall(text or "")
    if not matches:
        return []
    names: List[str] = []
    for raw in matches[-1].split(","):
        name = raw.strip().strip("`").strip()
        if not name or name.lower() in NONE_WORDS:
            continue
        if name not in names:
            names.append(name)
    return names


def _resolve(name: str, context: Mapping[str, Any]) -> str:
    """The clone URL for one declared name."""
    from dev_flow_agent.chongxiao import Chongxiao, LookupFailed, looks_like_repo
    from dev_flow_agent.config import Settings

    if looks_like_repo(name):
        return name
    lookup = context.get("applications")
    if lookup is None:
        settings = Settings()
        lookup = Chongxiao(
            access_token=settings.sso_access_token,
            base_url=settings.catalog_base_url or "https://catalog.example.com",
            resolve_ip=settings.sso_resolve_ip or "",
        )
    try:
        return lookup.repo_url(name)
    except LookupFailed:
        return ""


def prepare_repos(state, config, context) -> Dict[str, Any]:
    """Check out every repository the triage named, beside the first one.

    A repository that cannot be resolved or cloned is reported and skipped
    rather than failing the run: the work in the repository we do have is
    still worth doing, and the document says what was unreachable.
    """
    workspace = context.get("workspace")
    task_ref = str(state.get("task_ref") or "")
    branch = str(state.get("branch") or "")
    primary = {
        "name": str(state.get("repo_url") or ""),
        "url": str(state.get("repo_url") or ""),
        "path": str(state.get("repo_path") or ""),
        "primary": True,
    }
    names = declared_repos(str(state.get("triage_md") or ""))
    if workspace is None or not names:
        return {"repos": [primary]}

    prepared = [primary]
    unreachable = []
    for name in names:
        url = _resolve(name, context)
        if not url or url == primary["url"]:
            if not url:
                unreachable.append(name)
            continue
        try:
            path = workspace.prepare(
                url,
                branch=branch or None,
                scope=f"{task_ref}-{re.sub(r'[^A-Za-z0-9._-]+', '-', name)}",
                create_missing=True,
                local_branch=True,
                is_cancelled=context.get("is_cancelled"),
            )
        except Exception as exc:  # noqa: BLE001 - one missing repo is not the run
            unreachable.append(f"{name} ({exc.__class__.__name__})")
            continue
        prepared.append({"name": name, "url": url, "path": str(path), "primary": False})

    runtime = context.get("runtime_store")
    if runtime is not None and (len(prepared) > 1 or unreachable):
        runtime.append_event(
            task_ref=task_ref,
            event_type="agent_progress",
            severity="warning" if unreachable else "info",
            stage="triage",
            message=(
                f"working across {len(prepared)} repositories"
                + (f"; could not reach {', '.join(unreachable)}" if unreachable else "")
            ),
        )
    return {"repos": prepared, "repos_unreachable": unreachable}


def repo_list(state: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Every checkout this run has, the first one included."""
    repos = [dict(repo) for repo in (state.get("repos") or ()) if repo.get("path")]
    if repos:
        return repos
    path = str(state.get("repo_path") or "")
    return [{"name": str(state.get("repo_url") or ""), "url": "", "path": path, "primary": True}] if path else []


def with_changes(state: Mapping[str, Any], context: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """The checkouts this run actually changed, and what it changed in each.

    The gate must run where the code is. One run declared a second repository,
    built nothing in it, and ran the enforcement gate in the first -- which
    had no production change, reported `pitest.targets=0`, and passed on the
    strength of measuring nothing. A repository with no commits of ours is not
    a repository to enforce.
    """
    workspace = context.get("workspace")
    if workspace is None:
        return []
    changed = []
    for repo in repo_list(state):
        path = Path(str(repo["path"]))
        try:
            files = workspace.output(path, "diff", "--name-only", "origin/master...HEAD")
        except Exception:  # noqa: BLE001 - a repo we cannot diff is not the gate's answer
            continue
        names = [line.strip() for line in files.splitlines() if line.strip()]
        if names:
            changed.append({**repo, "changed": names})
    return changed


def secondary_paths(state: Mapping[str, Any]) -> Sequence[Path]:
    """The checkouts beside the first, which commits must not forget."""
    return [Path(repo["path"]) for repo in repo_list(state) if not repo.get("primary")]
