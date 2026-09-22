"""Product node: persist a stage's document into the artifact store."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Mapping

from agent_core.runtime.artifacts import SecureArtifactStore

from dev_flow_agent.artifacts import MAX_ARTIFACT_BYTES

OK_STATUSES = frozenset({"ok", "completed"})


def _store(context: Mapping[str, Any]) -> SecureArtifactStore:
    store = context.get("artifacts")
    if store is None:
        raise RuntimeError("persist_doc needs context['artifacts']")
    return store


def _require_ok(state: Mapping[str, Any], what: str) -> None:
    status = str(state.get("turn_status") or "")
    if status not in OK_STATUSES:
        raise RuntimeError(f"{what} turn failed: {status or 'missing'}")


def _slug(title: str) -> str:
    text = re.sub(r"[^\w]+", "-", (title or "").strip(), flags=re.UNICODE)
    text = text.strip("-") or "untitled"
    return text[:80]


def _free_name(store: SecureArtifactStore, task_id: str, stem: str) -> str:
    candidate = f"{stem}.md"
    n = 2
    while True:
        try:
            store.read_bytes(f"{task_id}/{candidate}", max_bytes=MAX_ARTIFACT_BYTES)
        except FileNotFoundError:
            return candidate
        candidate = f"{stem}-{n}.md"
        n += 1


def _document(state: Mapping[str, Any], filename: str, what: str) -> str:
    """Prefer the file the agent wrote in the checkout over chat recap."""
    repo = state.get("repo_path")
    if repo:
        path = Path(str(repo)) / filename
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            if text.strip():
                return text
    text = str(state.get("turn_text") or "")
    if not text.strip():
        raise RuntimeError(f"{what} turn produced no text")
    return text


def persist_doc(state, config, context) -> Dict[str, Any]:
    """Store one stage's document under its stable name plus a snapshot.

    Two writes, because they answer different questions. ``spec.md`` is
    mutable and always the current document -- what the UI shows and what the
    next stage reads. The snapshot is immutable and named after the feature, so
    a retry cannot rewrite what a reviewer already approved.
    """
    artifact = str(config.get("artifact") or "")
    if not artifact:
        raise RuntimeError("persist_doc needs config['artifact']")
    label = str(config.get("label") or artifact.removesuffix(".md"))
    state_key = str(config.get("state_key") or f"{label}_md")
    attempt_key = str(config.get("attempt_key") or f"{label}_attempt")

    _require_ok(state, label)
    text = _document(state, artifact, label)
    task_id = str(state.get("task_ref") or "")
    store = _store(context)
    stem = f"{artifact.removesuffix('.md')}-{_slug(str(state.get('title') or ''))}"
    store.write_text(
        f"{task_id}/{_free_name(store, task_id, stem)}",
        text,
        immutable=True,
        max_bytes=MAX_ARTIFACT_BYTES,
    )
    store.write_text(
        f"{task_id}/{artifact}",
        text,
        immutable=False,
        max_bytes=MAX_ARTIFACT_BYTES,
    )
    # Say so in the event log. Which documents exist says nothing about which
    # run wrote them -- a retry starts at the first stage while the previous
    # run's documents are all still in the store -- so the pipeline view needs
    # an event it can date, not a file it can only find.
    runtime = context.get("runtime_store")
    if runtime is not None:
        runtime.append_event(
            task_ref=task_id,
            event_type="document_written",
            severity="info",
            stage=label,
            message=f"wrote {artifact}",
            payload={"artifact": artifact},
        )
    update = {
        state_key: text,
        attempt_key: int(state.get(attempt_key) or 0) + 1,
    }
    if artifact == "triage.md":
        # The classification is the point of that document, so it leaves as
        # state rather than being re-parsed by whoever needs it next.
        from dev_flow_agent.triage import FEATURE_DEV, parse_kind

        update["workflow_kind"] = parse_kind(text) or FEATURE_DEV
    return update
