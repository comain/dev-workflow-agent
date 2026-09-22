"""Product task queue. Status is product truth; LangGraph holds graph position."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS df_tasks (
  task_id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  request TEXT NOT NULL,
  repo_url TEXT NOT NULL,
  branch TEXT NOT NULL,
  status TEXT NOT NULL,
  created_by TEXT,
  idempotency_key TEXT,
  request_hash TEXT,
  workflow_run_id TEXT NOT NULL,
  lease_owner TEXT,
  lease_until TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  -- Event id the current run started after. A retry moves it, which is
  -- what stops a previous run's documents from being read as this one's.
  run_watermark INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_df_tasks_idempotency
  ON df_tasks(idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_df_tasks_updated ON df_tasks(updated_at DESC);
CREATE INDEX IF NOT EXISTS ix_df_tasks_claim ON df_tasks(status, lease_until);

-- The plan's tasks, so a long build reports where it is rather than going
-- quiet for an hour. Keyed by (task, seq) because the plan numbers them.
CREATE TABLE IF NOT EXISTS df_plan_tasks (
  task_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  commit_sha TEXT,
  -- Where this task came from: the plan, or repair work the run gave itself
  -- after a gate or a reviewer rejected what it had built.
  origin TEXT NOT NULL DEFAULT 'plan',
  updated_at TEXT NOT NULL,
  PRIMARY KEY (task_id, seq)
);
"""

