from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit, urlunsplit


WATCH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
WATCH_STATES = frozenset({"active", "paused", "completed"})


@dataclass(frozen=True)
class WatchEntry:
    watch_id: str
    target_url: str
    state: str
    reanchor_scope: str | None
    reanchor_epoch: str | None
    last_result: str | None
    created_at: str
    updated_at: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_conversation_url(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("target_url must be a non-empty string")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.netloc != "chatgpt.com":
        raise ValueError("target_url must use https://chatgpt.com")
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("target_url must not contain query, fragment, or credentials")

    segments = parsed.path.split("/")
    if len(segments) < 3 or segments[-2] != "c" or not segments[-1]:
        raise ValueError("target_url must end with /c/<conversation-id>")
    if any(not segment for segment in segments[1:]):
        raise ValueError("target_url must be canonical and contain no empty path segments")

    return urlunsplit(("https", "chatgpt.com", parsed.path, "", ""))


def _validate_watch_id(watch_id: str) -> str:
    if not isinstance(watch_id, str) or not WATCH_ID_RE.fullmatch(watch_id):
        raise ValueError("watch_id must match [A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
    return watch_id


def _validate_state(state: str) -> str:
    if state not in WATCH_STATES:
        raise ValueError(f"state must be one of {sorted(WATCH_STATES)}")
    return state


def _validate_reanchor_pair(scope: str | None, epoch: str | None) -> tuple[str | None, str | None]:
    scope = scope or None
    epoch = epoch or None
    if (scope is None) != (epoch is None):
        raise ValueError("reanchor_scope and reanchor_epoch must be provided together")
    return scope, epoch


class WatchRegistry:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection: sqlite3.Connection | None = sqlite3.connect(self.path)
        self._connection.row_factory = sqlite3.Row
        self._initialize()

    def _conn(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("registry is closed")
        return self._connection

    def _initialize(self) -> None:
        with self._conn():
            self._conn().execute(
                """
                CREATE TABLE IF NOT EXISTS watches (
                    watch_id TEXT PRIMARY KEY,
                    target_url TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL CHECK (state IN ('active', 'paused', 'completed')),
                    reanchor_scope TEXT,
                    reanchor_epoch TEXT,
                    last_result TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK (
                        (reanchor_scope IS NULL AND reanchor_epoch IS NULL)
                        OR (reanchor_scope IS NOT NULL AND reanchor_epoch IS NOT NULL)
                    )
                )
                """
            )

    @staticmethod
    def _entry(row: sqlite3.Row) -> WatchEntry:
        return WatchEntry(
            watch_id=row["watch_id"],
            target_url=row["target_url"],
            state=row["state"],
            reanchor_scope=row["reanchor_scope"],
            reanchor_epoch=row["reanchor_epoch"],
            last_result=row["last_result"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def add(
        self,
        watch_id: str,
        target_url: str,
        *,
        reanchor_scope: str | None = None,
        reanchor_epoch: str | None = None,
    ) -> WatchEntry:
        watch_id = _validate_watch_id(watch_id)
        target_url = canonical_conversation_url(target_url)
        reanchor_scope, reanchor_epoch = _validate_reanchor_pair(
            reanchor_scope,
            reanchor_epoch,
        )
        now = _utc_now()
        try:
            with self._conn():
                self._conn().execute(
                    """
                    INSERT INTO watches (
                        watch_id, target_url, state, reanchor_scope, reanchor_epoch,
                        last_result, created_at, updated_at
                    ) VALUES (?, ?, 'active', ?, ?, NULL, ?, ?)
                    """,
                    (watch_id, target_url, reanchor_scope, reanchor_epoch, now, now),
                )
        except sqlite3.IntegrityError as error:
            raise ValueError("watch_id and target_url must both be unique") from error
        entry = self.get(watch_id)
        assert entry is not None
        return entry

    def get(self, watch_id: str) -> WatchEntry | None:
        _validate_watch_id(watch_id)
        row = self._conn().execute(
            "SELECT * FROM watches WHERE watch_id = ?",
            (watch_id,),
        ).fetchone()
        return None if row is None else self._entry(row)

    def list(self, *, state: str | None = None) -> list[WatchEntry]:
        if state is None:
            rows = self._conn().execute(
                "SELECT * FROM watches ORDER BY created_at, watch_id"
            ).fetchall()
        else:
            _validate_state(state)
            rows = self._conn().execute(
                "SELECT * FROM watches WHERE state = ? ORDER BY created_at, watch_id",
                (state,),
            ).fetchall()
        return [self._entry(row) for row in rows]

    def set_state(self, watch_id: str, state: str) -> WatchEntry:
        watch_id = _validate_watch_id(watch_id)
        state = _validate_state(state)
        now = _utc_now()
        with self._conn():
            cursor = self._conn().execute(
                "UPDATE watches SET state = ?, updated_at = ? WHERE watch_id = ?",
                (state, now, watch_id),
            )
        if cursor.rowcount != 1:
            raise KeyError(watch_id)
        entry = self.get(watch_id)
        assert entry is not None
        return entry

    def record_result(self, watch_id: str, result: str) -> WatchEntry:
        watch_id = _validate_watch_id(watch_id)
        if not isinstance(result, str) or not result:
            raise ValueError("result must be a non-empty string")
        now = _utc_now()
        with self._conn():
            cursor = self._conn().execute(
                "UPDATE watches SET last_result = ?, updated_at = ? WHERE watch_id = ?",
                (result, now, watch_id),
            )
        if cursor.rowcount != 1:
            raise KeyError(watch_id)
        entry = self.get(watch_id)
        assert entry is not None
        return entry

    def remove(self, watch_id: str) -> bool:
        watch_id = _validate_watch_id(watch_id)
        with self._conn():
            cursor = self._conn().execute(
                "DELETE FROM watches WHERE watch_id = ?",
                (watch_id,),
            )
        return cursor.rowcount == 1

    def close(self) -> None:
        if self._connection is None:
            return
        self._connection.close()
        self._connection = None

    def __enter__(self) -> "WatchRegistry":
        return self

    def __exit__(self, *_exc_info) -> None:
        self.close()
