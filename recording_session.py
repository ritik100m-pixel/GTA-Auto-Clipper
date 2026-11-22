"""Persistent recording sessions and crash-safe chunk checkpoints."""
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _now():
    return datetime.now(timezone.utc)


def _stamp(value=None):
    return (value or _now()).isoformat(timespec="microseconds")


class RecordingSession:
    def __init__(self, root, session_id=None):
        self.root = Path(root).resolve()
        self.session_id = str(uuid.UUID(session_id)) if session_id else str(uuid.uuid4())
        self.path = self.root / self.session_id
        self.captures_path = self.path / "captures"
        self.path.mkdir(parents=True, exist_ok=True)
        self.captures_path.mkdir(parents=True, exist_ok=True)
        self.db_path = self.path / "session.sqlite3"
        self._initialize()

    @contextmanager
    def _db(self):
        db = sqlite3.connect(str(self.db_path), timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("PRAGMA foreign_keys=ON")
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
                CREATE TABLE IF NOT EXISTS session (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS captures (
                    id TEXT PRIMARY KEY,
                    directory TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    id TEXT PRIMARY KEY,
                    capture_id TEXT NOT NULL REFERENCES captures(id),
                    relative_path TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    outputs_json TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(capture_id, relative_path)
                );
                CREATE INDEX IF NOT EXISTS chunks_ready
                ON chunks(status, lease_expires_at, created_at);
            """)
            db.execute(
                "INSERT OR IGNORE INTO session(id, created_at) VALUES (?, ?)",
                (self.session_id, _stamp())
            )

    def new_capture(self):
        capture_id = str(uuid.uuid4())
        directory = self.captures_path / capture_id
        directory.mkdir(parents=True, exist_ok=False)
        relative = directory.relative_to(self.path).as_posix()
        with self._db() as db:
            db.execute(
                "INSERT INTO captures(id, directory, created_at) VALUES (?, ?, ?)",
                (capture_id, relative, _stamp())
            )
        return {"id": capture_id, "path": str(directory)}

    def register_chunk(self, capture_id, chunk_path, *, max_attempts=3):
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")

        path = Path(chunk_path).resolve()
        with self._db() as db:
            capture = db.execute(
                "SELECT directory FROM captures WHERE id = ?", (capture_id,)
            ).fetchone()
            if capture is None:
                raise ValueError("unknown capture")

            capture_dir = (self.path / capture["directory"]).resolve()
            try:
                relative = path.relative_to(capture_dir).as_posix()
            except ValueError:
                raise ValueError("chunk must be inside its capture directory")

            if not path.is_file() or path.stat().st_size == 0:
                raise ValueError("chunk must exist and be non-empty")

            now = _stamp()
            db.execute("""
                INSERT OR IGNORE INTO chunks(
                    id, capture_id, relative_path, status, max_attempts,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'pending', ?, ?, ?)
            """, (str(uuid.uuid4()), capture_id, relative, max_attempts, now, now))

            row = db.execute("""
                SELECT * FROM chunks
                WHERE capture_id = ? AND relative_path = ?
            """, (capture_id, relative)).fetchone()
            return dict(row)

    def claim_next(self, worker_id, *, lease_seconds=900):
        if not worker_id:
            raise ValueError("worker_id is required")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")

        now = _now()
        now_text = _stamp(now)
        expiry = _stamp(now + timedelta(seconds=lease_seconds))

        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""
                SELECT * FROM chunks
                WHERE attempts < max_attempts
                  AND (
                    status = 'pending'
                    OR (status = 'processing' AND lease_expires_at <= ?)
                  )
                ORDER BY created_at, id
                LIMIT 1
            """, (now_text,)).fetchone()

            if row is None:
                return None

            changed = db.execute("""
                UPDATE chunks
                SET status = 'processing', attempts = attempts + 1,
                    lease_owner = ?, lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND attempts < max_attempts
                  AND (
                    status = 'pending'
                    OR (status = 'processing' AND lease_expires_at <= ?)
                  )
            """, (worker_id, expiry, now_text, row["id"], now_text)).rowcount

            if changed != 1:
                return None

            claimed = db.execute(
                "SELECT * FROM chunks WHERE id = ?", (row["id"],)
            ).fetchone()
            return dict(claimed)

    def renew_lease(self, chunk_id, worker_id, *, lease_seconds=900):
        if not worker_id:
            raise ValueError("worker_id is required")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")

        now = _now()
        now_text = _stamp(now)
        expiry = _stamp(now + timedelta(seconds=lease_seconds))

        with self._db() as db:
            changed = db.execute("""
                UPDATE chunks
                SET lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND status = 'processing'
                  AND lease_owner = ? AND lease_expires_at > ?
            """, (expiry, now_text, chunk_id, worker_id, now_text)).rowcount
            return changed == 1

    def complete_chunk(self, chunk_id, worker_id, outputs):
        """Complete a chunk with zero or more (video_path, metadata_path) pairs."""
        validated_outputs = []

        for pair in outputs:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise ValueError(
                    "each output must contain a video path and metadata path"
                )

            video_path, metadata_path = (
                Path(pair[0]).resolve(),
                Path(pair[1]).resolve()
            )

            if any(not p.is_file() or p.stat().st_size == 0
                   for p in (video_path, metadata_path)):
                raise ValueError(
                    "video and metadata outputs must both exist and be non-empty"
                )

            validated_outputs.append({
                "video": str(video_path),
                "metadata": str(metadata_path)
            })

        now = _stamp()
        with self._db() as db:
            changed = db.execute("""
                UPDATE chunks
                SET status = 'completed', outputs_json = ?,
                    lease_owner = NULL, lease_expires_at = NULL,
                    updated_at = ?
                WHERE id = ? AND status = 'processing' AND lease_owner = ?
                  AND lease_expires_at > ?
            """, (json.dumps(validated_outputs), now,
                  chunk_id, worker_id, now)).rowcount

        completed = changed == 1
        if completed:
            # Rendered MP4/JSON outputs are now durable in the output directory.
            # Drop the large raw transport-stream chunk to keep GitHub cache usage
            # bounded. If completion failed, retain the chunk for recovery.
            try:
                self.resolve_chunk_path(chunk_id).unlink(missing_ok=True)
            except OSError:
                pass
        return completed
    def fail_chunk(self, chunk_id, worker_id, error):
        now = _stamp()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""
                SELECT attempts, max_attempts FROM chunks
                WHERE id = ? AND status = 'processing' AND lease_owner = ?
                  AND lease_expires_at > ?
            """, (chunk_id, worker_id, now)).fetchone()
            if row is None:
                return False

            status = "pending" if row["attempts"] < row["max_attempts"] else "failed"
            db.execute("""
                UPDATE chunks
                SET status = ?, lease_owner = NULL, lease_expires_at = NULL,
                    last_error = ?, updated_at = ?
                WHERE id = ? AND status = 'processing' AND lease_owner = ?
            """, (status, str(error)[:4000], now, chunk_id, worker_id))
            return True

    def get_chunk(self, chunk_id):
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM chunks WHERE id = ?", (chunk_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_chunk_by_path(self, capture_id, relative_path):
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM chunks WHERE capture_id = ? AND relative_path = ?",
                (capture_id, str(relative_path))
            ).fetchone()
            return dict(row) if row else None


    def get_capture(self, capture_id):
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM captures WHERE id = ?", (capture_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_captures(self):
        with self._db() as db:
            rows = db.execute("SELECT * FROM captures ORDER BY created_at, id").fetchall()
            return [dict(row) for row in rows]

    def resolve_chunk_path(self, chunk_or_id):
        if isinstance(chunk_or_id, dict):
            chunk = chunk_or_id
        else:
            chunk = self.get_chunk(chunk_or_id)
        if not chunk:
            raise ValueError(f"unknown chunk: {chunk_or_id}")
        with self._db() as db:
            capture = db.execute(
                "SELECT directory FROM captures WHERE id = ?", (chunk["capture_id"],)
            ).fetchone()
            if not capture:
                raise ValueError(f"unknown capture: {chunk['capture_id']}")
            return (self.path / capture["directory"] / chunk["relative_path"]).resolve()

    def recover_unregistered_chunks(self, capture_id=None):
        """Find and register any valid .ts chunk files on disk that are not yet in the session database."""
        recovered = []
        with self._db() as db:
            if capture_id:
                captures = db.execute(
                    "SELECT id, directory FROM captures WHERE id = ?", (capture_id,)
                ).fetchall()
            else:
                captures = db.execute(
                    "SELECT id, directory FROM captures ORDER BY created_at, id"
                ).fetchall()

            for capture in captures:
                cap_dir = (self.path / capture["directory"]).resolve()
                if not cap_dir.is_dir():
                    continue
                for ts_path in sorted(cap_dir.glob("chunk_*.ts")):
                    if not ts_path.is_file() or ts_path.stat().st_size == 0:
                        continue
                    try:
                        relative = ts_path.relative_to(cap_dir).as_posix()
                    except ValueError:
                        continue
                    existing = db.execute(
                        "SELECT id FROM chunks WHERE capture_id = ? AND relative_path = ?",
                        (capture["id"], relative)
                    ).fetchone()
                    if existing is None:
                        chunk = self.register_chunk(capture["id"], ts_path)
                        recovered.append(chunk)
        return recovered

    def list_recoverable(self):
        now = _stamp()
        with self._db() as db:
            rows = db.execute("""
                SELECT * FROM chunks
                WHERE attempts < max_attempts
                  AND (
                    status = 'pending'
                    OR (status = 'processing' AND lease_expires_at <= ?)
                  )
                ORDER BY created_at, id
            """, (now,)).fetchall()
            return [dict(row) for row in rows]
