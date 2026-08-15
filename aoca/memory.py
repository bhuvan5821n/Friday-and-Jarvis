"""Memory hierarchy types and MemoryStore — Phase 4/5.

Five live tiers (WORKING → PREFERENCE) backed by the cognitive graph.
ARCHIVAL is stored in the graph but not in any in-process structure.

Working memory is bounded by count and TTL; all other tiers persist to the DB.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from aoca.config import flags
from aoca.graph import (
    AssistantScope, CognitiveEdge, CognitiveNode, MemoryClass,
    NodeStatus, NodeType, ProcedureStage, RelationType, Sensitivity,
    _content_hash, _edge_id, _node_id, get_db,
)

log = logging.getLogger("aoca.memory")

_WORKING_TTL = 300.0    # 5 min
_WORKING_MAX = 64       # slots


# ── working memory ────────────────────────────────────────────────────────────

@dataclass
class WorkingSlot:
    node_id: str
    summary: str
    scope: AssistantScope
    expires_at: float
    trace_id: str = ""
    importance: float = 0.5


class WorkingMemory:
    """Bounded in-process ring with TTL eviction. Not persisted."""

    def __init__(self, max_slots: int = _WORKING_MAX, ttl: float = _WORKING_TTL) -> None:
        self._max = max_slots
        self._ttl = ttl
        self._slots: list[WorkingSlot] = []
        self._lock = threading.Lock()

    def put(self, node_id: str, summary: str, scope: AssistantScope,
            trace_id: str = "", importance: float = 0.5) -> None:
        now = time.time()
        slot = WorkingSlot(node_id=node_id, summary=summary, scope=scope,
                           expires_at=now + self._ttl, trace_id=trace_id,
                           importance=importance)
        with self._lock:
            self._evict(now)
            # deduplicate by node_id
            self._slots = [s for s in self._slots if s.node_id != node_id]
            self._slots.append(slot)
            if len(self._slots) > self._max:
                # drop lowest importance
                self._slots.sort(key=lambda s: s.importance, reverse=True)
                self._slots = self._slots[:self._max]

    def get_active(self, scope: Optional[AssistantScope] = None) -> list[WorkingSlot]:
        now = time.time()
        with self._lock:
            self._evict(now)
            if scope is None:
                return list(self._slots)
            return [s for s in self._slots
                    if s.scope == scope or s.scope == AssistantScope.SHARED]

    def _evict(self, now: float) -> None:
        self._slots = [s for s in self._slots if s.expires_at > now]

    def clear(self) -> None:
        with self._lock:
            self._slots.clear()

    @property
    def count(self) -> int:
        now = time.time()
        with self._lock:
            self._evict(now)
            return len(self._slots)


# ── memory store (graph-backed tiers) ────────────────────────────────────────

class MemoryStore:
    """Thin facade over CognitiveGraphDB for the five durable memory tiers.

    Callers supply a CognitiveNode already classified by the admission policy.
    This class just persists it and attaches an embedding.
    """

    def __init__(self) -> None:
        self.working = WorkingMemory()

    # ── write path ────────────────────────────────────────────────────────────

    def store(self, node: CognitiveNode, embedding: Optional[list[float]] = None,
              trace_id: str = "") -> CognitiveNode:
        """Persist node to the graph and optionally store its embedding."""
        if not flags.enabled("AOCA_MEMORY_ENABLED"):
            return node

        db = get_db()
        node = db.upsert_node(node)

        if embedding and len(embedding) > 0:
            db.store_embedding(node.node_id, embedding,
                               model="Xenova/all-MiniLM-L6-v2",
                               content_hash=node.content_hash)

        if node.memory_class == MemoryClass.WORKING:
            self.working.put(node.node_id, node.safe_summary,
                             node.assistant_scope, trace_id, node.importance)

        return node

    def link(self, src_id: str, rel: RelationType, tgt_id: str,
             scope: AssistantScope = AssistantScope.SHARED,
             weight: float = 0.5, provenance: str = "",
             protected: bool = False) -> CognitiveEdge:
        edge = CognitiveEdge(
            edge_id=_edge_id(src_id, rel, tgt_id),
            source_node_id=src_id, target_node_id=tgt_id,
            relation_type=rel, assistant_scope=scope,
            weight=weight, provenance=provenance, protected=protected,
        )
        return get_db().upsert_edge(edge)

    # ── read path ─────────────────────────────────────────────────────────────

    def get(self, node_id: str) -> Optional[CognitiveNode]:
        return get_db().get_node(node_id)

    def get_by_key(self, key: str, scope: AssistantScope = AssistantScope.SHARED
                   ) -> Optional[CognitiveNode]:
        return get_db().get_node_by_key(key, scope)

    def neighbors(self, node_id: str, scope: Optional[AssistantScope] = None,
                  max_hops: int = 1, limit: int = 32
                  ) -> list[tuple[CognitiveNode, CognitiveEdge]]:
        return get_db().get_neighbors(node_id, scope, max_hops, limit)

    # ── procedural helpers ────────────────────────────────────────────────────

    def record_procedure_outcome(self, node_id: str, success: bool) -> None:
        """Update success/failure counters and advance ProcedureStage."""
        db = get_db()
        node = db.get_node(node_id)
        if node is None or node.node_type != NodeType.PROCEDURE:
            return
        if success:
            node.procedure_successes += 1
        else:
            node.procedure_failures += 1

        # stage advancement: CANDIDATE→OBSERVED(1 success)→REPEATED(3)→TRUSTED(5)
        if node.procedure_stage == ProcedureStage.CANDIDATE and node.procedure_successes >= 1:
            node.procedure_stage = ProcedureStage.OBSERVED
        elif node.procedure_stage == ProcedureStage.OBSERVED and node.procedure_successes >= 3:
            node.procedure_stage = ProcedureStage.REPEATED
        elif node.procedure_stage == ProcedureStage.REPEATED and node.procedure_successes >= 5:
            node.procedure_stage = ProcedureStage.TRUSTED

        node.content_hash = _content_hash(
            f"{node.node_id}:{node.procedure_successes}:{node.procedure_failures}"
        )
        db.upsert_node(node)


# module singleton
_store: Optional[MemoryStore] = None
_store_lock = threading.Lock()


def get_store() -> MemoryStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = MemoryStore()
    return _store
