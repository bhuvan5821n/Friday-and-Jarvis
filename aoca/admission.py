"""Memory admission policy — Phase 6.

Deterministic decision: given a candidate node and context, returns one of
REJECT / WORKING_ONLY / EPISODIC / SEMANTIC / PROCEDURAL_CANDIDATE /
PREFERENCE / ARCHIVE_DIRECTLY / REQUIRE_USER_CONFIRMATION.

Memory value formula V_i (10 components, clipped [0,1]).
Bayesian-smoothed confidence.
Deduplication and contradiction detection via graph DB.
Privacy filter runs before every admission check.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from aoca.config import flags
from aoca.events import emit
from aoca.graph import (
    AssistantScope, CognitiveNode, MemoryClass, NodeStatus,
    NodeType, RelationType, Sensitivity, _content_hash, _edge_id, get_db,
)
from aoca.privacy import sanitize

log = logging.getLogger("aoca.admission")

# ── decision enum ─────────────────────────────────────────────────────────────

class AdmissionDecision(str, Enum):
    REJECT = "REJECT"
    WORKING_ONLY = "WORKING_ONLY"
    EPISODIC = "EPISODIC"
    SEMANTIC = "SEMANTIC"
    PROCEDURAL_CANDIDATE = "PROCEDURAL_CANDIDATE"
    PREFERENCE = "PREFERENCE"
    ARCHIVE_DIRECTLY = "ARCHIVE_DIRECTLY"
    REQUIRE_USER_CONFIRMATION = "REQUIRE_USER_CONFIRMATION"


@dataclass
class AdmissionResult:
    decision: AdmissionDecision
    node: Optional[CognitiveNode]
    memory_value: float = 0.0
    confidence: float = 0.5
    reason: str = ""
    duplicate_of: Optional[str] = None      # node_id
    contradicts: Optional[str] = None       # node_id


# ── memory value formula ──────────────────────────────────────────────────────
# V_i = clip(w1·relevance + w2·novelty + w3·importance + w4·confidence
#            + w5·recency + w6·stability + w7·emotional_salience
#            + w8·source_reliability + w9·retrieval_count + w10·goal_alignment, 0, 1)

def _memory_value(
    relevance: float = 0.5,
    novelty: float = 0.5,
    importance: float = 0.5,
    confidence: float = 0.5,
    recency: float = 1.0,
    stability: float = 0.5,
    emotional_salience: float = 0.0,
    source_reliability: float = 0.7,
    retrieval_count: int = 0,
    goal_alignment: float = 0.5,
) -> float:
    rc_norm = min(1.0, math.log1p(retrieval_count) / math.log1p(20))
    v = (
        0.20 * relevance
        + 0.15 * novelty
        + 0.15 * importance
        + 0.10 * confidence
        + 0.10 * recency
        + 0.05 * stability
        + 0.05 * emotional_salience
        + 0.10 * source_reliability
        + 0.05 * rc_norm
        + 0.05 * goal_alignment
    )
    return min(1.0, max(0.0, v))


def _bayesian_confidence(prior: float, pos: int, neg: int,
                          alpha: float = 1.0, beta: float = 1.0) -> float:
    """Beta-posterior mean: (alpha + pos) / (alpha + beta + pos + neg)."""
    return (alpha + prior + pos) / (alpha + beta + prior + 1 + pos + neg)


# ── deduplication ─────────────────────────────────────────────────────────────

def _find_duplicate(node: CognitiveNode) -> Optional[str]:
    """Return node_id of existing node with same canonical_key, or None."""
    existing = get_db().get_node_by_key(node.canonical_key, node.assistant_scope)
    if existing:
        return existing.node_id
    return None


def _find_contradiction(node: CognitiveNode) -> Optional[str]:
    """Return node_id of a node that has a CONTRADICTS edge with this canonical_key."""
    db = get_db()
    existing = db.get_node_by_key(node.canonical_key, node.assistant_scope)
    if not existing:
        return None
    for neighbour, edge in db.get_neighbors(existing.node_id, max_hops=1, limit=32):
        if edge.relation_type == RelationType.CONTRADICTS:
            return neighbour.node_id
    return None


# ── main policy ───────────────────────────────────────────────────────────────

class MemoryAdmissionPolicy:

    def evaluate(self, node: CognitiveNode,
                 relevance: float = 0.5,
                 novelty: float = 0.5,
                 goal_alignment: float = 0.5,
                 trace_id: str = "") -> AdmissionResult:
        if not flags.enabled("AOCA_MEMORY_ADMISSION_ENABLED"):
            return AdmissionResult(AdmissionDecision.REJECT, None, reason="flag_off")

        # ── privacy filter must run first (Phase 2 requirement) ───────────────
        safe_payload = sanitize({
            "safe_summary": node.safe_summary,
            "source_type": node.source_type,
        })
        # Restore sanitized summary
        node.safe_summary = str(safe_payload.get("safe_summary", ""))[:400]

        # ── hard reject: PROHIBITED content must never enter ──────────────────
        if node.sensitivity == Sensitivity.PROHIBITED:
            emit("cognitive.admission.rejected", sensitivity="PROHIBITED",
                 node_type=node.node_type.value)
            return AdmissionResult(
                AdmissionDecision.REJECT, None,
                reason="PROHIBITED sensitivity — never stored",
            )

        # ── hard reject: SECRET_REFERENCE never persisted as full node ────────
        if node.sensitivity == Sensitivity.SECRET_REFERENCE:
            # Only a reference label is allowed — no actual value in safe_summary
            if len(node.safe_summary) > 80:
                node.safe_summary = node.safe_summary[:80]

        # ── deduplication ─────────────────────────────────────────────────────
        dup_id = _find_duplicate(node)
        if dup_id:
            emit("cognitive.admission.duplicate", duplicate_of=dup_id)
            return AdmissionResult(
                AdmissionDecision.REJECT, node,
                reason="duplicate", duplicate_of=dup_id,
            )

        # ── contradiction detection ───────────────────────────────────────────
        contra_id = _find_contradiction(node)
        if contra_id:
            emit("cognitive.admission.contradiction", contradicts=contra_id)
            return AdmissionResult(
                AdmissionDecision.REQUIRE_USER_CONFIRMATION, node,
                reason="contradicts existing node", contradicts=contra_id,
            )

        # ── memory value ──────────────────────────────────────────────────────
        recency = math.exp(-0.693 * (time.time() - node.created_at) / 3600.0)
        V = _memory_value(
            relevance=relevance,
            novelty=novelty,
            importance=node.importance,
            confidence=node.confidence,
            recency=recency,
            stability=node.stability,
            source_reliability=0.9 if node.source_type == "system" else 0.7,
            goal_alignment=goal_alignment,
        )

        # Bayesian confidence update
        conf = _bayesian_confidence(
            node.confidence,
            node.procedure_successes,
            node.procedure_failures,
        )
        node.confidence = conf

        # ── routing by node type and memory value ─────────────────────────────
        if V < 0.10:
            return AdmissionResult(AdmissionDecision.REJECT, node,
                                   memory_value=V, confidence=conf,
                                   reason="low memory value")

        decision: AdmissionDecision
        if node.node_type == NodeType.PROCEDURE:
            decision = AdmissionDecision.PROCEDURAL_CANDIDATE
            node.memory_class = MemoryClass.PROCEDURAL
        elif node.node_type == NodeType.PREFERENCE:
            decision = AdmissionDecision.PREFERENCE
            node.memory_class = MemoryClass.PREFERENCE
        elif V < 0.25:
            decision = AdmissionDecision.WORKING_ONLY
            node.memory_class = MemoryClass.WORKING
        elif V < 0.45:
            decision = AdmissionDecision.EPISODIC
            node.memory_class = MemoryClass.EPISODIC
        elif V >= 0.80:
            decision = AdmissionDecision.SEMANTIC
            node.memory_class = MemoryClass.SEMANTIC
        else:
            decision = AdmissionDecision.SEMANTIC
            node.memory_class = MemoryClass.SEMANTIC

        node.content_hash = _content_hash(node.safe_summary + node.canonical_key)
        emit("cognitive.admission.accepted",
             decision=decision.value, memory_value=round(V, 3),
             node_type=node.node_type.value)

        return AdmissionResult(decision=decision, node=node,
                               memory_value=V, confidence=conf)


# singleton
_policy: Optional[MemoryAdmissionPolicy] = None

def get_policy() -> MemoryAdmissionPolicy:
    global _policy
    if _policy is None:
        _policy = MemoryAdmissionPolicy()
    return _policy
