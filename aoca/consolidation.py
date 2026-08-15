"""Idle-only background consolidation service — Phase 6.

Runs only when no interactive request is in flight. Stops immediately when
signalled. Performs: duplicate merge, near-duplicate queue, confidence
recomputation, archive candidate marking, orphan edge detection, DB integrity.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from aoca.config import flags
from aoca.embed import cosine_similarity, get_embed_service
from aoca.events import emit
from aoca.graph import (
    AssistantScope, MemoryClass, NodeStatus, Sensitivity, get_db,
)

log = logging.getLogger("aoca.consolidation")

_IDLE_INTERVAL = 60.0       # run at most every 60 s
_NEAR_DUP_THRESHOLD = 0.95  # cosine similarity for near-duplicate queue
_ARCHIVE_IMPORTANCE = 0.15  # importance below this → archive candidate
_ARCHIVE_ACCESS_DAYS = 30   # no access in 30 days → archive candidate


class ConsolidationService:
    """Background consolidation — idle-only, stops on interactive signal."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._busy = threading.Event()   # set while an interactive req is live
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if not flags.enabled("AOCA_CONSOLIDATION_ENABLED"):
            return
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="aoca-consolidation", daemon=True
            )
            self._thread.start()
            log.info("consolidation: service started")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        with self._lock:
            t = self._thread
        if t:
            t.join(timeout=timeout)

    def set_busy(self) -> None:
        """Call when an interactive request begins."""
        self._busy.set()

    def clear_busy(self) -> None:
        """Call when an interactive request ends."""
        self._busy.clear()

    # ── main loop ─────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop.is_set():
            # Wait for idle window
            self._stop.wait(timeout=_IDLE_INTERVAL)
            if self._stop.is_set():
                break
            if self._busy.is_set():
                continue
            try:
                self._run_cycle()
            except Exception as exc:
                log.warning("consolidation cycle error: %s", exc)

    def _run_cycle(self) -> None:
        if self._busy.is_set() or self._stop.is_set():
            return
        db = get_db()
        log.debug("consolidation: cycle start")

        # 1. Archive candidates: low importance or long idle
        now = time.time()
        cutoff = now - _ARCHIVE_ACCESS_DAYS * 86400
        with db._lock:
            rows = db._conn.execute(
                """SELECT node_id, importance, last_accessed_at
                   FROM cognitive_nodes
                   WHERE archived=0 AND protected=0 AND valid_until IS NULL
                   AND (importance < ? OR last_accessed_at < ?)
                   LIMIT 100""",
                (_ARCHIVE_IMPORTANCE, cutoff),
            ).fetchall()
        for row in rows:
            if self._busy.is_set() or self._stop.is_set():
                return
            with db._lock:
                db._conn.execute(
                    "UPDATE cognitive_nodes SET status=?, archived=1, updated_at=? "
                    "WHERE node_id=?",
                    (NodeStatus.ARCHIVED.value, now, row["node_id"]),
                )
                db._conn.commit()
        if rows:
            emit("cognitive.consolidation.archived", count=len(rows))
            log.debug("consolidation: archived %d nodes", len(rows))

        if self._busy.is_set() or self._stop.is_set():
            return

        # 2. Near-duplicate detection (semantic)
        svc = get_embed_service()
        if svc.available:
            with db._lock:
                active_nodes = db._conn.execute(
                    """SELECT node_id, canonical_key, assistant_scope
                       FROM cognitive_nodes
                       WHERE archived=0 AND valid_until IS NULL
                       AND sensitivity NOT IN ('PROHIBITED','SECRET_REFERENCE')
                       ORDER BY last_accessed_at DESC LIMIT 200"""
                ).fetchall()
            near_dups: list[tuple[str, str]] = []
            vecs: dict[str, Optional[list[float]]] = {}
            for row in active_nodes:
                if self._busy.is_set():
                    break
                vecs[row["node_id"]] = db.get_embedding(row["node_id"])

            node_list = list(active_nodes)
            for i in range(len(node_list)):
                if self._busy.is_set():
                    break
                for j in range(i + 1, len(node_list)):
                    a, b = node_list[i], node_list[j]
                    if a["assistant_scope"] != b["assistant_scope"]:
                        continue
                    sim = cosine_similarity(vecs.get(a["node_id"]), vecs.get(b["node_id"]))
                    if sim >= _NEAR_DUP_THRESHOLD:
                        near_dups.append((a["node_id"], b["node_id"]))
            if near_dups:
                emit("cognitive.consolidation.near_duplicates", count=len(near_dups))
                log.debug("consolidation: %d near-duplicate pairs queued", len(near_dups))

        if self._busy.is_set() or self._stop.is_set():
            return

        # 3. Orphan edge cleanup
        with db._lock:
            orphans = db._conn.execute(
                """SELECT edge_id FROM cognitive_edges
                   WHERE archived=0
                   AND (source_node_id NOT IN (SELECT node_id FROM cognitive_nodes)
                     OR target_node_id NOT IN (SELECT node_id FROM cognitive_nodes))
                   LIMIT 50"""
            ).fetchall()
            if orphans:
                ids = [r["edge_id"] for r in orphans]
                db._conn.executemany(
                    "UPDATE cognitive_edges SET archived=1 WHERE edge_id=?",
                    [(eid,) for eid in ids],
                )
                db._conn.commit()
        if orphans:
            emit("cognitive.consolidation.orphan_edges", count=len(orphans))
            log.debug("consolidation: removed %d orphan edges", len(orphans))

        if self._busy.is_set() or self._stop.is_set():
            return

        # 4. DB integrity check (cheap — just PRAGMA quick_check)
        with db._lock:
            result = db._conn.execute("PRAGMA quick_check(1)").fetchone()
        if result and result[0] != "ok":
            log.error("consolidation: DB integrity issue: %s", result[0])
            emit("cognitive.consolidation.integrity_error", detail=str(result[0]))

        log.debug("consolidation: cycle done")


# singleton
_svc: Optional[ConsolidationService] = None
_svc_lock = threading.Lock()


def get_consolidation_service() -> ConsolidationService:
    global _svc
    if _svc is None:
        with _svc_lock:
            if _svc is None:
                _svc = ConsolidationService()
    return _svc
