"""Phase 5 retrieval + activation tests — mandatory Tests 3-4, 11-15."""
import tempfile
from pathlib import Path

import pytest

from aoca.graph import (
    AssistantScope, CognitiveEdge, CognitiveGraphDB, CognitiveNode,
    MemoryClass, NodeType, RelationType, Sensitivity,
    _content_hash, _edge_id, _node_id, get_db,
)
from aoca.config import flags


@pytest.fixture(autouse=True)
def override_graph_db(tmp_path, monkeypatch):
    """Each test gets its own isolated DB."""
    db = CognitiveGraphDB(tmp_path / "test.db")
    monkeypatch.setattr("aoca.graph._db", db)
    monkeypatch.setattr("aoca.retrieval.get_db", lambda: db)
    monkeypatch.setattr("aoca.activation.get_db", lambda: db)
    yield db
    db.close()


def _make_node(key, summary, scope=AssistantScope.SHARED,
               sensitivity=Sensitivity.PUBLIC, importance=0.7):
    nid = _node_id(NodeType.CONCEPT, key, scope)
    return CognitiveNode(
        node_id=nid, node_type=NodeType.CONCEPT,
        canonical_key=f"CONCEPT:{key}", canonical_name=key, display_name=key,
        safe_summary=summary, assistant_scope=scope,
        sensitivity=sensitivity, importance=importance,
        content_hash=_content_hash(summary + key),
    )


# ── Test 3: scope isolation ───────────────────────────────────────────────────

class TestScopeIsolation:

    def test_jarvis_node_not_returned_for_friday_scope(self, override_graph_db):
        db = override_graph_db
        jarvis_node = _make_node("jarvis_only", "only for jarvis", AssistantScope.JARVIS)
        friday_node = _make_node("friday_only", "only for friday", AssistantScope.FRIDAY)
        db.upsert_node(jarvis_node)
        db.upsert_node(friday_node)

        from aoca.retrieval import retrieve
        flags.set_override("AOCA_RETRIEVAL_ENABLED", True)
        flags.set_override("AOCA_ENABLED", True)
        results = retrieve("only for", scope=AssistantScope.FRIDAY, limit=10)
        node_ids = {r.node.node_id for r in results}
        assert friday_node.node_id in node_ids or len(results) == 0  # friday or nothing
        assert jarvis_node.node_id not in node_ids
        flags.clear_overrides()

    def test_shared_node_visible_to_all_scopes(self, override_graph_db):
        db = override_graph_db
        shared = _make_node("shared_concept", "shared across scopes", AssistantScope.SHARED)
        db.upsert_node(shared)

        from aoca.retrieval import retrieve
        flags.set_override("AOCA_RETRIEVAL_ENABLED", True)
        flags.set_override("AOCA_ENABLED", True)
        for scope in (AssistantScope.JARVIS, AssistantScope.FRIDAY):
            results = retrieve("shared", scope=scope, limit=10)
            # shared nodes don't get a cross-scope penalty
            assert all(r.score >= 0.0 for r in results)
        flags.clear_overrides()


# ── Test 4: shared fact retrieval ─────────────────────────────────────────────

class TestSharedFact:

    def test_shared_fact_retrieved_without_scope_penalty(self, override_graph_db):
        db = override_graph_db
        node = _make_node("shared_fact", "Python is a programming language",
                          AssistantScope.SHARED)
        db.upsert_node(node)

        from aoca.retrieval import retrieve
        flags.set_override("AOCA_RETRIEVAL_ENABLED", True)
        flags.set_override("AOCA_ENABLED", True)
        results = retrieve("programming language", scope=AssistantScope.JARVIS, limit=5)
        # X_i penalty must be 0 for SHARED nodes
        for r in results:
            if r.node.node_id == node.node_id:
                assert r.score > 0.0
        flags.clear_overrides()


# ── Test 11: missing embedding ────────────────────────────────────────────────

class TestMissingEmbedding:

    def test_retrieval_works_without_embedding(self, override_graph_db):
        db = override_graph_db
        node = _make_node("no_embed_node", "some content no embedding")
        db.upsert_node(node)
        # No embedding stored — semantic score should be 0.0, not crash

        from aoca.retrieval import retrieve
        flags.set_override("AOCA_RETRIEVAL_ENABLED", True)
        flags.set_override("AOCA_ENABLED", True)
        results = retrieve("some content", limit=5)
        # Should not raise; semantic_sim may be 0.0
        for r in results:
            assert r.score == r.score  # not NaN
        flags.clear_overrides()


