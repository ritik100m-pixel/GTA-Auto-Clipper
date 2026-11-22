"""Persistent job queue and batch ledger for GTA Auto Clipper Cloud.

Uses only Python's standard library. No media processing or publishing is
performed by this module.
"""
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path


def utc_now():
    return datetime.now(timezone.utc)


def iso_time(value=None):
    return (value or utc_now()).isoformat(timespec="seconds")


class CloudQueue:
    def __init__(self, db_path="cloud_state/queue.sqlite3"):
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _db(self):
        db = sqlite3.connect(str(self.db_path), timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _initialize(self):
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    job_type TEXT NOT NULL,
                    job_key TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    authorized INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'queued',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    available_at TEXT NOT NULL,
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_jobs_ready
                ON jobs(status, authorized, available_at);

                CREATE INDEX IF NOT EXISTS idx_jobs_lease
                ON jobs(status, lease_expires_at);

                CREATE TABLE IF NOT EXISTS batches (
                    id TEXT PRIMARY KEY,
                    worker_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    deadline_at TEXT NOT NULL,
                    finished_at TEXT,
                    last_error TEXT
                );
            """)

    def enqueue(self, job_type, job_key, payload, *, authorized=False,
                max_attempts=3, available_at=None):
        """Insert once. Existing job_key values are never duplicated."""
        if not job_type or not job_key:
            raise ValueError("job_type and job_key are required")
        if not isinstance(payload, dict):
            raise TypeError("payload must be a dictionary")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")

        now = iso_time()
        job_id = str(uuid.uuid4())
        with self._db() as db:
            db.execute("""
                INSERT OR IGNORE INTO jobs (
                    id, job_type, job_key, payload_json, authorized,
                    status, attempts, max_attempts, available_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'queued', 0, ?, ?, ?, ?)
            """, (
                job_id, job_type, job_key,
                json.dumps(payload, ensure_ascii=False),
                int(bool(authorized)), max_attempts,
                available_at or now, now, now
            ))
            row = db.execute(
                "SELECT * FROM jobs WHERE job_key = ?", (job_key,)
            ).fetchone()
            return self._job_dict(row)

    @staticmethod
    def _job_dict(row):
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        result["authorized"] = bool(result["authorized"])
        return result

    def claim_next(self, worker_id, *, lease_seconds=900, job_types=None):
        """Atomically claim one authorized job, recovering expired leases."""
        if not worker_id:
            raise ValueError("worker_id is required")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")

        now = utc_now()
        now_text = iso_time(now)
        expiry_text = iso_time(now + timedelta(seconds=lease_seconds))

        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")

            # A worker that disappeared must not hold a job forever.
            db.execute("""
                UPDATE jobs
                SET status = CASE
                    WHEN attempts >= max_attempts THEN 'failed'
                    ELSE 'queued'
                END,
                lease_owner = NULL,
                lease_expires_at = NULL,
                updated_at = ?
                WHERE status = 'running'
                  AND lease_expires_at IS NOT NULL
                  AND lease_expires_at <= ?
            """, (now_text, now_text))

            params = [now_text]
            type_filter = ""
            if job_types:
                placeholders = ",".join("?" for _ in job_types)
                type_filter = f" AND job_type IN ({placeholders})"
                params.extend(job_types)

            row = db.execute(f"""
                SELECT * FROM jobs
                WHERE status = 'queued'
                  AND authorized = 1
                  AND available_at <= ?
                  {type_filter}
                ORDER BY created_at ASC
                LIMIT 1
            """, params).fetchone()

            if row is None:
                return None

            changed = db.execute("""
                UPDATE jobs
                SET status = 'running',
                    attempts = attempts + 1,
                    lease_owner = ?,
                    lease_expires_at = ?,
                    updated_at = ?
                WHERE id = ? AND status = 'queued'
            """, (worker_id, expiry_text, now_text, row["id"])).rowcount

            if changed != 1:
                return None

            claimed = db.execute(
                "SELECT * FROM jobs WHERE id = ?", (row["id"],)
            ).fetchone()
            return self._job_dict(claimed)

    def heartbeat(self, job_id, worker_id, *, lease_seconds=900):
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        now = utc_now()
        now_text = iso_time(now)
        expires = iso_time(now + timedelta(seconds=lease_seconds))
        with self._db() as db:
            changed = db.execute("""
                UPDATE jobs
                SET lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND lease_owner = ?
                  AND lease_expires_at > ?
            """, (expires, now_text, job_id, worker_id, now_text)).rowcount
            return changed == 1

    def complete(self, job_id, worker_id):
        now_text = iso_time()
        with self._db() as db:
            changed = db.execute("""
                UPDATE jobs
                SET status = 'completed',
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    updated_at = ?
                WHERE id = ? AND status = 'running' AND lease_owner = ?
                  AND lease_expires_at > ?
            """, (now_text, job_id, worker_id, now_text)).rowcount
            return changed == 1

    def fail(self, job_id, worker_id, error, *, retry_delay_seconds=60):
        """Retry after a delay, or mark failed when attempts are exhausted."""
        now = utc_now()
        now_text = iso_time(now)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""
                SELECT attempts, max_attempts FROM jobs
                WHERE id = ? AND status = 'running' AND lease_owner = ?
                  AND lease_expires_at > ?
            """, (job_id, worker_id, now_text)).fetchone()
            if row is None:
                return False

            retry = row["attempts"] < row["max_attempts"]
            status = "queued" if retry else "failed"
            available = iso_time(
                now + timedelta(seconds=max(0, retry_delay_seconds))
            ) if retry else now_text

            db.execute("""
                UPDATE jobs
                SET status = ?, available_at = ?,
                    lease_owner = NULL, lease_expires_at = NULL,
                    last_error = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND lease_owner = ?
            """, (status, available, str(error)[:4000], now_text, job_id, worker_id))
            return True

    def get_job(self, job_id):
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            return self._job_dict(row)

    def start_batch(self, worker_id, *, duration_seconds=21600):
        """Create a batch ledger entry. Does not launch a worker."""
        if duration_seconds < 1:
            raise ValueError("duration_seconds must be positive")
        started = utc_now()
        batch_id = str(uuid.uuid4())
        with self._db() as db:
            db.execute("""
                INSERT INTO batches (
                    id, worker_id, status, started_at, deadline_at
                ) VALUES (?, ?, 'running', ?, ?)
            """, (
                batch_id, worker_id, iso_time(started),
                iso_time(started + timedelta(seconds=duration_seconds))
            ))
        return batch_id

    def finish_batch(self, batch_id, *, error=None):
        with self._db() as db:
            changed = db.execute("""
                UPDATE batches
                SET status = ?, finished_at = ?, last_error = ?
                WHERE id = ? AND status = 'running'
            """, (
                "failed" if error else "completed",
                iso_time(), str(error)[:4000] if error else None, batch_id
            )).rowcount
            return changed == 1

    def list_batches(self, limit=20):
        with self._db() as db:
            rows = db.execute("""
                SELECT * FROM batches
                ORDER BY started_at DESC LIMIT ?
            """, (max(1, min(int(limit), 100)),)).fetchall()
            return [dict(row) for row in rows]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Inspect cloud queue state")
    parser.add_argument("--db", default="cloud_state/queue.sqlite3")
    args = parser.parse_args()

    queue = CloudQueue(args.db)
    print("Database:", queue.db_path)
    print("Recent batches:")
    for batch in queue.list_batches():
        print(batch)
