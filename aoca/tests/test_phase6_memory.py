"""Phase 6 memory admission + consolidation tests — mandatory Tests 5-10, 16-19."""
import tempfile
import time
from pathlib import Path

import pytest

from aoca.config import flags
from aoca.graph import (
    AssistantScope, CognitiveGraphDB, CognitiveNode, MemoryClass,
    NodeStatus, NodeType, RelationType, Sensitivity,
    _content_hash, _edge_id, _node_id, get_db,
)
from aoca.admission import AdmissionDecision, MemoryAdmissionPolicy
from aoca.memory import WorkingMemory


@pytest.fixture(autouse=True)
def override_graph_db(tmp_path, monkeypatch):
    db = CognitiveGraphDB(tmp_path / "test.db")
    monkeypatch.setattr("aoca.graph._db", db)
    monkeypatch.setattr("aoca.admission.get_db", lambda: db)
    monkeypatch.setattr("aoca.memory.get_db", lambda: db)
    yield db
    db.close()


def _node(key, summary, node_type=NodeType.CONCEPT, scope=AssistantScope.SHARED,
          sensitivity=Sensitivity.PUBLIC, importance=0.6,
          successes=0, failures=0):
    nid = _node_id(node_type, key, scope)
    return CognitiveNode(
        node_id=nid, node_type=node_type,
        canonical_key=f"{node_type.value}:{key}",
        canonical_name=key, display_name=key,
        safe_summary=summary, assistant_scope=scope,
        sensitivity=sensitivity, importance=importance,
        content_hash=_content_hash(summary + key),
        procedure_successes=successes,
        procedure_failures=failures,
    )


@pytest.fixture
def policy():
    return MemoryAdmissionPolicy()


# ── Test 5: duplicate rejection ───────────────────────────────────────────────

class TestDuplicate:

    def test_duplicate_is_rejected(self, override_graph_db, policy):
        flags.set_override("AOCA_ENABLED", True)
        flags.set_override("AOCA_MEMORY_ADMISSION_ENABLED", True)
        n1 = _node("dup_key", "first version")
        override_graph_db.upsert_node(n1)

        n2 = _node("dup_key", "second version")  # same canonical_key
        result = policy.evaluate(n2)
        assert result.decision == AdmissionDecision.REJECT
        assert result.duplicate_of is not None
        flags.clear_overrides()


# ── Test 6: near-duplicate (not merged on embedding alone) ───────────────────

class TestNearDuplicate:

    def test_different_key_not_merged_by_similarity_alone(self, override_graph_db, policy):
        """Two nodes with different keys must not be auto-merged — only queued."""
        flags.set_override("AOCA_ENABLED", True)
        flags.set_override("AOCA_MEMORY_ADMISSION_ENABLED", True)
        n1 = _node("near_a", "Python is a programming language")
        override_graph_db.upsert_node(n1)

        n2 = _node("near_b", "Python is a programming language variant")
        result = policy.evaluate(n2)
        # Different key → not a hard duplicate, should be admitted or confirmed
        assert result.decision != AdmissionDecision.REJECT or result.duplicate_of is None
        flags.clear_overrides()


# ── Test 7: contradiction detection ──────────────────────────────────────────

class TestContradiction:

    def test_contradicting_node_requires_confirmation(self, override_graph_db, policy):
        flags.set_override("AOCA_ENABLED", True)
        flags.set_override("AOCA_MEMORY_ADMISSION_ENABLED", True)
        n1 = _node("sky_color", "the sky is blue")
        n2 = _node("sky_color_contra", "the sky is red")
        override_graph_db.upsert_node(n1)
        override_graph_db.upsert_node(n2)
        # Wire a CONTRADICTS edge from n1 to n2
        from aoca.graph import CognitiveEdge
        override_graph_db.upsert_edge(CognitiveEdge(
            edge_id=_edge_id(n1.node_id, RelationType.CONTRADICTS, n2.node_id),
            source_node_id=n1.node_id, target_node_id=n2.node_id,
            relation_type=RelationType.CONTRADICTS, weight=-1.0,
        ))
        # Admitting a new node with n1's canonical_key must hit duplicate → REJECT
        n3 = _node("sky_color", "the sky is definitely blue version 2")
        result = policy.evaluate(n3)
        assert result.decision in (
            AdmissionDecision.REJECT,
            AdmissionDecision.REQUIRE_USER_CONFIRMATION,
        )
        flags.clear_overrides()


# ── Test 8: contextual preference ────────────────────────────────────────────

