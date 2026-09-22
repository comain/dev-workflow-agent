"""Agent progress, from the harness into the task event log.

The SSE route and the page that tails it were built first; what was missing is
anything writing the events. The harness emits progress only when a turn is
given a progress port, so without this the stream is well-formed and empty.

Two decisions worth stating. Progress goes through agent-core's sink rather
than straight to ``append_event``: the harness calls the publish path inline
while polling the provider, so an unbatched write would put one SQLite
transaction on the model's critical path per update. And what is stored is the
*projected* `AgentProgressEvent`, never the raw `TurnProgress` -- the page is
readable by anyone who can reach it, and raw progress carries model text, tool
commands, and provider errors.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

from agent_core.runtime import ProgressBatcher, RuntimeStore, SinkProgressPort

#: Kinds that are failures in themselves.
_ERROR_KINDS = frozenset({"error", "failed"})

#: The status a *tool* failure projects to. A failing `mvn` arrives as an
#: ordinary tool update whose status is this, so reading only the kind made a
#: broken build render exactly like a passing one -- the reason a JDK
#: misconfiguration was found in the agent's own output rather than here.
FAILED_STATUS = "issue"


def _severity(kind: str, status: str = "") -> str:
    if str(kind or "").lower() in _ERROR_KINDS:
        return "error"
    return "error" if str(status or "").lower() == FAILED_STATUS else "info"


def turn_progress_port(store: RuntimeStore, *, task_ref: str) -> SinkProgressPort:
    """A progress port that records this task's updates as ``agent_progress``.

    One port serves the whole run: the phase comes off each turn's request, so
    a single sink covers every stage without being rebuilt per node.
    """

    def append_batch(*, session_id: Optional[str], events: Sequence[Any]) -> None:
        # Never raises: progress is not authoritative, and a failure here would
        # reclassify a model turn that actually succeeded.
        for event in events:
            store.append_event(
                task_ref=task_ref,
                event_type="agent_progress",
                severity=_severity(event.kind, event.status),
                stage=event.phase or None,
                message=event.summary,
                payload={
                    "kind": event.kind,
                    "tool": event.tool,
                    "status": event.status,
                    "detail": event.detail,
                    "sequence": event.sequence,
                    "session_id": session_id,
                },
            )

    return SinkProgressPort(ProgressBatcher(append_batch))