FAILABLE = frozenset({"queued", "running", "waiting_human"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def request_hash(payload: Dict[str, Any]) -> str:
    body = {
        "title": payload.get("title"),
        "request": payload.get("request"),
        "repo_url": payload.get("repo_url"),
        "branch": payload.get("branch"),
    }
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class TaskStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Add columns a database created by an earlier version is missing.

        `CREATE TABLE IF NOT EXISTS` does nothing to a table that already
        exists, so a new column never reaches a deployed database without this.
        """
        self._add_column("df_tasks", "run_watermark", "INTEGER NOT NULL DEFAULT 0")
        self._add_column("df_plan_tasks", "origin", "TEXT NOT NULL DEFAULT 'plan'")

    def _add_column(self, table: str, column: str, declaration: str) -> None:
        """Add a column once, tolerating a racing process adding it too.

        `dev` runs the server and the worker in one process, each with its own
        connection, so two migrations start together: both see the column
        missing and both try to add it. The loser's failure is not a failure --
        the column is there, which is all this asked for -- but it crashed the
        service on the first start after a deploy.
        """
        present = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}
        if not present or column in present:
            return
        try:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise

    def close(self) -> None:
        self._conn.close()

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM df_tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        return dict(row) if row else None

    def list(self, *, limit: int = 50, status: Optional[str] = None) -> List[Dict[str, Any]]:
        if status:
            rows = self._conn.execute(
                "SELECT * FROM df_tasks WHERE status = ? ORDER BY updated_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM df_tasks ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def create(
        self,
        *,
        title: str,
        request: str,
        repo_url: str,
        branch: str,
        created_by: Optional[str],
        idempotency_key: Optional[str],
    ) -> tuple[Dict[str, Any], bool]:
        """Insert a queued task. Returns (task, created). Replay is created=False."""
        payload = {
            "title": title,
            "request": request,
            "repo_url": repo_url,
            "branch": branch,
        }
        digest = request_hash(payload)
        now = _now()
        task_id = uuid.uuid4().hex[:16]
        run_id = uuid.uuid4().hex[:16]
        try:
            self._conn.execute(
                """
                INSERT INTO df_tasks (
                    task_id, title, request, repo_url, branch, status, created_by,
                    idempotency_key, request_hash, workflow_run_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id, title, request, repo_url, branch, created_by,
                    idempotency_key, digest, run_id, now, now,
                ),
            )
            self._conn.commit()
            return self.get(task_id), True  # type: ignore[return-value]
        except sqlite3.IntegrityError:
            if not idempotency_key:
                raise
            existing = self._conn.execute(
                "SELECT * FROM df_tasks WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is None:
                raise
            if existing["request_hash"] != digest:
                raise ValueError("idempotency key reused with a different payload")
            return dict(existing), False

    def requeue(self, task_id: str, *, run_watermark: int = 0) -> Dict[str, Any]:
        """Queue a failed task again under a new workflow run.

        ``run_watermark`` is the event log's high-water mark at the moment of
        the retry. The new run starts from the first stage, so every document
        already in the store belongs to the previous one; the watermark is how
        the pipeline view tells them apart instead of reporting a stage this
        run has not reached.
        """
        row = self.get(task_id)
        if row is None:
            raise KeyError(f"unknown task: {task_id}")
        if row["status"] != "failed":
            raise ValueError(f"cannot retry status {row['status']}")
        now = _now()
        run_id = uuid.uuid4().hex[:16]
        self._conn.execute(
            """
            UPDATE df_tasks
            SET status = 'queued', workflow_run_id = ?, lease_owner = NULL,
                lease_until = NULL, error = NULL, updated_at = ?,
                run_watermark = ?
            WHERE task_id = ? AND status = 'failed'
            """,
            (run_id, now, int(run_watermark), task_id),
        )
        self._conn.commit()
        retried = self.get(task_id)
        if retried is None or retried["status"] != "queued":
            raise ValueError(f"cannot retry status {row['status']}")
        return retried

    # -- the plan's tasks ---------------------------------------------

    def replace_plan_tasks(self, task_id: str, items) -> None:
        """Set the task tree for a run, discarding any earlier plan's.

        A re-approved plan supersedes the one before it; keeping both would
        leave the tree describing work nobody asked for.
        """
        now = _now()
        with self._conn:
            self._conn.execute("DELETE FROM df_plan_tasks WHERE task_id = ?", (task_id,))
            self._conn.executemany(
                """
                INSERT INTO df_plan_tasks (task_id, seq, title, body, status, updated_at)
                VALUES (?, ?, ?, ?, 'pending', ?)
                """,
                [(task_id, i, t["title"], t.get("body", ""), now) for i, t in enumerate(items, 1)],
            )

    def append_plan_task(
        self, task_id: str, *, title: str, body: str = "", origin: str = "plan"
    ) -> int:
        """Add one task to the end of the tree, and return its seq.

        Repair work -- a failed enforcement gate, a reviewer's Critical
        finding -- is work like any other, so it joins the same list and goes
        through the same gate rather than running beside it.
        """
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM df_plan_tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        seq = int(row["seq"])
        self._conn.execute(
            """
            INSERT INTO df_plan_tasks (task_id, seq, title, body, status, origin, updated_at)
            VALUES (?, ?, ?, ?, 'pending', ?, ?)
            """,
            (task_id, seq, title, body, origin, _now()),
        )
        self._conn.commit()
        return seq

    def plan_tasks(self, task_id: str) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM df_plan_tasks WHERE task_id = ? ORDER BY seq", (task_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def next_plan_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """The next task to build: the first that is not finished.

        A failed task is still unfinished. Excluding it here skipped it
        silently and left the rest of the plan to build on work that was never
        done -- and the attempt limit, which is what stops a run for a human,
        could never be reached because the task was never offered again.
        """
        row = self._conn.execute(
            """
            SELECT * FROM df_plan_tasks
            WHERE task_id = ? AND status IN ('pending', 'running', 'failed')
            ORDER BY seq LIMIT 1
            """,
            (task_id,),
        ).fetchone()
        return dict(row) if row else None

    def set_plan_task(
        self,
        task_id: str,
        seq: int,
        *,
        status: str,
        commit_sha: Optional[str] = None,
        bump_attempts: bool = False,
    ) -> None:
        self._conn.execute(
            """
            UPDATE df_plan_tasks
            SET status = ?, updated_at = ?,
                commit_sha = COALESCE(?, commit_sha),
                attempts = attempts + ?
            WHERE task_id = ? AND seq = ?
            """,
            (status, _now(), commit_sha, 1 if bump_attempts else 0, task_id, seq),
        )
        self._conn.commit()

    def fail(self, task_id: str) -> Dict[str, Any]:
        """Mark an in-flight task failed so it can be retried from the UI."""
        row = self.get(task_id)
        if row is None:
            raise KeyError(f"unknown task: {task_id}")
        if row["status"] not in FAILABLE:
            raise ValueError(f"cannot fail status {row['status']}")
        now = _now()
        self._conn.execute(
            """
            UPDATE df_tasks
            SET status = 'failed', lease_owner = NULL, lease_until = NULL, updated_at = ?
            WHERE task_id = ? AND status IN ('queued', 'running', 'waiting_human')
            """,
            (now, task_id),
        )
        self._conn.commit()
        failed = self.get(task_id)
        if failed is None or failed["status"] != "failed":
            raise ValueError(f"cannot fail status {row['status']}")
        return failed
