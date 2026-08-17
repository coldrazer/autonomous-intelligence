from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import InvalidTransition, ReplayConflict
from .models import (
    ActionClass,
    ActionRequest,
    BrokerReceipt,
    BrokerState,
    EngineState,
    canonical_json,
)


ENGINE_TRANSITIONS: dict[EngineState, set[EngineState]] = {
    EngineState.PREPARED: {
        EngineState.DELIVERY_ATTEMPTED,
        EngineState.VERIFIED,
        EngineState.UNCERTAIN,
        EngineState.REJECTED,
        EngineState.EXPIRED,
        EngineState.CANCELLED,
        EngineState.FAILED,
        EngineState.SUPERSEDED,
    },
    EngineState.DELIVERY_ATTEMPTED: {
        EngineState.VERIFIED,
        EngineState.UNCERTAIN,
        EngineState.FAILED,
        EngineState.SUPERSEDED,
    },
}

BROKER_TRANSITIONS: dict[BrokerState, set[BrokerState]] = {
    BrokerState.ACCEPTED: {
        BrokerState.IN_FLIGHT,
        BrokerState.REJECTED,
        BrokerState.EXPIRED,
        BrokerState.CANCELLED,
        BrokerState.FAILED,
    },
    BrokerState.IN_FLIGHT: {
        BrokerState.DELIVERY_ATTEMPTED,
        BrokerState.UNCERTAIN,
        BrokerState.FAILED,
        BrokerState.SUPERSEDED,
    },
    BrokerState.DELIVERY_ATTEMPTED: {
        BrokerState.UNCERTAIN,
        BrokerState.SUPERSEDED,
    },
}


class SQLiteStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.path,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA busy_timeout=5000")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self._connection.close()


