"""CognitiveService facade — Phase 4-6.

Integrates graph + memory + retrieval + activation + admission.
Single entry point for main.py prompt construction.
Context budget: 3-5 items (simple), 5-10 (project), up to 20 (explicit search).
Flag-off: returns empty context immediately.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from aoca.config import flags
from aoca.admission import AdmissionDecision, get_policy
from aoca.activation import spread
from aoca.consolidation import get_consolidation_service
from aoca.embed import get_embed_service
from aoca.events import emit
from aoca.graph import (
    AssistantScope, CognitiveNode, MemoryClass, NodeStatus,
    NodeType, Sensitivity, _content_hash, _node_id, get_db,
)
from aoca.memory import MemoryStore, get_store
from aoca.retrieval import CognitiveRetrievalResult, retrieve

log = logging.getLogger("aoca.cognition")


@dataclass
class CognitiveContext:
    """What cognition injects into the prompt."""
    items: list[CognitiveRetrievalResult] = field(default_factory=list)
    working_summaries: list[str] = field(default_factory=list)
    retrieval_ms: float = 0.0

    def is_empty(self) -> bool:
        return not self.items and not self.working_summaries

    def to_prompt_block(self) -> str:
        """Format for insertion into the system prompt."""
        if self.is_empty():
            return ""
        parts: list[str] = ["[Cognitive Memory]"]
        for slot_summary in self.working_summaries:
            parts.append(f"  (working) {slot_summary}")
        for r in self.items:
            scope = r.node.assistant_scope.value
            parts.append(f"  [{scope}] {r.node.display_name}: {r.node.safe_summary}")
        return "\n".join(parts)


class CognitiveService:
    """Main facade. Instantiate once per process via get_cognitive_service()."""

    def __init__(self) -> None:
        self._store = get_store()
        self._started = False
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        if flags.enabled("AOCA_CONSOLIDATION_ENABLED"):
            get_consolidation_service().start()
        log.info("cognition: service started")

    def stop(self) -> None:
        get_consolidation_service().stop(timeout=3.0)
        log.info("cognition: service stopped")

    # ── context retrieval for prompt injection ────────────────────────────────

    def get_context(
        self,
        query: str,
        scope: Optional[AssistantScope] = None,
        mode: str = "simple",   # "simple" | "project" | "search"
        anchor_node_ids: Optional[list[str]] = None,
    ) -> CognitiveContext:
        if not flags.enabled("AOCA_RETRIEVAL_ENABLED"):
            return CognitiveContext()

        get_consolidation_service().set_busy()
        t0 = time.monotonic()
        try:
            limit = {"simple": 5, "project": 10, "search": 20}.get(mode, 5)

            # spreading activation on anchors to boost neighbours
            if anchor_node_ids and flags.enabled("AOCA_ACTIVATION_ENABLED"):
                db = get_db()
                seed_nodes = []
                for nid in anchor_node_ids[:8]:
                    node = db.get_node(nid)
                    if node:
                        seed_nodes.append((node, 1.0))
                if seed_nodes:
                    act = spread(seed_nodes, scope=scope)
                    # boost anchor's neighbours in retrieval via anchor list
                    anchor_node_ids = list(act.activations.keys())[:32]

            results = retrieve(query, scope=scope, limit=limit,
                               anchor_node_ids=anchor_node_ids)

            working = self._store.working.get_active(scope=scope)
            working_summaries = [s.summary for s in working[:5]]

            ctx = CognitiveContext(
                items=results,
                working_summaries=working_summaries,
                retrieval_ms=(time.monotonic() - t0) * 1000,
            )
            emit("cognitive.context.retrieved",
                 items=len(results), working=len(working_summaries),
                 ms=round(ctx.retrieval_ms, 1))
            return ctx
        except Exception as exc:
            log.warning("cognition: get_context failed (%s)", exc)
            return CognitiveContext()
        finally:
            get_consolidation_service().clear_busy()

    # ── memory admission ──────────────────────────────────────────────────────

    def admit(
        self,
        node: CognitiveNode,
        relevance: float = 0.5,
        novelty: float = 0.5,
        goal_alignment: float = 0.5,
        trace_id: str = "",
        embed_text: Optional[str] = None,
    ) -> Optional[CognitiveNode]:
        """Run admission policy and store the node if accepted."""
        if not flags.enabled("AOCA_MEMORY_ADMISSION_ENABLED"):
            return None

        get_consolidation_service().set_busy()
        try:
            result = get_policy().evaluate(
                node, relevance=relevance, novelty=novelty,
                goal_alignment=goal_alignment, trace_id=trace_id,
            )
            if result.decision == AdmissionDecision.REJECT:
                return None
            if result.node is None:
                return None

            # Generate embedding if text provided
            embedding: Optional[list[float]] = None
            if embed_text and flags.enabled("AOCA_GRAPH_ENABLED"):
                embedding = get_embed_service().encode(embed_text)

            return self._store.store(result.node, embedding=embedding, trace_id=trace_id)
        except Exception as exc:
            log.warning("cognition: admit failed (%s)", exc)
            return None
        finally:
            get_consolidation_service().clear_busy()

    # ── convenience builders ──────────────────────────────────────────────────

    def remember_fact(self, key: str, summary: str,
                      scope: AssistantScope = AssistantScope.SHARED,
                      importance: float = 0.5,
                      trace_id: str = "") -> Optional[CognitiveNode]:
        """Convenience: admit a SEMANTIC CONCEPT node."""
        node = CognitiveNode(
            node_id=_node_id(NodeType.CONCEPT, key, scope),
            node_type=NodeType.CONCEPT,
            canonical_key=f"CONCEPT:{key}",
            canonical_name=key, display_name=key,
            safe_summary=summary[:400],
            assistant_scope=scope,
            importance=importance,
            content_hash=_content_hash(summary + key),
            source_type="conversation",
        )
        return self.admit(node, relevance=0.7, novelty=0.6,
                          trace_id=trace_id, embed_text=summary)


# ── module singleton ──────────────────────────────────────────────────────────
_svc: Optional[CognitiveService] = None
_svc_lock = threading.Lock()


def get_cognitive_service() -> CognitiveService:
    global _svc
    if _svc is None:
        with _svc_lock:
            if _svc is None:
                _svc = CognitiveService()
                _svc.start()
    return _svc
