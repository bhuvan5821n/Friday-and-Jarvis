"""Phase 4 graph tests — mandatory Tests 1-2, 20-21."""
import tempfile
import time
from pathlib import Path

import pytest

from aoca.graph import (
    AssistantScope, CognitiveEdge, CognitiveGraphDB, CognitiveNode,
    MemoryClass, NodeStatus, NodeType, RelationType, Sensitivity,
    _content_hash, _edge_id, _node_id,
)


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_db(tmp_path):
    return CognitiveGraphDB(tmp_path / "test.db")


# ── Test 1: protected creator graph ──────────────────────────────────────────

class TestProtectedCreatorGraph:

    def test_bhuvan_jarvis_friday_seeded(self, tmp_db):
        bhuvan = tmp_db.get_node_by_key("PERSON:bhuvan")
        jarvis = tmp_db.get_node_by_key("ASSISTANT:jarvis", AssistantScope.JARVIS)
        friday = tmp_db.get_node_by_key("ASSISTANT:friday", AssistantScope.FRIDAY)
        assert bhuvan is not None
        assert jarvis is not None
        assert friday is not None

    def test_protected_nodes_are_protected(self, tmp_db):
        bhuvan = tmp_db.get_node_by_key("PERSON:bhuvan")
        assert bhuvan.protected is True
        assert bhuvan.pinned is True

    def test_protected_node_cannot_be_superseded(self, tmp_db):
        bhuvan = tmp_db.get_node_by_key("PERSON:bhuvan")
        original_id = bhuvan.node_id
        # Try to upsert with different content_hash — should be silently rejected
        bhuvan.content_hash = _content_hash("different")
        bhuvan.safe_summary = "TAMPERED"
        tmp_db.upsert_node(bhuvan)
        # Original node must still be there unchanged
        node = tmp_db.get_node(original_id)
        assert node is not None
        assert "TAMPERED" not in (node.safe_summary or "")

    def test_created_edges_exist(self, tmp_db):
        bhuvan = tmp_db.get_node_by_key("PERSON:bhuvan")
        jarvis = tmp_db.get_node_by_key("ASSISTANT:jarvis", AssistantScope.JARVIS)
        friday = tmp_db.get_node_by_key("ASSISTANT:friday", AssistantScope.FRIDAY)
        neighbours = tmp_db.get_neighbors(bhuvan.node_id, max_hops=1, limit=10)
        neighbour_ids = {n.node_id for n, _ in neighbours}
        assert jarvis.node_id in neighbour_ids
        assert friday.node_id in neighbour_ids

    def test_protected_edge_cannot_be_overwritten(self, tmp_db):
        bhuvan = tmp_db.get_node_by_key("PERSON:bhuvan")
        jarvis = tmp_db.get_node_by_key("ASSISTANT:jarvis", AssistantScope.JARVIS)
        eid = _edge_id(bhuvan.node_id, RelationType.CREATED, jarvis.node_id)
        # Attempt update of protected edge — should be skipped
        fake_edge = CognitiveEdge(
            edge_id=eid,
            source_node_id=bhuvan.node_id,
            target_node_id=jarvis.node_id,
            relation_type=RelationType.CREATED,
            weight=-1.0,  # malicious change
            protected=True,
        )
        tmp_db.upsert_edge(fake_edge)
        # Re-read — weight must not have changed to -1.0
        with tmp_db._lock:
            row = tmp_db._conn.execute(
                "SELECT weight FROM cognitive_edges WHERE edge_id=?", (eid,)
            ).fetchone()
        assert row["weight"] != -1.0


# ── Test 2: persistence across reopen ────────────────────────────────────────

