"""SQLite-backed history store for Daily CI runs, jobs, and parsed failures."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Dict, List, Optional

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS workflow_runs (
    run_id        INTEGER PRIMARY KEY,
    repo          TEXT    NOT NULL DEFAULT 'valkey-io/valkey',
    workflow_file TEXT    NOT NULL,
    status        TEXT    NOT NULL,
    branch        TEXT    NOT NULL,
    commit_sha    TEXT    NOT NULL,
    run_date      TEXT    NOT NULL,
    duration_secs INTEGER,
    raw_json      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id     INTEGER PRIMARY KEY,
    run_id     INTEGER NOT NULL REFERENCES workflow_runs(run_id),
    name       TEXT    NOT NULL,
    status     TEXT    NOT NULL,
    conclusion TEXT,
    raw_json   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS job_logs (
    job_id     INTEGER PRIMARY KEY REFERENCES jobs(job_id),
    raw_log    TEXT    NOT NULL,
    fetched_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS test_failures (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        INTEGER NOT NULL REFERENCES jobs(job_id),
    test_name     TEXT    NOT NULL,
    error_summary TEXT    NOT NULL,
    log_lines     TEXT    NOT NULL,
    UNIQUE(job_id, test_name)
);

CREATE TABLE IF NOT EXISTS parse_status (
    job_id    INTEGER PRIMARY KEY REFERENCES jobs(job_id),
    status    TEXT    NOT NULL,
    parsed_at TEXT    NOT NULL
);

-- Per-run ingest progress for weekly release-branch splitting.
-- status: 'split' = jobs fetched + synthetic per-branch runs stored;
--         'done'  = all failed-job logs fetched/parsed (or confirmed expired).
CREATE TABLE IF NOT EXISTS weekly_ingest (
    run_id      INTEGER PRIMARY KEY,
    status      TEXT    NOT NULL,
    ingested_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_workflow ON workflow_runs(workflow_file);
CREATE INDEX IF NOT EXISTS idx_runs_date ON workflow_runs(run_date);
CREATE INDEX IF NOT EXISTS idx_runs_status ON workflow_runs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_run ON jobs(run_id);
CREATE INDEX IF NOT EXISTS idx_jobs_conclusion ON jobs(conclusion);
CREATE INDEX IF NOT EXISTS idx_failures_job ON test_failures(job_id);
CREATE INDEX IF NOT EXISTS idx_failures_name ON test_failures(test_name);
"""


_MIGRATIONS = [
    # Migration 1: add repo column to existing databases
    (
        "ALTER TABLE workflow_runs ADD COLUMN repo TEXT NOT NULL DEFAULT 'valkey-io/valkey'",
        "CREATE INDEX IF NOT EXISTS idx_runs_repo ON workflow_runs(repo)",
    ),
]

# Bump this when log_parser.py changes produce different test_name values.
# On mismatch, test_failures and parse_status are wiped and re-parsed.
PARSER_VERSION = 7


def _now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


