"""Durable record of what actions actually did.

One table, append-only. It stores the *shape* of an action and how it ended —
never what the user said, never what a page contained. Every column is either a
number, a boolean, an enum value, or a tool name, which is what makes it safe to
keep on disk indefinitely.

`learnable` is written at insert time from `FinalActionOutcome.learnable`, so a
later learning layer selects on a column rather than re-deriving the rule. An
UNVERIFIED row is kept — it is evidence about verifier coverage — but it is
never learnable.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aoca.config import flags, limits
from aoca.verify import FinalActionOutcome

log = logging.getLogger("aoca.outcomes")

DB_PATH = Path(__file__).with_name("outcomes.db")

_SCHEMA_VERSION = 1

_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """
        CREATE TABLE IF NOT EXISTS action_outcomes (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            trace_id                TEXT    NOT NULL,
            span_id                 TEXT    NOT NULL DEFAULT '',
            assistant               TEXT    NOT NULL DEFAULT 'SHARED',
            origin                  TEXT    NOT NULL DEFAULT 'unknown',
            tool                    TEXT    NOT NULL,
            method                  TEXT    NOT NULL DEFAULT '',
            outcome                 TEXT    NOT NULL,
            execution_started       INTEGER NOT NULL,
            verification_completed  INTEGER NOT NULL,
            expected_state_observed INTEGER NOT NULL,
            verifier                TEXT    NOT NULL DEFAULT 'none',
            learnable               INTEGER NOT NULL,
            duration_ms             INTEGER NOT NULL DEFAULT 0,
            error_code              TEXT,
            occurred_at             REAL    NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_outcomes_trace ON action_outcomes(trace_id)",
        "CREATE INDEX IF NOT EXISTS idx_outcomes_tool ON action_outcomes(tool, outcome)",
        "CREATE INDEX IF NOT EXISTS idx_outcomes_learnable "
        "ON action_outcomes(learnable, occurred_at)",
    ),
}


@dataclass
class StoreStats:
    written: int = 0
    failed: int = 0
    pruned: int = 0


class OutcomeStore:
    """SQLite, WAL, one connection guarded by a lock.

    ponytail: one lock and one connection. Writes are a handful per user action,
    so contention is not real; a connection pool is the upgrade if a background
    writer ever appears.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else DB_PATH
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None
        self.stats = StoreStats()

    # ---- connection ------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        with self._lock:
            if self._connection is not None:
                return self._connection
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                str(self.path), check_same_thread=False, timeout=5.0)
            connection.row_factory = sqlite3.Row
            # WAL so a reader never blocks the write path on the UI thread.
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA foreign_keys=ON")
            self._migrate(connection)
            self._connection = connection
            return connection

    def _migrate(self, connection: sqlite3.Connection) -> None:
        current = connection.execute("PRAGMA user_version").fetchone()[0]
        for version in range(current + 1, _SCHEMA_VERSION + 1):
            for statement in _MIGRATIONS.get(version, ()):
                connection.execute(statement)
            connection.execute(f"PRAGMA user_version={version}")
        connection.commit()

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    # ---- writing ---------------------------------------------------------

    def record(self, outcome: FinalActionOutcome,
               trace_id: str = "", span_id: str = "",
               assistant: str = "", origin: str = "",
               error_code: str | None = None) -> int | None:
        """Insert one row. Returns the row id, or None if nothing was written.

        Never raises: a telemetry write must not be able to fail a user action.
        """
        if not flags.enabled("AOCA_OUTCOME_STORAGE_ENABLED"):
            return None

        from aoca import trace as tracing

        context = tracing.current()
        row = (
            trace_id or context.trace_id,
            span_id or context.span_id,
            assistant or context.assistant.value,
            origin or context.origin,
            outcome.tool,
            outcome.execution.method,
            outcome.outcome.value,
            int(outcome.execution.execution_started),
            int(outcome.verification.verification_completed),
            int(outcome.verification.expected_state_observed),
            outcome.verification.verifier,
            int(outcome.learnable),
            outcome.execution.duration_ms + outcome.verification.duration_ms,
            error_code,
            time.time(),
        )
        try:
            with self._lock:
                connection = self._connect()
                cursor = connection.execute(
                    "INSERT INTO action_outcomes ("
                    " trace_id, span_id, assistant, origin, tool, method,"
                    " outcome, execution_started, verification_completed,"
                    " expected_state_observed, verifier, learnable,"
                    " duration_ms, error_code, occurred_at"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
                connection.commit()
                self.stats.written += 1
                if self.stats.written % 500 == 0:
                    self._prune(connection)
                return cursor.lastrowid
        except Exception as exc:
            self.stats.failed += 1
            log.debug("outcome write failed: %s", exc)
            return None

    def _prune(self, connection: sqlite3.Connection) -> None:
        """Keep the table bounded. Oldest rows go first."""
        try:
            total = connection.execute(
                "SELECT COUNT(*) FROM action_outcomes").fetchone()[0]
            excess = total - limits.OUTCOME_ROWS_MAX
            if excess > 0:
                connection.execute(
                    "DELETE FROM action_outcomes WHERE id IN ("
                    " SELECT id FROM action_outcomes ORDER BY id LIMIT ?)",
                    (excess,))
                connection.commit()
                self.stats.pruned += excess
        except Exception as exc:
            log.debug("prune failed: %s", exc)

    # ---- reading ---------------------------------------------------------

    def recent(self, limit: int = 50, tool: str = "") -> list[dict[str, Any]]:
        try:
            with self._lock:
                connection = self._connect()
                if tool:
                    rows = connection.execute(
                        "SELECT * FROM action_outcomes WHERE tool = ?"
                        " ORDER BY id DESC LIMIT ?", (tool, limit)).fetchall()
                else:
                    rows = connection.execute(
                        "SELECT * FROM action_outcomes ORDER BY id DESC"
                        " LIMIT ?", (limit,)).fetchall()
                return [dict(row) for row in rows]
        except Exception as exc:
            log.debug("outcome read failed: %s", exc)
            return []

    def reliability(self, tool: str) -> dict[str, Any]:
        """Observed success rate for one tool, over verified rows only.

        Unverified rows are excluded from the denominator as well as the
        numerator: counting them as failures would punish missing verifiers
        rather than the tool.
        """
        try:
            with self._lock:
                row = self._connect().execute(
                    "SELECT COUNT(*) AS verified,"
                    " SUM(CASE WHEN outcome = 'succeeded' THEN 1 ELSE 0 END)"
                    " AS succeeded FROM action_outcomes"
                    " WHERE tool = ? AND learnable = 1", (tool,)).fetchone()
        except Exception:
            return {"tool": tool, "verified": 0, "succeeded": 0, "rate": None}
        verified = row["verified"] or 0
        succeeded = row["succeeded"] or 0
        return {
            "tool": tool,
            "verified": verified,
            "succeeded": succeeded,
            "rate": (succeeded / verified) if verified else None,
        }

    def counts_by_outcome(self) -> dict[str, int]:
        try:
            with self._lock:
                rows = self._connect().execute(
                    "SELECT outcome, COUNT(*) AS n FROM action_outcomes"
                    " GROUP BY outcome").fetchall()
            return {row["outcome"]: row["n"] for row in rows}
        except Exception:
            return {}


store = OutcomeStore()

import atexit as _atexit
_atexit.register(store.close)
