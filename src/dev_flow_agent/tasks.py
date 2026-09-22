"""Claim and release against df_tasks. The runtime store supplies answered gates."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from agent_core.runtime import RuntimeStore, TaskOutcome

from dev_flow_agent.db import TaskStore, _now

#: Status the run ended in, and the event that says so on the stream.
TERMINAL_EVENTS = {"completed": "task_completed", "failed": "task_failed"}


class TaskQueue:
    def __init__(self, tasks: TaskStore, runtime: RuntimeStore, *, lease_seconds: int = 300):
        self.tasks = tasks
        self.runtime = runtime
        self.lease_seconds = lease_seconds

    def claim(self, *, daemon_id: str, lease_seconds: int) -> Optional[str]:
        now = datetime.now(timezone.utc)
        now_s = now.isoformat()
        until = (now + timedelta(seconds=int(lease_seconds))).isoformat()
        conn = self.tasks._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                """
                SELECT task_id FROM df_tasks
                WHERE status IN ('queued', 'running')
                  AND (lease_until IS NULL OR lease_until < ?)
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (now_s,),
            ).fetchone()
            if row is None:
                refs = [
                    g.task_ref
                    for g in self.runtime.answered_gates_awaiting_resume(limit=100)
                    if g.task_ref
                ]
                if refs:
                    placeholders = ",".join("?" * len(refs))
                    row = conn.execute(
                        f"""
                        SELECT task_id FROM df_tasks
                        WHERE status = 'waiting_human' AND task_id IN ({placeholders})
                        ORDER BY created_at ASC
                        LIMIT 1
                        """,
                        tuple(refs),
                    ).fetchone()
            if row is None:
                conn.commit()
                return None
            task_id = row["task_id"]
            cur = conn.execute(
                """
                UPDATE df_tasks
                SET status = 'running', lease_owner = ?, lease_until = ?, updated_at = ?
                WHERE task_id = ?
                  AND (
                    (status IN ('queued', 'running')
                     AND (lease_until IS NULL OR lease_until < ?))
                    OR status = 'waiting_human'
                  )
                """,
                (daemon_id, until, now_s, task_id, now_s),
            )
            if cur.rowcount != 1:
                conn.commit()
                return None
            conn.commit()
            # Say that work has restarted. Answering a gate produces no event of
            # its own, and the status change it causes is invisible to a page
            # tailing the log -- so an approved task went on showing the gate it
            # had already answered until the *next* gate opened, minutes later.
            self.runtime.append_event(
                task_ref=task_id,
                event_type="run_started",
                severity="info",
                stage=None,
                message="run started",
            )
            return task_id
        except Exception:
            conn.rollback()
            raise

    def renew_lease(self, task_ref: str, *, daemon_id: str, lease_seconds: int) -> bool:
        until = (datetime.now(timezone.utc) + timedelta(seconds=int(lease_seconds))).isoformat()
        cur = self.tasks._conn.execute(
            """
            UPDATE df_tasks SET lease_until = ?, updated_at = ?
            WHERE task_id = ? AND lease_owner = ? AND status = 'running'
            """,
            (until, _now(), task_ref, daemon_id),
        )
        self.tasks._conn.commit()
        return cur.rowcount == 1

    def release(self, task_ref: str, outcome: TaskOutcome) -> None:
        if outcome is TaskOutcome.SUSPENDED:
            status = "waiting_human"
        elif outcome is TaskOutcome.COMPLETED:
            status = "completed"
        else:
            status = "failed"
            for gate in self.runtime.pending_gates(limit=1000):
                if gate.task_ref == task_ref:
                    self.runtime.cancel_gate(gate.gate_id)
        self.tasks._conn.execute(
            """
            UPDATE df_tasks
            SET status = ?, lease_owner = NULL, lease_until = NULL, updated_at = ?
            WHERE task_id = ?
            """,
            (status, _now(), task_ref),
        )
        self.tasks._conn.commit()
        if status in TERMINAL_EVENTS:
            # The SSE stream ends on these, so a viewer's connection and poll
            # loop end with the run instead of waiting out the duration cap.
            self.runtime.append_event(
                task_ref=task_ref,
                event_type=TERMINAL_EVENTS[status],
                severity="error" if status == "failed" else "info",
                stage=None,
                message=f"task {status}",
            )

    def retry(self, task_ref: str):
        """Requeue a failed task, marking where its new run begins."""
        return self.tasks.requeue(
            task_ref, run_watermark=self.runtime.latest_event_id(task_ref=task_ref)
        )

    def fail(self, task_ref: str):
        """Operator abort: same terminal state as a worker failure."""
        row = self.tasks.get(task_ref)
        if row is None:
            raise KeyError(f"unknown task: {task_ref}")
        if row["status"] not in ("queued", "running", "waiting_human"):
            raise ValueError(f"cannot fail status {row['status']}")
        for gate in self.runtime.pending_gates(limit=1000):
            if gate.task_ref == task_ref:
                self.runtime.cancel_gate(gate.gate_id)
        return self.tasks.fail(task_ref)