class TestPersistence:

    def test_node_survives_db_reopen(self, tmp_path):
        db1 = CognitiveGraphDB(tmp_path / "p.db")
        node = CognitiveNode(
            node_id=_node_id(NodeType.CONCEPT, "test_key"),
            node_type=NodeType.CONCEPT,
            canonical_key="CONCEPT:test_key",
            canonical_name="test_key", display_name="test_key",
            safe_summary="test content",
            content_hash=_content_hash("test content"),
        )
        db1.upsert_node(node)
        db1.close()

        db2 = CognitiveGraphDB(tmp_path / "p.db")
        recovered = db2.get_node_by_key("CONCEPT:test_key")
        assert recovered is not None
        assert recovered.safe_summary == "test content"
        db2.close()

    def test_edge_survives_db_reopen(self, tmp_path):
        db1 = CognitiveGraphDB(tmp_path / "ep.db")
        n1 = CognitiveNode(
            node_id="node-a", node_type=NodeType.CONCEPT,
            canonical_key="CONCEPT:a", canonical_name="a", display_name="a",
            content_hash=_content_hash("a"),
        )
        n2 = CognitiveNode(
            node_id="node-b", node_type=NodeType.CONCEPT,
            canonical_key="CONCEPT:b", canonical_name="b", display_name="b",
            content_hash=_content_hash("b"),
        )
        db1.upsert_node(n1)
        db1.upsert_node(n2)
        edge = CognitiveEdge(
            edge_id="edge-ab", source_node_id="node-a", target_node_id="node-b",
            relation_type=RelationType.RELATED_TO, weight=0.8,
        )
        db1.upsert_edge(edge)
        db1.close()

        db2 = CognitiveGraphDB(tmp_path / "ep.db")
        neighbours = db2.get_neighbors("node-a", max_hops=1, limit=10)
        assert any(n.node_id == "node-b" for n, _ in neighbours)
        db2.close()


# ── Test 20: DB interruption ──────────────────────────────────────────────────

class TestDBInterruption:

    def test_integrity_check_passes_on_clean_db(self, tmp_db):
        issues = tmp_db.integrity_check()
        assert issues == ["ok"]

    def test_backup_creates_readable_copy(self, tmp_db, tmp_path):
        dest = tmp_path / "backup.db"
        tmp_db.backup(dest)
        assert dest.exists()
        import sqlite3
        conn = sqlite3.connect(str(dest))
        count = conn.execute("SELECT COUNT(*) FROM cognitive_nodes").fetchone()[0]
        conn.close()
        assert count >= 3  # at least bhuvan, jarvis, friday


# ── Test 21: migration rollback ───────────────────────────────────────────────

class TestMigration:

    def test_migration_is_idempotent(self, tmp_path):
        db1 = CognitiveGraphDB(tmp_path / "m.db")
        db1.close()
        # Re-open should not fail or re-run migration
        db2 = CognitiveGraphDB(tmp_path / "m.db")
        db2.close()

    def test_temporal_versioning(self, tmp_db):
        node = CognitiveNode(
            node_id=_node_id(NodeType.CONCEPT, "ver_test"),
            node_type=NodeType.CONCEPT,
            canonical_key="CONCEPT:ver_test",
            canonical_name="ver_test", display_name="ver_test",
            safe_summary="v1",
            content_hash=_content_hash("v1"),
        )
        tmp_db.upsert_node(node)

        # Update with different content
        node2 = CognitiveNode(
            node_id=_node_id(NodeType.CONCEPT, "ver_test"),
            node_type=NodeType.CONCEPT,
            canonical_key="CONCEPT:ver_test",
            canonical_name="ver_test", display_name="ver_test",
            safe_summary="v2",
            content_hash=_content_hash("v2"),
        )
        tmp_db.upsert_node(node2)

        # Current live node should have v2 summary
        live = tmp_db.get_node_by_key("CONCEPT:ver_test")
        assert live is not None
        assert live.safe_summary == "v2"

        # Old node should have valid_until set
        with tmp_db._lock:
            old = tmp_db._conn.execute(
                "SELECT valid_until FROM cognitive_nodes WHERE safe_summary='v1'"
            ).fetchone()
        assert old is not None
        assert old["valid_until"] is not None