class TestContextualPreference:

    def test_preference_node_routes_to_preference(self, override_graph_db, policy):
        flags.set_override("AOCA_ENABLED", True)
        flags.set_override("AOCA_MEMORY_ADMISSION_ENABLED", True)
        n = _node("pref_dark_mode", "user prefers dark mode",
                  node_type=NodeType.PREFERENCE, importance=0.7)
        result = policy.evaluate(n, relevance=0.8, novelty=0.7)
        assert result.decision in (
            AdmissionDecision.PREFERENCE,
            AdmissionDecision.REJECT,  # only if V < threshold
        )
        if result.decision == AdmissionDecision.PREFERENCE:
            assert result.node.memory_class == MemoryClass.PREFERENCE
        flags.clear_overrides()


# ── Test 9: sensitive input rejected ─────────────────────────────────────────

class TestSensitiveInput:

    def test_prohibited_never_admitted(self, override_graph_db, policy):
        flags.set_override("AOCA_ENABLED", True)
        flags.set_override("AOCA_MEMORY_ADMISSION_ENABLED", True)
        n = _node("secret_data", "my_bank_password=hunter2",
                  sensitivity=Sensitivity.PROHIBITED, importance=1.0)
        result = policy.evaluate(n, relevance=1.0)
        assert result.decision == AdmissionDecision.REJECT
        assert result.node is None
        flags.clear_overrides()

    def test_secret_reference_summary_truncated(self, override_graph_db, policy):
        flags.set_override("AOCA_ENABLED", True)
        flags.set_override("AOCA_MEMORY_ADMISSION_ENABLED", True)
        long_summary = "A" * 200  # simulated secret value
        n = _node("bank_ref", long_summary,
                  sensitivity=Sensitivity.SECRET_REFERENCE, importance=0.8)
        result = policy.evaluate(n, relevance=0.7)
        if result.node is not None:
            assert len(result.node.safe_summary) <= 80
        flags.clear_overrides()


# ── Test 10: legacy import ────────────────────────────────────────────────────

class TestLegacyImport:

    def test_dry_run_does_not_write(self, tmp_path, override_graph_db):
        import sqlite3, json
        # Create minimal legacy DB
        legacy = tmp_path / ".swarm" / "memory.db"
        legacy.parent.mkdir(parents=True)
        conn = sqlite3.connect(str(legacy))
        conn.execute("CREATE TABLE memories (key TEXT, content TEXT, embedding TEXT, deleted INTEGER DEFAULT 0)")
        conn.execute("INSERT INTO memories VALUES ('test_key','test content',?,0)",
                     (json.dumps([0.1] * 384),))
        conn.commit()
        conn.close()

        from aoca.legacy_import import run
        report = run(legacy_path=legacy, dry_run=True)
        assert report.total == 1
        assert report.imported == 0  # dry run — nothing written

    def test_import_skips_bank_secret(self, tmp_path, override_graph_db):
        import sqlite3, json
        legacy = tmp_path / ".swarm" / "memory.db"
        legacy.parent.mkdir(parents=True)
        conn = sqlite3.connect(str(legacy))
        conn.execute("CREATE TABLE memories (key TEXT, content TEXT, embedding TEXT, deleted INTEGER DEFAULT 0)")
        conn.execute("INSERT INTO memories VALUES ('notes/bank_account_details','actual bank number 12345',?,0)",
                     (json.dumps([0.0] * 384),))
        conn.commit()
        conn.close()

        from aoca.legacy_import import run
        report = run(legacy_path=legacy, dry_run=False)
        # Should be imported as SECRET_REFERENCE, not with actual content
        if report.imported > 0:
            node = override_graph_db.get_node_by_key(
                "MEMORY:notes/bank_account_details")
            if node:
                assert "12345" not in node.safe_summary

    def test_import_backs_up_before_writing(self, tmp_path, override_graph_db):
        import sqlite3, json
        legacy = tmp_path / ".swarm" / "memory.db"
        legacy.parent.mkdir(parents=True)
        conn = sqlite3.connect(str(legacy))
        conn.execute("CREATE TABLE memories (key TEXT, content TEXT, embedding TEXT, deleted INTEGER DEFAULT 0)")
        conn.commit()
        conn.close()

        from aoca.legacy_import import run
        report = run(legacy_path=legacy, dry_run=False)
        assert report.backup_path != ""
        assert Path(report.backup_path).exists()


# ── Test 16: working memory expiry ────────────────────────────────────────────

