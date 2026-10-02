"""Transactional desired registrations and completion receipts, not browser state."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4


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
                        last_error TEXT,
                        runtime_state TEXT,
                        last_registration TEXT,
                        registration_id TEXT
                    )
                """)
                columns = {
                    row[1] for row in self._db.execute("PRAGMA table_info(watch_records)")
                }
                if "runtime_state" not in columns:
                    self._db.execute("ALTER TABLE watch_records ADD COLUMN runtime_state TEXT")
                if "last_registration" not in columns:
                    self._db.execute("ALTER TABLE watch_records ADD COLUMN last_registration TEXT")
                if "registration_id" not in columns:
                    self._db.execute("ALTER TABLE watch_records ADD COLUMN registration_id TEXT")
                for row in self._db.execute(
                        "SELECT conversation_id FROM watch_records WHERE registration_id IS NULL"):
                    self._db.execute(
                        "UPDATE watch_records SET registration_id=? WHERE conversation_id=?",
                        (str(uuid4()), row[0]),
                    )
                self._db.execute("""
                    CREATE TABLE IF NOT EXISTS pending_withdrawals (
                        conversation_id TEXT PRIMARY KEY,
                        target_url TEXT NOT NULL,
                        registration_id TEXT NOT NULL,
                        provenance TEXT NOT NULL
                    )
                """)
                self._db.execute("""
                    CREATE TABLE IF NOT EXISTS watch_lifecycle (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        conversation_id TEXT NOT NULL,
                        target_url TEXT NOT NULL,
                        operation TEXT NOT NULL,
                        source TEXT NOT NULL,
                        actor TEXT NOT NULL,
                        operation_id TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        at REAL NOT NULL
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
        records = []
        for row in self._db.execute("SELECT * FROM watch_records"):
            record = dict(row)
            raw_state = record.get("runtime_state")
            if raw_state is not None:
                try:
                    runtime_state = json.loads(raw_state)
                except json.JSONDecodeError as error:
                    raise RuntimeError("invalid persisted watchdog runtime state") from error
                if not isinstance(runtime_state, dict):
                    raise RuntimeError("persisted watchdog runtime state must be an object")
                record["runtime_state"] = runtime_state
            raw_registration = record.get("last_registration")
            record["last_registration"] = (
                json.loads(raw_registration) if raw_registration is not None else None
            )
            records.append(record)
        return records

    def register(self, conversation_id: str, target_url: str, at: float, provenance: dict,
                 registration_id: str) -> None:
        with self._db:
            self._db.execute(
                """INSERT INTO watch_records
                   (conversation_id, target_url, status, registered_at, last_registration, registration_id)
                   VALUES (?, ?, 'active', ?, ?, ?)
                   ON CONFLICT(conversation_id) DO UPDATE SET
                   target_url=excluded.target_url, status='active', result=NULL,
                   registered_at=excluded.registered_at, last_poll_at=NULL,
                   last_success_at=NULL, consecutive_failures=0, last_error=NULL,
                   runtime_state=NULL, last_registration=excluded.last_registration,
                   registration_id=excluded.registration_id""",
                (conversation_id, target_url, at, json.dumps(provenance, ensure_ascii=False), registration_id),
            )
            self._record(conversation_id, target_url, "register", provenance)

    def observe(self, entry) -> None:
        runtime_state = (
            None
            if entry.runtime_state is None
            else json.dumps(entry.runtime_state, ensure_ascii=False, separators=(",", ":"))
        )
        with self._db:
            self._db.execute(
                """UPDATE watch_records SET last_poll_at=?, last_success_at=?,
                   consecutive_failures=?, last_error=?, runtime_state=?
                   WHERE conversation_id=? AND status='active'""",
                (entry.last_poll_at, entry.last_success_at, entry.consecutive_failures,
                 entry.last_error, runtime_state, entry.conversation_id),
            )

    def reserve_runtime_state(self, conversation_id: str, registration_id: str,
                              runtime_state: dict) -> None:
        with self._db:
            result = self._db.execute(
                "UPDATE watch_records SET runtime_state=? "
                "WHERE conversation_id=? AND registration_id=? AND status='active'",
                (json.dumps(runtime_state, ensure_ascii=False), conversation_id, registration_id),
            )
            if result.rowcount != 1:
                raise RuntimeError("watch generation was withdrawn before effect reservation")

    def complete(self, conversation_id: str, result: str | None, provenance: dict) -> None:
        with self._db:
            row = self._db.execute(
                "SELECT target_url FROM watch_records WHERE conversation_id=? AND status='active'",
                (conversation_id,),
            ).fetchone()
            self._db.execute(
                "UPDATE watch_records SET status='completed', result=? WHERE conversation_id=?",
                (result, conversation_id),
            )
            if row is not None:
                self._record(conversation_id, row["target_url"], "complete", provenance)

    def remove(self, conversation_id: str, *, status: str, provenance: dict) -> None:
        with self._db:
            row = self._db.execute(
                "SELECT target_url FROM watch_records WHERE conversation_id=? AND status=?",
                (conversation_id, status),
            ).fetchone()
            self._db.execute(
                "DELETE FROM watch_records WHERE conversation_id=? AND status=?",
                (conversation_id, status),
            )
            if row is not None:
                operation = "unregister" if status == "active" else "completion_ack"
                self._record(conversation_id, row["target_url"], operation, provenance)

    def load_withdrawals(self) -> list[dict]:
        return [{
            "conversation_id": row["conversation_id"],
            "target_url": row["target_url"],
            "registration_id": row["registration_id"],
            "provenance": json.loads(row["provenance"]),
        } for row in self._db.execute("SELECT * FROM pending_withdrawals")]

    def begin_withdrawal(self, withdrawal: dict) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO pending_withdrawals "
                "(conversation_id,target_url,registration_id,provenance) VALUES (?,?,?,?)",
                (withdrawal["conversation_id"], withdrawal["target_url"],
                 withdrawal["registration_id"], json.dumps(withdrawal["provenance"])),
            )
            self._db.execute(
                "DELETE FROM watch_records WHERE conversation_id=? AND status='active'",
                (withdrawal["conversation_id"],),
            )
            self._record(withdrawal["conversation_id"], withdrawal["target_url"],
                         "unregister_requested", withdrawal["provenance"])

    def finish_withdrawal(self, withdrawal: dict, at: float) -> None:
        with self._db:
            result = self._db.execute(
                "DELETE FROM pending_withdrawals WHERE conversation_id=? AND registration_id=?",
                (withdrawal["conversation_id"], withdrawal["registration_id"]),
            )
            if result.rowcount:
                self._record(withdrawal["conversation_id"], withdrawal["target_url"], "unregister",
                             {**withdrawal["provenance"], "at": at})

    def _record(self, conversation_id: str, target_url: str, operation: str, provenance: dict) -> None:
        self._db.execute(
            """INSERT INTO watch_lifecycle
               (conversation_id, target_url, operation, source, actor, operation_id, reason, at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (conversation_id, target_url, operation, provenance["source"], provenance["actor"],
             provenance["operation_id"], provenance["reason"], provenance["at"]),
        )

    def lifecycle(self, *, limit: int = 50) -> list[dict]:
        return [dict(row) for row in self._db.execute(
            "SELECT * FROM watch_lifecycle ORDER BY id DESC LIMIT ?", (limit,),
        )]

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
