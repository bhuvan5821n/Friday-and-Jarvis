"""Bounded spreading activation — Phase 5.

a_i^(0) = clip(q_i × importance_i × confidence_i × scope_factor, 0, 1)
Propagation: sigmoid(sum of weighted neighbour activations) × depth_decay
Max depth=3, max nodes=128, max edges=512, epsilon=0.001
Cycle detection via visited set. Emits COGNITIVE_ACTIVATION_UNSTABLE on divergence.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Optional

from aoca.config import flags
from aoca.events import emit
from aoca.graph import AssistantScope, CognitiveNode, get_db

log = logging.getLogger("aoca.activation")

_MAX_DEPTH = 3
_MAX_NODES = 128
_MAX_EDGES = 512
_EPSILON = 0.001
_DEPTH_DECAY = 0.6   # per hop
_SIGMOID_GAIN = 4.0  # steepness


def _sigmoid(x: float) -> float:
    try:
        return 1.0 / (1.0 + math.exp(-_SIGMOID_GAIN * x))
    except OverflowError:
        return 0.0 if x < 0 else 1.0


@dataclass
class ActivationResult:
    activations: dict[str, float] = field(default_factory=dict)
    visited_count: int = 0
    edge_count: int = 0
    stable: bool = True
    depth_reached: int = 0


def spread(
    seed_nodes: list[tuple[CognitiveNode, float]],  # (node, query_similarity)
    scope: Optional[AssistantScope] = None,
) -> ActivationResult:
    """Return activation levels for all reachable nodes within budget."""
    if not flags.enabled("AOCA_ACTIVATION_ENABLED"):
        return ActivationResult()

    db = get_db()
    result = ActivationResult()

    # Initial activations
    activations: dict[str, float] = {}
    for node, q_sim in seed_nodes:
        scope_factor = 1.0 if (scope is None or node.assistant_scope in
                                (scope, AssistantScope.SHARED)) else 0.3
        a0 = min(1.0, max(0.0, q_sim * node.importance * node.confidence * scope_factor))
        if a0 > _EPSILON:
            activations[node.node_id] = a0

    frontier = list(activations.keys())
    visited: set[str] = set(frontier)
    edge_count = 0

    for depth in range(1, _MAX_DEPTH + 1):
        if not frontier or len(visited) >= _MAX_NODES:
            break
        result.depth_reached = depth
        next_frontier: list[str] = []

        for nid in frontier:
            if edge_count >= _MAX_EDGES:
                break
            parent_activation = activations.get(nid, 0.0)
            if parent_activation < _EPSILON:
                continue

            neighbours = db.get_neighbors(nid, scope=scope, max_hops=1, limit=32)
            for child_node, edge in neighbours:
                edge_count += 1
                if edge_count > _MAX_EDGES:
                    break
                cid = child_node.node_id
                # propagate: sigmoid of weighted parent activation × depth decay
                contribution = (
                    _sigmoid(parent_activation * edge.weight)
                    * (_DEPTH_DECAY ** depth)
                    * child_node.importance
                    * child_node.confidence
                )
                if contribution < _EPSILON:
                    continue
                new_val = min(1.0, activations.get(cid, 0.0) + contribution)
                activations[cid] = new_val

                if cid not in visited:
                    visited.add(cid)
                    next_frontier.append(cid)
                    if len(visited) >= _MAX_NODES:
                        break

        frontier = next_frontier

    # Stability check: any activation > 1.0 signals divergence (shouldn't happen with clamp)
    if any(v > 1.0 + 1e-6 for v in activations.values()):
        result.stable = False
        emit("cognitive.activation.unstable", node_count=len(activations))
        log.warning("activation: instability detected — clamping")
        activations = {k: min(1.0, v) for k, v in activations.items()}

    result.activations = activations
    result.visited_count = len(visited)
    result.edge_count = edge_count
    return result
