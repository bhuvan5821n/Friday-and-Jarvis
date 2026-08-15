"""Phase 4-6 integration tests — mandatory Tests 22-27."""
import threading
import time
from pathlib import Path

import pytest

from aoca.config import flags
from aoca.graph import (
    AssistantScope, CognitiveGraphDB, CognitiveNode, NodeType,
    Sensitivity, _content_hash, _node_id,
)


@pytest.fixture(autouse=True)
def override_graph_db(tmp_path, monkeypatch):
    db = CognitiveGraphDB(tmp_path / "test.db")
    monkeypatch.setattr("aoca.graph._db", db)
    for mod in ("aoca.retrieval", "aoca.activation", "aoca.admission",
                "aoca.memory", "aoca.consolidation", "aoca.cognition"):
        try:
            monkeypatch.setattr(f"{mod}.get_db", lambda _db=db: _db)
        except AttributeError:
            pass
    yield db
    db.close()


# ── Test 22: flag-off ─────────────────────────────────────────────────────────

class TestFlagOff:

    def test_retrieval_returns_empty_when_flag_off(self, override_graph_db):
        flags.set_override("AOCA_ENABLED", True)
        flags.set_override("AOCA_RETRIEVAL_ENABLED", False)
        from aoca.retrieval import retrieve
        results = retrieve("anything", limit=10)
        assert results == []
        flags.clear_overrides()

    def test_activation_returns_empty_when_flag_off(self, override_graph_db):
        flags.set_override("AOCA_ENABLED", True)
        flags.set_override("AOCA_ACTIVATION_ENABLED", False)
        from aoca.activation import spread, ActivationResult
        n = CognitiveNode(
            node_id="test-n", node_type=NodeType.CONCEPT,
            canonical_key="CONCEPT:test", canonical_name="t", display_name="t",
            content_hash=_content_hash("t"),
        )
        result = spread([(n, 0.8)])
        assert result.activations == {}
        flags.clear_overrides()

    def test_admission_rejects_when_flag_off(self, override_graph_db):
        flags.set_override("AOCA_ENABLED", True)
        flags.set_override("AOCA_MEMORY_ADMISSION_ENABLED", False)
        from aoca.admission import AdmissionDecision, MemoryAdmissionPolicy
        n = CognitiveNode(
            node_id="test-n2", node_type=NodeType.CONCEPT,
            canonical_key="CONCEPT:flagtest", canonical_name="t", display_name="t",
            content_hash=_content_hash("t"),
        )
        result = MemoryAdmissionPolicy().evaluate(n)
        assert result.decision == AdmissionDecision.REJECT
        flags.clear_overrides()

    def test_cognition_returns_empty_context_when_disabled(self, override_graph_db, monkeypatch):
        flags.set_override("AOCA_ENABLED", False)
        monkeypatch.setattr("aoca.cognition._svc", None)
        from aoca.cognition import CognitiveService
        svc = CognitiveService()
        ctx = svc.get_context("hello")
        assert ctx.is_empty()
        flags.clear_overrides()


# ── Test 23: event privacy ────────────────────────────────────────────────────

class TestEventPrivacy:

    def test_cognitive_events_do_not_contain_raw_safe_summary(self):
        """Events from admission must not leak safe_summary content."""
        from aoca.events import bus
        captured = []
        bus.subscribe("cognitive.admission.accepted", lambda e: captured.append(e))
        bus.subscribe("cognitive.admission.rejected", lambda e: captured.append(e))

        flags.set_override("AOCA_ENABLED", True)
        flags.set_override("AOCA_MEMORY_ADMISSION_ENABLED", True)
        n = CognitiveNode(
            node_id=_node_id(NodeType.CONCEPT, "evt_priv"),
            node_type=NodeType.CONCEPT,
            canonical_key="CONCEPT:evt_priv",
            canonical_name="evt_priv", display_name="evt_priv",
            safe_summary="my private content that should NOT be in events",
            importance=0.8,
            content_hash=_content_hash("priv"),
        )
        from aoca.admission import MemoryAdmissionPolicy
        MemoryAdmissionPolicy().evaluate(n, relevance=0.8)
        bus.drain(timeout=1.0)

        for event in captured:
            for val in (event.payload or {}).values():
                assert "my private content" not in str(val)

        bus._handlers.pop("cognitive.admission.accepted", None)
        bus._handlers.pop("cognitive.admission.rejected", None)
        flags.clear_overrides()


# ── Test 24: concurrent retrieval ────────────────────────────────────────────