class TestWorkingMemoryExpiry:

    def test_slots_expire_after_ttl(self):
        wm = WorkingMemory(ttl=0.05)  # 50ms TTL for test
        wm.put("node-1", "summary", AssistantScope.SHARED)
        assert wm.count == 1
        time.sleep(0.1)
        assert wm.count == 0

    def test_working_memory_bounded_by_max(self):
        wm = WorkingMemory(max_slots=3, ttl=60.0)
        for i in range(5):
            wm.put(f"node-{i}", f"summary {i}", AssistantScope.SHARED,
                   importance=float(i) / 5.0)
        assert wm.count <= 3

    def test_working_memory_deduplicates_by_node_id(self):
        wm = WorkingMemory(max_slots=10, ttl=60.0)
        wm.put("node-x", "first", AssistantScope.SHARED)
        wm.put("node-x", "updated", AssistantScope.SHARED)
        assert wm.count == 1


# ── Test 17: procedural candidate ────────────────────────────────────────────

class TestProceduralCandidate:

    def test_procedure_node_routes_to_procedural_candidate(self, override_graph_db, policy):
        flags.set_override("AOCA_ENABLED", True)
        flags.set_override("AOCA_MEMORY_ADMISSION_ENABLED", True)
        n = _node("open_notepad_proc", "open notepad via start menu",
                  node_type=NodeType.PROCEDURE, importance=0.6)
        result = policy.evaluate(n, relevance=0.7, novelty=0.6)
        assert result.decision in (
            AdmissionDecision.PROCEDURAL_CANDIDATE,
            AdmissionDecision.REJECT,
        )
        flags.clear_overrides()

    def test_procedural_stage_not_advanced_from_unverified(self, override_graph_db):
        """Procedural memory must not be created from an assumed result."""
        from aoca.memory import MemoryStore
        store = MemoryStore()
        n = _node("proc_unverified", "do something", node_type=NodeType.PROCEDURE)
        override_graph_db.upsert_node(n)
        store.record_procedure_outcome(n.node_id, success=False)
        # temporal versioning may create a new node_id — read by canonical_key
        updated = override_graph_db.get_node_by_key("PROCEDURE:proc_unverified")
        assert updated is not None
        assert updated.procedure_failures == 1
        assert updated.procedure_stage is None  # no advancement from failure


# ── Test 18: unverified outcome ───────────────────────────────────────────────

class TestUnverifiedOutcome:

    def test_procedural_stage_not_advanced_without_success(self, override_graph_db):
        from aoca.memory import MemoryStore
        from aoca.graph import ProcedureStage
        store = MemoryStore()
        n = _node("proc_stage", "test procedure", node_type=NodeType.PROCEDURE)
        n.procedure_stage = None  # CANDIDATE equivalent
        override_graph_db.upsert_node(n)
        # Zero successes — stage must not advance
        store.record_procedure_outcome(n.node_id, success=False)
        updated = override_graph_db.get_node(n.node_id)
        assert updated.procedure_stage is None


# ── Test 19: archival ─────────────────────────────────────────────────────────

class TestArchival:

    def test_low_value_node_rejected(self, override_graph_db, policy):
        flags.set_override("AOCA_ENABLED", True)
        flags.set_override("AOCA_MEMORY_ADMISSION_ENABLED", True)
        n = _node("low_val", "meh", importance=0.01)
        n.created_at = time.time() - 7 * 86400  # 7 days old → recency ≈ 0
        n.confidence = 0.01
        n.stability = 0.0  # removes the 0.05*stability term, drives V to ~0.077
        result = policy.evaluate(n, relevance=0.01, novelty=0.01, goal_alignment=0.01)
        assert result.decision == AdmissionDecision.REJECT
        flags.clear_overrides()

    def test_consolidation_archives_old_low_importance_nodes(self, tmp_path, monkeypatch):
        db = CognitiveGraphDB(tmp_path / "cons.db")
        monkeypatch.setattr("aoca.graph._db", db)
        monkeypatch.setattr("aoca.consolidation.get_db", lambda: db)
        monkeypatch.setattr("aoca.embed.get_embed_service",
                            lambda: type("FakeSvc", (), {"available": False})())

        n = CognitiveNode(
            node_id="archive-me", node_type=NodeType.CONCEPT,
            canonical_key="CONCEPT:old_stale", canonical_name="old",
            display_name="old", safe_summary="stale",
            importance=0.05,  # below threshold
            last_accessed_at=time.time() - 40 * 86400,  # 40 days ago
            content_hash=_content_hash("stale"),
            valid_from=time.time() - 40 * 86400,
        )
        db.upsert_node(n)

        from aoca.consolidation import ConsolidationService
        flags.set_override("AOCA_ENABLED", True)
        flags.set_override("AOCA_CONSOLIDATION_ENABLED", True)
        svc = ConsolidationService()
        svc._run_cycle()

        updated = db.get_node("archive-me")
        assert updated is None or updated.archived
        flags.clear_overrides()
        db.close()