class Cache:
    """SQLite cache for workflow runs, jobs, logs, and test failures."""

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA_SQL)
        self._migrate()
        self._check_parser_version()
        # Ensure repo index exists (for both new and migrated databases)
        try:
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_runs_repo ON workflow_runs(repo)"
            )
            self._conn.commit()
        except sqlite3.OperationalError:
            pass

    def _migrate(self) -> None:
        """Apply schema migrations for existing databases."""
        for statements in _MIGRATIONS:
            try:
                for sql in statements:
                    self._conn.execute(sql)
                self._conn.commit()
            except sqlite3.OperationalError:
                # Column/index already exists — skip
                pass

    def _check_parser_version(self) -> None:
        """Invalidate cached parse results when parser logic changes.

        Keeps workflow_runs, jobs, and job_logs intact (expensive to
        re-fetch). Only wipes test_failures and parse_status so the
        next sync re-parses all cached logs with the new parser.
        """
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)"
        )
        row = self._conn.execute(
            "SELECT value FROM metadata WHERE key = 'parser_version'"
        ).fetchone()
        stored = int(row[0]) if row else 0

        if stored < PARSER_VERSION:
            self._conn.execute("DELETE FROM test_failures")
            self._conn.execute("DELETE FROM parse_status")
            self._conn.execute(
                "INSERT OR REPLACE INTO metadata (key, value) VALUES ('parser_version', ?)",
                (str(PARSER_VERSION),),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Workflow Runs
    # ------------------------------------------------------------------

    def has_run(self, run_id: int) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM workflow_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return row is not None

    def store_runs(self, runs: List[Dict]) -> None:
        self._conn.executemany(
            """INSERT OR IGNORE INTO workflow_runs
               (run_id, repo, workflow_file, status, branch, commit_sha, run_date, duration_secs, raw_json)
               VALUES (:run_id, :repo, :workflow_file, :status, :branch, :commit_sha, :run_date, :duration_secs, :raw_json)""",
            [
                {
                    "run_id": r["run_id"],
                    "repo": r.get("repo", "valkey-io/valkey"),
                    "workflow_file": r["workflow_file"],
                    "status": r["status"],
                    "branch": r["branch"],
                    "commit_sha": r["commit_sha"],
                    "run_date": r["run_date"],
                    "duration_secs": r.get("duration_secs"),
                    "raw_json": r.get("raw_json", json.dumps(r)),
                }
                for r in runs
            ],
        )
        self._conn.commit()

    def query_runs(
        self,
        repo: Optional[str] = None,
        workflow: Optional[str] = None,
        status: Optional[str] = None,
        branch: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
    ) -> List[Dict]:
        clauses: List[str] = []
        params: List[object] = []
        if repo is not None:
            clauses.append("repo = ?")
            params.append(repo)
        if workflow is not None:
            clauses.append("workflow_file = ?")
            params.append(workflow)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if branch is not None:
            clauses.append("branch = ?")
            params.append(branch)
        if since is not None:
            clauses.append("run_date >= ?")
            params.append(since)
        if until is not None:
            clauses.append("run_date <= ?")
            params.append(until)

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            "SELECT run_id, repo, workflow_file, status, branch, commit_sha, run_date, duration_secs FROM workflow_runs"
            + where
            + " ORDER BY run_date DESC"
        )
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------

    def has_jobs_for_run(self, run_id: int) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM jobs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return row is not None

    def has_incomplete_jobs(self, run_id: int) -> bool:
        """Return True if any cached job for *run_id* is still in progress."""
        row = self._conn.execute(
            "SELECT 1 FROM jobs WHERE run_id = ? AND status != 'completed' LIMIT 1",
            (run_id,),
        ).fetchone()
        return row is not None

    def store_jobs(self, run_id: int, jobs: List[Dict]) -> None:
        self._conn.executemany(
            """INSERT OR REPLACE INTO jobs
               (job_id, run_id, name, status, conclusion, raw_json)
               VALUES (:job_id, :run_id, :name, :status, :conclusion, :raw_json)""",
            [
                {
                    "job_id": j["job_id"],
                    "run_id": run_id,
                    "name": j["name"],
                    "status": j["status"],
                    "conclusion": j.get("conclusion"),
                    "raw_json": j.get("raw_json", json.dumps(j)),
                }
                for j in jobs
            ],
        )
        self._conn.commit()

    def get_job_name(self, job_id: int) -> Optional[str]:
        """Return the job name for *job_id*, or None if unknown."""
        row = self._conn.execute(
            "SELECT name FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return row["name"] if row else None

    def query_jobs(self, run_id: int, failed_only: bool = False) -> List[Dict]:
        clauses = ["run_id = ?"]
        params: List[object] = [run_id]
        if failed_only:
            clauses.append("conclusion = 'failure'")
        where = " WHERE " + " AND ".join(clauses)
        sql = "SELECT job_id, run_id, name, status, conclusion FROM jobs" + where
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Logs
    # ------------------------------------------------------------------

    def has_log(self, job_id: int) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM job_logs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return row is not None

    def store_log(self, job_id: int, raw_log: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO job_logs (job_id, raw_log, fetched_at) VALUES (?, ?, ?)",
            (job_id, raw_log, _now_iso()),
        )
        self._conn.commit()

    def get_log(self, job_id: int) -> Optional[str]:
        row = self._conn.execute(
            "SELECT raw_log FROM job_logs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return row["raw_log"] if row else None

    # ------------------------------------------------------------------
    # Test Failures
    # ------------------------------------------------------------------

    def has_failures_for_job(self, job_id: int) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM parse_status WHERE job_id = ?", (job_id,)
        ).fetchone()
        return row is not None

    def query_unparsed_jobs_with_logs(self) -> List[int]:
        """Return job_ids that have cached logs but no parse_status entry."""
        rows = self._conn.execute(
            "SELECT job_id FROM job_logs WHERE job_id NOT IN (SELECT job_id FROM parse_status)"
        ).fetchall()
        return [r[0] for r in rows]

    def store_failures(self, job_id: int, failures: List[Dict]) -> None:
        now = _now_iso()
        self._conn.executemany(
            """INSERT OR IGNORE INTO test_failures
               (job_id, test_name, error_summary, log_lines)
               VALUES (:job_id, :test_name, :error_summary, :log_lines)""",
            [
                {
                    "job_id": job_id,
                    "test_name": f["test_name"],
                    "error_summary": f["error_summary"],
                    "log_lines": f["log_lines"],
                }
                for f in failures
            ],
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO parse_status (job_id, status, parsed_at) VALUES (?, 'parsed', ?)",
            (job_id, now),
        )
        self._conn.commit()

    def mark_unparseable(self, job_id: int) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO parse_status (job_id, status, parsed_at) VALUES (?, 'unparseable', ?)",
            (job_id, _now_iso()),
        )
        self._conn.commit()

    def mark_log_expired(self, job_id: int) -> None:
        """Record that a job's log is gone from GitHub (~90-day retention).

        Stored in parse_status so the job is never re-fetched; distinct
        from 'unparseable' (log present but no test names recognised).
        """
        self._conn.execute(
            "INSERT OR REPLACE INTO parse_status (job_id, status, parsed_at) VALUES (?, 'log_expired', ?)",
            (job_id, _now_iso()),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Weekly release-branch ingest markers
    # ------------------------------------------------------------------

    def get_weekly_ingest_status(self, run_id: int) -> Optional[str]:
        row = self._conn.execute(
            "SELECT status FROM weekly_ingest WHERE run_id = ?", (run_id,)
        ).fetchone()
        return row["status"] if row else None

    def set_weekly_ingest_status(self, run_id: int, status: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO weekly_ingest (run_id, status, ingested_at) VALUES (?, ?, ?)",
            (run_id, status, _now_iso()),
        )
        self._conn.commit()

    def query_failures(
        self,
        job_id: Optional[int] = None,
        test_name_pattern: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
    ) -> List[Dict]:
        clauses: List[str] = []
        params: List[object] = []

        # Always join with parse_status for date filtering
        base = (
            "SELECT tf.job_id, tf.test_name, tf.error_summary, tf.log_lines "
            "FROM test_failures tf "
            "JOIN parse_status ps ON tf.job_id = ps.job_id"
        )

        if job_id is not None:
            clauses.append("tf.job_id = ?")
            params.append(job_id)
        if test_name_pattern is not None:
            clauses.append("tf.test_name LIKE ?")
            params.append(test_name_pattern)
        if since is not None:
            clauses.append("ps.parsed_at >= ?")
            params.append(since)
        if until is not None:
            clauses.append("ps.parsed_at <= ?")
            params.append(until)

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = base + where
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]
