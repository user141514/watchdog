"""Transactional desired registrations and completion receipts, not browser state."""
from __future__ import annotations

import os
from pathlib import Path
import sqlite3


class RegistryStore:
    """One live process owns a store; the kernel releases ownership after a crash.

    The caller serializes connection access. FULL synchronous commits happen before
    registration/withdrawal/completion acknowledgements. Transport objects are never
    serialized, and shutdown does not remove desired registrations.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = str(Path(path).expanduser().resolve()) if path is not None else None
        self._lock_file = None
        self._owns_lock = False
        self._db = None
        try:
            if self.path is not None:
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
                self._acquire_owner()
            self._db = sqlite3.connect(self.path or ":memory:", check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.execute("PRAGMA synchronous=FULL")
            tables = {
                row[0] for row in self._db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            if tables and "watch_records" not in tables:
                raise RuntimeError("incompatible registry store; use a separate database")
            self._db.execute("PRAGMA journal_mode=WAL")
            with self._db:
                self._db.execute("""
                    CREATE TABLE IF NOT EXISTS watch_records (
                        conversation_id TEXT PRIMARY KEY,
                        target_url TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN ('active', 'completed')),
                        result TEXT,
                        registered_at REAL NOT NULL,
                        last_poll_at REAL,
                        last_success_at REAL,
                        consecutive_failures INTEGER NOT NULL DEFAULT 0,
                        last_error TEXT
                    )
                """)
        except BaseException:
            self.close()
            raise

    def _acquire_owner(self) -> None:
        self._lock_file = open(self.path + ".lock", "a+b")
        if self._lock_file.tell() == 0:
            self._lock_file.write(b"\0")
            self._lock_file.flush()
        self._lock_file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError(f"registry store is already owned: {self.path}") from error
        self._owns_lock = True

    def load(self) -> list[dict]:
        return [dict(row) for row in self._db.execute("SELECT * FROM watch_records")]

    def register(self, conversation_id: str, target_url: str, at: float) -> None:
        with self._db:
            self._db.execute(
                """INSERT INTO watch_records
                   (conversation_id, target_url, status, registered_at)
                   VALUES (?, ?, 'active', ?)
                   ON CONFLICT(conversation_id) DO UPDATE SET
                   target_url=excluded.target_url, status='active', result=NULL,
                   registered_at=excluded.registered_at, last_poll_at=NULL,
                   last_success_at=NULL, consecutive_failures=0, last_error=NULL""",
                (conversation_id, target_url, at),
            )

    def observe(self, entry) -> None:
        with self._db:
            self._db.execute(
                """UPDATE watch_records SET last_poll_at=?, last_success_at=?,
                   consecutive_failures=?, last_error=?
                   WHERE conversation_id=? AND status='active'""",
                (entry.last_poll_at, entry.last_success_at, entry.consecutive_failures,
                 entry.last_error, entry.conversation_id),
            )

    def complete(self, conversation_id: str, result: str | None) -> None:
        with self._db:
            self._db.execute(
                "UPDATE watch_records SET status='completed', result=? WHERE conversation_id=?",
                (result, conversation_id),
            )

    def remove(self, conversation_id: str, *, status: str) -> None:
        with self._db:
            self._db.execute(
                "DELETE FROM watch_records WHERE conversation_id=? AND status=?",
                (conversation_id, status),
            )

    def close(self) -> None:
        try:
            if self._db is not None:
                self._db.close()
                self._db = None
        finally:
            if self._lock_file is not None:
                try:
                    if self._owns_lock:
                        self._lock_file.seek(0)
                        if os.name == "nt":
                            import msvcrt
                            msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
                finally:
                    self._lock_file.close()
                    self._lock_file = None
                    self._owns_lock = False