class EngineJournal(SQLiteStore):
    def __init__(self, path: str | Path):
        super().__init__(path)
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS engine_attempts (
                attempt_id TEXT PRIMARY KEY,
                logical_operation_id TEXT NOT NULL,
                action TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                state TEXT NOT NULL,
                action_class TEXT,
                result_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_engine_logical_operation
                ON engine_attempts(logical_operation_id);
            CREATE TABLE IF NOT EXISTS engine_transitions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_id TEXT NOT NULL,
                from_state TEXT,
                to_state TEXT NOT NULL,
                metadata_json TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(attempt_id) REFERENCES engine_attempts(attempt_id)
            );
            """
        )

    def prepare(self, request: ActionRequest) -> None:
        payload_json = canonical_json(request.canonical_payload)
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT payload_hash FROM engine_attempts WHERE attempt_id = ?",
                (request.attempt_id,),
            ).fetchone()
            if existing:
                if existing["payload_hash"] != request.payload_hash:
                    raise ReplayConflict("attempt_id reused with a different engine payload")
                return
            connection.execute(
                """
                INSERT INTO engine_attempts (
                    attempt_id, logical_operation_id, action, payload_json,
                    payload_hash, state
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    request.attempt_id,
                    request.logical_operation_id,
                    request.action,
                    payload_json,
                    request.payload_hash,
                    EngineState.PREPARED.value,
                ),
            )
            connection.execute(
                "INSERT INTO engine_transitions(attempt_id, from_state, to_state) VALUES (?, NULL, ?)",
                (request.attempt_id, EngineState.PREPARED.value),
            )

    def get(self, attempt_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM engine_attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        return dict(row) if row else None

    def request_for(self, attempt_id: str) -> ActionRequest:
        row = self.get(attempt_id)
        if not row:
            raise KeyError(attempt_id)
        payload = json.loads(row["payload_json"])
        return ActionRequest.from_dict(payload)

    def pending(self) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            """
            SELECT * FROM engine_attempts
            WHERE state IN (?, ?)
            ORDER BY created_at, attempt_id
            """,
            (EngineState.PREPARED.value, EngineState.DELIVERY_ATTEMPTED.value),
        ).fetchall()
        return [dict(row) for row in rows]

    def attempts_for(self, logical_operation_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT * FROM engine_attempts WHERE logical_operation_id = ? ORDER BY created_at, attempt_id",
            (logical_operation_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            """
            SELECT attempt_id, logical_operation_id, action, state, action_class,
                   error, created_at, updated_at
            FROM engine_attempts
            ORDER BY rowid DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def transition(
        self,
        attempt_id: str,
        target: EngineState,
        *,
        action_class: ActionClass | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT state FROM engine_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if not row:
                raise KeyError(attempt_id)
            current = EngineState(row["state"])
            if current == target:
                return
            if target not in ENGINE_TRANSITIONS.get(current, set()):
                raise InvalidTransition(f"engine: {current.value} -> {target.value}")
            connection.execute(
                """
                UPDATE engine_attempts
                SET state = ?, action_class = COALESCE(?, action_class),
                    result_json = COALESCE(?, result_json), error = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE attempt_id = ?
                """,
                (
                    target.value,
                    action_class.value if action_class else None,
                    canonical_json(result) if result is not None else None,
                    error,
                    attempt_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO engine_transitions(attempt_id, from_state, to_state, metadata_json)
                VALUES (?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    current.value,
                    target.value,
                    canonical_json({"error": error}) if error else None,
                ),
            )


class BrokerLedger(SQLiteStore):
    def __init__(self, path: str | Path):
        super().__init__(path)
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS broker_attempts (
                attempt_id TEXT PRIMARY KEY,
                logical_operation_id TEXT NOT NULL,
                action TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                request_hash TEXT,
                action_class TEXT NOT NULL,
                state TEXT NOT NULL,
                approval_id TEXT,
                result_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_broker_logical_operation
                ON broker_attempts(logical_operation_id);
            CREATE TABLE IF NOT EXISTS broker_transitions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_id TEXT NOT NULL,
                from_state TEXT,
                to_state TEXT NOT NULL,
                metadata_json TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(attempt_id) REFERENCES broker_attempts(attempt_id)
            );
            CREATE TABLE IF NOT EXISTS approvals (
                approval_id TEXT PRIMARY KEY,
                attempt_id TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                token_digest TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                consumed_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(attempt_id) REFERENCES broker_attempts(attempt_id)
            );
            """
        )
        columns = {
            row["name"]
            for row in self._connection.execute("PRAGMA table_info(broker_attempts)").fetchall()
        }
        if "request_hash" not in columns:
            self._connection.execute("ALTER TABLE broker_attempts ADD COLUMN request_hash TEXT")

    def accept(
        self,
        request: ActionRequest,
        action_class: ActionClass,
        normalized_payload: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        payload_json = canonical_json(normalized_payload)
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM broker_attempts WHERE attempt_id = ?", (request.attempt_id,)
            ).fetchone()
            if existing:
                if existing["request_hash"] and existing["request_hash"] != request.payload_hash:
                    raise ReplayConflict("attempt_id reused with a different source request")
                if existing["payload_hash"] != payload_hash:
                    raise ReplayConflict("attempt_id reused with a different broker payload")
                return dict(existing), False
            connection.execute(
                """
                INSERT INTO broker_attempts (
                    attempt_id, logical_operation_id, action, payload_json,
                    payload_hash, request_hash, action_class, state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.attempt_id,
                    request.logical_operation_id,
                    request.action,
                    payload_json,
                    payload_hash,
                    request.payload_hash,
                    action_class.value,
                    BrokerState.ACCEPTED.value,
                ),
            )
            connection.execute(
                "INSERT INTO broker_transitions(attempt_id, from_state, to_state) VALUES (?, NULL, ?)",
                (request.attempt_id, BrokerState.ACCEPTED.value),
            )
            row = connection.execute(
                "SELECT * FROM broker_attempts WHERE attempt_id = ?", (request.attempt_id,)
            ).fetchone()
            return dict(row), True

    def get(self, attempt_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM broker_attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        return dict(row) if row else None

    def transition(
        self,
        attempt_id: str,
        target: BrokerState,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        approval_id: str | None = None,
    ) -> None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT state FROM broker_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if not row:
                raise KeyError(attempt_id)
            current = BrokerState(row["state"])
            if current == target:
                return
            if target not in BROKER_TRANSITIONS.get(current, set()):
                raise InvalidTransition(f"broker: {current.value} -> {target.value}")
            connection.execute(
                """
                UPDATE broker_attempts
                SET state = ?, result_json = COALESCE(?, result_json), error = ?,
                    approval_id = COALESCE(?, approval_id), updated_at = CURRENT_TIMESTAMP
                WHERE attempt_id = ?
                """,
                (
                    target.value,
                    canonical_json(result) if result is not None else None,
                    error,
                    approval_id,
                    attempt_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO broker_transitions(attempt_id, from_state, to_state, metadata_json)
                VALUES (?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    current.value,
                    target.value,
                    canonical_json({"error": error}) if error else None,
                ),
            )

    def receipt(self, attempt_id: str) -> BrokerReceipt | None:
        row = self.get(attempt_id)
        if not row:
            return None
        return BrokerReceipt(
            attempt_id=attempt_id,
            state=BrokerState(row["state"]),
            action_class=ActionClass(row["action_class"]),
            payload_hash=row["payload_hash"],
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            error=row["error"],
        )

    def record_approval(
        self,
        approval_id: str,
        attempt_id: str,
        payload_hash: str,
        token_digest: str,
        expires_at: int,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO approvals(approval_id, attempt_id, payload_hash, token_digest, expires_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (approval_id, attempt_id, payload_hash, token_digest, expires_at),
            )

    def consume_approval(self, approval_id: str, token_digest: str, now: int) -> None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
            ).fetchone()
            if not row:
                raise KeyError(approval_id)
            if row["token_digest"] != token_digest:
                raise ReplayConflict("approval token digest mismatch")
            if row["consumed_at"] is not None:
                raise ReplayConflict("approval token has already been consumed")
            if row["expires_at"] < now:
                raise ReplayConflict("approval token has expired")
            connection.execute(
                "UPDATE approvals SET consumed_at = CURRENT_TIMESTAMP WHERE approval_id = ?",
                (approval_id,),
            )