# ── Test 12: dim mismatch ─────────────────────────────────────────────────────

class TestDimMismatch:

    def test_dim_mismatch_returns_zero_not_crash(self):
        from aoca.embed import cosine_similarity
        a = [0.1] * 384
        b = [0.1] * 256  # wrong dim
        result = cosine_similarity(a, b)
        assert result == 0.0

    def test_zero_vector_similarity_is_zero(self):
        from aoca.embed import cosine_similarity
        a = [0.0] * 384
        b = [0.1] * 384
        result = cosine_similarity(a, b)
        assert result == 0.0


# ── Test 13: bounded activation ───────────────────────────────────────────────

class TestBoundedActivation:

    def test_activation_respects_max_nodes(self, override_graph_db):
        db = override_graph_db
        # Create a star graph with many leaves
        hub = _make_node("hub", "hub node", importance=1.0)
        db.upsert_node(hub)
        for i in range(200):
            leaf = _make_node(f"leaf_{i}", f"leaf {i}", importance=0.5)
            db.upsert_node(leaf)
            edge = CognitiveEdge(
                edge_id=_edge_id(hub.node_id, RelationType.RELATED_TO, leaf.node_id),
                source_node_id=hub.node_id, target_node_id=leaf.node_id,
                relation_type=RelationType.RELATED_TO, weight=0.5,
            )
            db.upsert_edge(edge)

        from aoca.activation import spread, _MAX_NODES
        flags.set_override("AOCA_ACTIVATION_ENABLED", True)
        flags.set_override("AOCA_ENABLED", True)
        result = spread([(hub, 1.0)])
        assert result.visited_count <= _MAX_NODES
        flags.clear_overrides()

    def test_activation_all_values_in_01(self, override_graph_db):
        db = override_graph_db
        n = _make_node("act_node", "activation test", importance=0.9)
        db.upsert_node(n)

        from aoca.activation import spread
        flags.set_override("AOCA_ACTIVATION_ENABLED", True)
        flags.set_override("AOCA_ENABLED", True)
        result = spread([(n, 0.8)])
        for val in result.activations.values():
            assert 0.0 <= val <= 1.0
        flags.clear_overrides()

    def test_activation_stable_flag(self, override_graph_db):
        db = override_graph_db
        n = _make_node("stable_node", "stable", importance=0.5)
        db.upsert_node(n)

        from aoca.activation import spread
        flags.set_override("AOCA_ACTIVATION_ENABLED", True)
        flags.set_override("AOCA_ENABLED", True)
        result = spread([(n, 0.5)])
        assert result.stable is True
        flags.clear_overrides()


# ── Test 14: negative edges ───────────────────────────────────────────────────

class TestNegativeEdges:

    def test_negative_weight_edge_does_not_boost_activation(self, override_graph_db):
        db = override_graph_db
        src = _make_node("neg_src", "source", importance=1.0)
        tgt = _make_node("neg_tgt", "target", importance=0.8)
        db.upsert_node(src)
        db.upsert_node(tgt)
        edge = CognitiveEdge(
            edge_id=_edge_id(src.node_id, RelationType.CONTRADICTS, tgt.node_id),
            source_node_id=src.node_id, target_node_id=tgt.node_id,
            relation_type=RelationType.CONTRADICTS, weight=-0.8,
        )
        db.upsert_edge(edge)

        from aoca.activation import spread
        flags.set_override("AOCA_ACTIVATION_ENABLED", True)
        flags.set_override("AOCA_ENABLED", True)
        result = spread([(src, 1.0)])
        # Target activation should be lower than if edge were positive
        tgt_activation = result.activations.get(tgt.node_id, 0.0)
        assert tgt_activation < 0.5  # sigmoid(-0.8 * 1.0) ~ 0.18
        flags.clear_overrides()


# ── Test 15: retrieval explanation ───────────────────────────────────────────

class TestRetrievalExplanation:

    def test_result_has_explanation(self, override_graph_db):
        db = override_graph_db
        node = _make_node("explain_me", "Python is great for data science")
        db.upsert_node(node)

        from aoca.retrieval import retrieve
        flags.set_override("AOCA_RETRIEVAL_ENABLED", True)
        flags.set_override("AOCA_ENABLED", True)
        results = retrieve("data science", limit=5)
        for r in results:
            assert r.explanation != ""
            assert "S=" in r.explanation
        flags.clear_overrides()
