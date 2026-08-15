"""Two-stage hybrid retrieval — Phase 5.

Stage A: candidate generation (FTS5 + graph neighbours + recency), limit 64.
Stage B: mathematical ranking with the full R_i formula (12 components).

R_i = w_s·S_i + w_l·L_i + w_g·G_i + w_m·M_i + w_c·C_i + w_r·Rcy_i
      + w_f·F_i + w_p·P_i + w_a·A_i - w_u·U_i - w_x·X_i - w_z·Z_i
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Optional

from aoca.config import flags, limits
from aoca.embed import cosine_similarity, get_embed_service
from aoca.graph import (
    AssistantScope, CognitiveNode, MemoryClass, NodeStatus,
    Sensitivity, get_db,
)

log = logging.getLogger("aoca.retrieval")

# ── weights (all sum to ~1 before penalty terms) ──────────────────────────────
_W = dict(
    s=0.30,   # semantic similarity
    l=0.15,   # lexical / FTS rank
    g=0.10,   # graph proximity
    m=0.10,   # memory class bonus
    c=0.10,   # confidence
    r=0.05,   # recency
    f=0.05,   # access frequency
    p=0.05,   # importance (priority)
    a=0.05,   # activation baseline
    u=0.05,   # uncertainty penalty
    x=0.05,   # cross-scope mismatch penalty
    z=0.05,   # archival penalty
)

_MEMORY_CLASS_BONUS = {
    MemoryClass.WORKING: 1.0,
    MemoryClass.EPISODIC: 0.7,
    MemoryClass.SEMANTIC: 0.6,
    MemoryClass.PROCEDURAL: 0.8,
    MemoryClass.PREFERENCE: 0.7,
    MemoryClass.ARCHIVAL: 0.1,
}

_PROHIBITED = {Sensitivity.PROHIBITED, Sensitivity.SECRET_REFERENCE}
_STAGE_A_LIMIT = 64


@dataclass
class CognitiveRetrievalResult:
    node: CognitiveNode
    score: float
    semantic_sim: float = 0.0
    fts_rank: float = 0.0
    graph_proximity: float = 0.0
    explanation: str = ""


def retrieve(
    query: str,
    scope: Optional[AssistantScope] = None,
    limit: int = 10,
    anchor_node_ids: Optional[list[str]] = None,
) -> list[CognitiveRetrievalResult]:
    """Return up to `limit` ranked nodes relevant to `query`."""
    if not flags.enabled("AOCA_RETRIEVAL_ENABLED"):
        return []

    db = get_db()
    now = time.time()

    # ── Stage A: candidate generation ─────────────────────────────────────────
    candidates: dict[str, CognitiveNode] = {}

    # A1: FTS5 full-text search
    fts_nodes = db.fts_search(query, scope=scope, limit=_STAGE_A_LIMIT)
    for n in fts_nodes:
        candidates[n.node_id] = n

    # A2: graph neighbourhood of anchor nodes
    if anchor_node_ids and flags.enabled("AOCA_ACTIVATION_ENABLED"):
        for anchor_id in anchor_node_ids[:8]:
            for node, _ in db.get_neighbors(anchor_id, scope=scope,
                                            max_hops=2, limit=32):
                if node.node_id not in candidates:
                    candidates[node.node_id] = node

    # A3: recency fallback — most-recently-accessed active nodes
    if len(candidates) < 8:
        with db._lock:
            scope_clause = "AND assistant_scope=?" if scope else ""
            params = ([scope.value] if scope else []) + [_STAGE_A_LIMIT - len(candidates)]
            rows = db._conn.execute(
                f"""SELECT * FROM cognitive_nodes
                    WHERE archived=0 AND valid_until IS NULL
                    AND status != 'ARCHIVED'
                    {scope_clause}
                    ORDER BY last_accessed_at DESC LIMIT ?""",
                params,
            ).fetchall()
        for r in rows:
            n = db._row_to_node(r)
            candidates.setdefault(n.node_id, n)

    # Filter out prohibited and archived
    candidates = {
        nid: n for nid, n in candidates.items()
        if n.sensitivity not in _PROHIBITED and not n.archived
        and n.status != NodeStatus.ELIGIBLE_FOR_DELETION
    }

    if not candidates:
        return []

    # ── get query embedding once ──────────────────────────────────────────────
    svc = get_embed_service()
    q_vec = svc.encode(query) if query.strip() else None

    # ── Stage B: ranking ──────────────────────────────────────────────────────
    fts_ids = {n.node_id for n in fts_nodes}
    anchor_set = set(anchor_node_ids or [])
    results: list[CognitiveRetrievalResult] = []

    for nid, node in candidates.items():
        # S_i semantic similarity
        if q_vec:
            node_vec = db.get_embedding(nid)
            S = cosine_similarity(q_vec, node_vec)
        else:
            S = 0.0

        # L_i lexical rank (1.0 if in FTS results, 0 otherwise — simple)
        L = 1.0 if nid in fts_ids else 0.0

        # G_i graph proximity (1.0 if anchor neighbour, 0.5 if 2-hop)
        G = 0.0
        if nid in anchor_set:
            G = 1.0
        elif anchor_node_ids:
            for aid in anchor_node_ids:
                nhbs = db.get_neighbors(aid, scope=scope, max_hops=1, limit=64)
                if any(n.node_id == nid for n, _ in nhbs):
                    G = 1.0
                    break
                nhbs2 = db.get_neighbors(aid, scope=scope, max_hops=2, limit=64)
                if any(n.node_id == nid for n, _ in nhbs2):
                    G = 0.5
                    break

        # M_i memory class bonus
        M = _MEMORY_CLASS_BONUS.get(node.memory_class, 0.5)

        # C_i confidence
        C = node.confidence

        # Rcy_i recency (exponential decay, half-life 24 h)
        age_h = (now - node.last_accessed_at) / 3600.0
        Rcy = math.exp(-0.693 * age_h / 24.0)

        # F_i access frequency (log-scaled, capped at 1)
        F = min(1.0, math.log1p(node.access_count) / math.log1p(100))

        # P_i importance
        P = node.importance

        # A_i activation baseline
        A = min(1.0, max(0.0, node.activation_baseline))

        # U_i uncertainty penalty
        U = node.confidence  # low confidence → low U penalises less; use (1-confidence)
        U = 1.0 - node.confidence  # ponytail: inverted — high uncertainty = high penalty

        # X_i cross-scope mismatch penalty
        X = 0.0
        if scope and node.assistant_scope not in (scope, AssistantScope.SHARED):
            X = 1.0

        # Z_i archival penalty
        Z = 1.0 if node.memory_class == MemoryClass.ARCHIVAL else 0.0

        score = (
            _W["s"] * S + _W["l"] * L + _W["g"] * G + _W["m"] * M
            + _W["c"] * C + _W["r"] * Rcy + _W["f"] * F
            + _W["p"] * P + _W["a"] * A
            - _W["u"] * U - _W["x"] * X - _W["z"] * Z
        )

        explanation = (
            f"S={S:.2f} L={L:.0f} G={G:.1f} M={M:.1f} "
            f"C={C:.2f} Rcy={Rcy:.2f} F={F:.2f} "
            f"P={P:.2f} A={A:.2f} U={U:.2f} X={X:.0f} Z={Z:.0f}"
        )

        results.append(CognitiveRetrievalResult(
            node=node, score=score, semantic_sim=S,
            fts_rank=L, graph_proximity=G, explanation=explanation,
        ))

    results.sort(key=lambda r: r.score, reverse=True)
    return results[:limit]