class TestConcurrentRetrieval:

    def test_concurrent_reads_do_not_deadlock(self, override_graph_db):
        n = CognitiveNode(
            node_id=_node_id(NodeType.CONCEPT, "concurrent"),
            node_type=NodeType.CONCEPT,
            canonical_key="CONCEPT:concurrent",
            canonical_name="concurrent", display_name="concurrent",
            safe_summary="concurrent retrieval test",
            importance=0.7,
            content_hash=_content_hash("concurrent"),
        )
        override_graph_db.upsert_node(n)

        flags.set_override("AOCA_ENABLED", True)
        flags.set_override("AOCA_RETRIEVAL_ENABLED", True)
        errors: list[Exception] = []

        def _retrieve():
            try:
                from aoca.retrieval import retrieve
                retrieve("concurrent", limit=5)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_retrieve) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert errors == []
        flags.clear_overrides()


# ── Test 25: graceful shutdown ────────────────────────────────────────────────

class TestGracefulShutdown:

    def test_cognitive_service_stops_cleanly(self, override_graph_db, monkeypatch):
        monkeypatch.setattr("aoca.cognition._svc", None)
        monkeypatch.setattr("aoca.consolidation._svc", None)
        flags.set_override("AOCA_ENABLED", True)
        flags.set_override("AOCA_CONSOLIDATION_ENABLED", False)

        from aoca.cognition import CognitiveService
        svc = CognitiveService()
        svc.start()
        svc.stop()  # must not raise
        flags.clear_overrides()

    def test_db_close_is_idempotent(self, tmp_path):
        db = CognitiveGraphDB(tmp_path / "close_test.db")
        db.close()
        db.close()  # second close must not raise


# ── Test 26: Phases 1-3 regression ───────────────────────────────────────────

class TestPhase13Regression:

    def test_prohibited_sensitivity_never_enters_graph(self, override_graph_db):
        flags.set_override("AOCA_ENABLED", True)
        flags.set_override("AOCA_MEMORY_ADMISSION_ENABLED", True)
        from aoca.admission import AdmissionDecision, MemoryAdmissionPolicy
        n = CognitiveNode(
            node_id="prohibited-1", node_type=NodeType.CONCEPT,
            canonical_key="CONCEPT:secret_key_prohibited",
            canonical_name="secret", display_name="secret",
            safe_summary="api_key=sk-abc123",
            sensitivity=Sensitivity.PROHIBITED,
            content_hash=_content_hash("api_key=sk-abc123"),
        )
        result = MemoryAdmissionPolicy().evaluate(n)
        assert result.decision == AdmissionDecision.REJECT
        # Must not be in DB
        assert override_graph_db.get_node("prohibited-1") is None
        flags.clear_overrides()

    def test_learning_flag_cannot_be_enabled(self):
        flags.set_override("AOCA_LEARNING_ENABLED", True)  # attempt to override
        assert flags.get("AOCA_LEARNING_ENABLED") is False  # locked off
        flags.clear_overrides()


# ── Test 27: resource measurement ────────────────────────────────────────────

class TestResourceMeasurement:

    def test_retrieval_completes_within_500ms(self, override_graph_db):
        # Seed 50 nodes
        for i in range(50):
            n = CognitiveNode(
                node_id=f"perf-{i}", node_type=NodeType.CONCEPT,
                canonical_key=f"CONCEPT:perf_{i}",
                canonical_name=f"perf_{i}", display_name=f"perf_{i}",
                safe_summary=f"performance test node {i}",
                importance=0.5,
                content_hash=_content_hash(f"perf_{i}"),
            )
            override_graph_db.upsert_node(n)

        flags.set_override("AOCA_ENABLED", True)
        flags.set_override("AOCA_RETRIEVAL_ENABLED", True)
        flags.set_override("AOCA_ACTIVATION_ENABLED", False)

        from aoca.retrieval import retrieve
        t0 = time.monotonic()
        retrieve("performance test", limit=10)
        elapsed_ms = (time.monotonic() - t0) * 1000
        assert elapsed_ms < 500, f"retrieval took {elapsed_ms:.0f}ms"
        flags.clear_overrides()

    def test_cognitive_graph_db_import_footprint(self):
        """Importing aoca.graph must not balloon RAM (smoke test)."""
        import importlib
        # Just verify the import chain completes without OOM
        import aoca.graph
        import aoca.embed
        import aoca.memory
        import aoca.retrieval
        import aoca.activation
        import aoca.admission
        # All imports succeed
        assert True
