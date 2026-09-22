"""Read artifacts the UI is allowed to show."""

from __future__ import annotations

from pathlib import Path

from agent_core.runtime.artifacts import SecureArtifactStore

from dev_flow_agent.workflow.stages import artifact_names

MAX_ARTIFACT_BYTES = 1_048_576


class UnknownArtifact(ValueError):
    """Name is not on the allowlist."""


def open_store(root: Path) -> SecureArtifactStore:
    return SecureArtifactStore(root)


def read_artifact(store: SecureArtifactStore, task_id: str, name: str) -> bytes:
    if name not in artifact_names():
        raise UnknownArtifact(name)
    return store.read_bytes(f"{task_id}/{name}", max_bytes=MAX_ARTIFACT_BYTES)
