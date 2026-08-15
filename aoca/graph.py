"""Persistent temporal cognitive graph — Phase 4.

Two tables (nodes + edges) form G_t = (V_t, E_t). A third table holds packed
float32 embeddings separately from node metadata, so loading a node never
forces a 1.5 KB blob into RAM.

Temporal versioning: when a fact changes the old row's valid_until is stamped
and a new row is written, preserving history without altering existing PKs.

Protected nodes (Bhuvan, JARVIS, FRIDAY) and protected edges (CREATED) are
seeded at first open and cannot be archived or decayed.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import struct
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Optional

from aoca.config import flags, limits

log = logging.getLogger("aoca.graph")

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "aoca_cognition.db"
_SCHEMA_VERSION = 1

# ── enumerations ─────────────────────────────────────────────────────────────

class NodeType(str, Enum):
    PERSON="PERSON"; ASSISTANT="ASSISTANT"; PROJECT="PROJECT"; TASK="TASK"
    GOAL="GOAL"; TOPIC="TOPIC"; CONCEPT="CONCEPT"; EVENT="EVENT"
    MEMORY="MEMORY"; PREFERENCE="PREFERENCE"; PROCEDURE="PROCEDURE"
    TOOL="TOOL"; MODEL="MODEL"; APPLICATION="APPLICATION"; FILE="FILE"
    FOLDER="FOLDER"; WEBSITE="WEBSITE"; EMAIL_ACCOUNT="EMAIL_ACCOUNT"
    EMAIL_THREAD="EMAIL_THREAD"; COLLEGE_SUBJECT="COLLEGE_SUBJECT"
    DEADLINE="DEADLINE"; ERROR="ERROR"; SOLUTION="SOLUTION"
    WORKFLOW="WORKFLOW"; DEVICE="DEVICE"; LOCATION="LOCATION"
    SECURITY_POLICY="SECURITY_POLICY"

class RelationType(str, Enum):
    CREATED="CREATED"; CREATED_BY="CREATED_BY"; RELATED_TO="RELATED_TO"
    PART_OF="PART_OF"; DEPENDS_ON="DEPENDS_ON"; USES="USES"
    PREFERS="PREFERS"; BEST_HANDLED_BY="BEST_HANDLED_BY"
    WORKS_WITH="WORKS_WITH"; FAILED_WITH="FAILED_WITH"; SOLVES="SOLVES"
    CAUSED="CAUSED"; RESULTED_IN="RESULTED_IN"; ASSOCIATED_WITH="ASSOCIATED_WITH"
    BEFORE="BEFORE"; AFTER="AFTER"; DEADLINE_FOR="DEADLINE_FOR"
    ROUTES_TO="ROUTES_TO"; TRUSTED_FOR="TRUSTED_FOR"; CONFIRMS="CONFIRMS"
    CONTRADICTS="CONTRADICTS"; REQUIRES_PERMISSION="REQUIRES_PERMISSION"
    BLOCKED_BY="BLOCKED_BY"; DERIVED_FROM="DERIVED_FROM"; MENTIONS="MENTIONS"
    HAS_PREFERENCE="HAS_PREFERENCE"; HAS_PROCEDURE="HAS_PROCEDURE"
    HAS_OUTCOME="HAS_OUTCOME"

class AssistantScope(str, Enum):
    SHARED="SHARED"; JARVIS="JARVIS"; FRIDAY="FRIDAY"; NEXUS="NEXUS"

class Sensitivity(str, Enum):
    PUBLIC="PUBLIC"; PERSONAL="PERSONAL"; SENSITIVE="SENSITIVE"
    SECRET_REFERENCE="SECRET_REFERENCE"; PROHIBITED="PROHIBITED"

class MemoryClass(str, Enum):
    WORKING="WORKING"; EPISODIC="EPISODIC"; SEMANTIC="SEMANTIC"
    PROCEDURAL="PROCEDURAL"; PREFERENCE="PREFERENCE"; ARCHIVAL="ARCHIVAL"

class NodeStatus(str, Enum):
    ACTIVE="ACTIVE"; DORMANT="DORMANT"; ARCHIVED="ARCHIVED"
    ELIGIBLE_FOR_DELETION="ELIGIBLE_FOR_DELETION"

class ProcedureStage(str, Enum):
    CANDIDATE="PROCEDURAL_CANDIDATE"; OBSERVED="OBSERVED_SUCCESS"
    REPEATED="REPEATED_SUCCESS"; TRUSTED="TRUSTED_PROCEDURE"


# ── dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class CognitiveNode:
    node_id: str
    node_type: NodeType
    canonical_key: str          # e.g. "PERSON:bhuvan"
    canonical_name: str
    display_name: str
    safe_summary: str = ""
    assistant_scope: AssistantScope = AssistantScope.SHARED
    sensitivity: Sensitivity = Sensitivity.PUBLIC
    importance: float = 0.5     # [0,1]
    confidence: float = 0.5     # [0,1]
    activation_baseline: float = 0.0
    stability: float = 0.5      # decay resistance [0,1]
    protected: bool = False
    pinned: bool = False
    archived: bool = False
    memory_class: MemoryClass = MemoryClass.SEMANTIC
    status: NodeStatus = NodeStatus.ACTIVE
    procedure_stage: Optional[ProcedureStage] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_accessed_at: float = field(default_factory=time.time)
    access_count: int = 0
    source_type: str = "user"
    source_reference: str = ""
    content_hash: str = ""
    metadata_json: str = "{}"
    version: int = 1
    valid_from: float = field(default_factory=time.time)
    valid_until: Optional[float] = None   # None = still active
    previous_version_id: Optional[str] = None
    procedure_successes: int = 0
    procedure_failures: int = 0


@dataclass
class CognitiveEdge:
    edge_id: str
    source_node_id: str
    target_node_id: str
    relation_type: RelationType
    assistant_scope: AssistantScope = AssistantScope.SHARED
    weight: float = 0.5         # [-1.0, 1.0]
    confidence: float = 0.5
    uncertainty: float = 0.5
    positive_evidence: int = 0
    negative_evidence: int = 0
    protected: bool = False
    archived: bool = False
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_accessed_at: float = field(default_factory=time.time)
    last_reinforced_at: float = field(default_factory=time.time)
    valid_from: float = field(default_factory=time.time)
    valid_until: Optional[float] = None
    provenance: str = ""
    metadata_json: str = "{}"
    version: int = 1


# ── helpers ───────────────────────────────────────────────────────────────────

def _node_id(node_type: NodeType, canonical_key: str,
             scope: AssistantScope = AssistantScope.SHARED) -> str:
    """Stable, deterministic node id from type + key + scope."""
    raw = f"{node_type.value}:{canonical_key}:{scope.value}"
    return "n-" + hashlib.sha1(raw.encode()).hexdigest()[:16]


def _edge_id(src: str, rel: RelationType, tgt: str) -> str:
    raw = f"{src}:{rel.value}:{tgt}"
    return "e-" + hashlib.sha1(raw.encode()).hexdigest()[:16]


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _pack_vec(v: list[float]) -> bytes:
    return struct.pack(f"{len(v)}f", *v)


def _unpack_vec(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# ── migrations ────────────────────────────────────────────────────────────────

_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """
        CREATE TABLE IF NOT EXISTS cognitive_nodes (
            node_id             TEXT    PRIMARY KEY,
            node_type           TEXT    NOT NULL,
            canonical_key       TEXT    NOT NULL,
            canonical_name      TEXT    NOT NULL,
            display_name        TEXT    NOT NULL,
            safe_summary        TEXT    NOT NULL DEFAULT '',
            assistant_scope     TEXT    NOT NULL DEFAULT 'SHARED',
            sensitivity         TEXT    NOT NULL DEFAULT 'PUBLIC',
            importance          REAL    NOT NULL DEFAULT 0.5,
            confidence          REAL    NOT NULL DEFAULT 0.5,
            activation_baseline REAL    NOT NULL DEFAULT 0.0,
            stability           REAL    NOT NULL DEFAULT 0.5,
            protected           INTEGER NOT NULL DEFAULT 0,
            pinned              INTEGER NOT NULL DEFAULT 0,
            archived            INTEGER NOT NULL DEFAULT 0,
            memory_class        TEXT    NOT NULL DEFAULT 'SEMANTIC',
            status              TEXT    NOT NULL DEFAULT 'ACTIVE',
            procedure_stage     TEXT,
            created_at          REAL    NOT NULL,
            updated_at          REAL    NOT NULL,
            last_accessed_at    REAL    NOT NULL,
            access_count        INTEGER NOT NULL DEFAULT 0,
            source_type         TEXT    NOT NULL DEFAULT 'user',
            source_reference    TEXT    NOT NULL DEFAULT '',
            content_hash        TEXT    NOT NULL DEFAULT '',
            metadata_json       TEXT    NOT NULL DEFAULT '{}',
            version             INTEGER NOT NULL DEFAULT 1,
            valid_from          REAL    NOT NULL,
            valid_until         REAL,
            previous_version_id TEXT,
            procedure_successes INTEGER NOT NULL DEFAULT 0,
            procedure_failures  INTEGER NOT NULL DEFAULT 0
        )
        """,
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_node_canonical "
        "ON cognitive_nodes(canonical_key, assistant_scope) "
        "WHERE valid_until IS NULL",
        "CREATE INDEX IF NOT EXISTS idx_node_type "
        "ON cognitive_nodes(node_type, status)",
        "CREATE INDEX IF NOT EXISTS idx_node_scope "
        "ON cognitive_nodes(assistant_scope, memory_class)",
        "CREATE INDEX IF NOT EXISTS idx_node_importance "
        "ON cognitive_nodes(importance DESC) WHERE archived=0",
        """
        CREATE TABLE IF NOT EXISTS cognitive_edges (
            edge_id             TEXT    PRIMARY KEY,
            source_node_id      TEXT    NOT NULL REFERENCES cognitive_nodes(node_id),
            target_node_id      TEXT    NOT NULL REFERENCES cognitive_nodes(node_id),
            relation_type       TEXT    NOT NULL,
            assistant_scope     TEXT    NOT NULL DEFAULT 'SHARED',
            weight              REAL    NOT NULL DEFAULT 0.5,
            confidence          REAL    NOT NULL DEFAULT 0.5,
            uncertainty         REAL    NOT NULL DEFAULT 0.5,
            positive_evidence   INTEGER NOT NULL DEFAULT 0,
            negative_evidence   INTEGER NOT NULL DEFAULT 0,
            protected           INTEGER NOT NULL DEFAULT 0,
            archived            INTEGER NOT NULL DEFAULT 0,
            created_at          REAL    NOT NULL,
            updated_at          REAL    NOT NULL,
            last_accessed_at    REAL    NOT NULL,
            last_reinforced_at  REAL    NOT NULL,
            valid_from          REAL    NOT NULL,
            valid_until         REAL,
            provenance          TEXT    NOT NULL DEFAULT '',
            metadata_json       TEXT    NOT NULL DEFAULT '',
            version             INTEGER NOT NULL DEFAULT 1
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_edge_source "
        "ON cognitive_edges(source_node_id) WHERE archived=0",
        "CREATE INDEX IF NOT EXISTS idx_edge_target "
        "ON cognitive_edges(target_node_id) WHERE archived=0",
        "CREATE INDEX IF NOT EXISTS idx_edge_type "
        "ON cognitive_edges(relation_type, source_node_id)",
        """
        CREATE TABLE IF NOT EXISTS node_embeddings (
            embedding_id    TEXT    PRIMARY KEY,
            node_id         TEXT    NOT NULL REFERENCES cognitive_nodes(node_id),
            embedding_model TEXT    NOT NULL,
            dimension       INTEGER NOT NULL,
            content_hash    TEXT    NOT NULL,
            created_at      REAL    NOT NULL,
            model_version   TEXT    NOT NULL DEFAULT '1',
            vector          BLOB    NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_emb_node "
        "ON node_embeddings(node_id)",
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS node_fts
        USING fts5(
            node_id UNINDEXED,
            canonical_name,
            display_name,
            safe_summary,
            content='cognitive_nodes',
            content_rowid='rowid'
        )
        """,
        """
        CREATE TRIGGER IF NOT EXISTS node_fts_insert
        AFTER INSERT ON cognitive_nodes BEGIN
            INSERT INTO node_fts(rowid, node_id, canonical_name, display_name, safe_summary)
            VALUES (new.rowid, new.node_id, new.canonical_name,
                    new.display_name, new.safe_summary);
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS node_fts_update
        AFTER UPDATE ON cognitive_nodes BEGIN
            INSERT INTO node_fts(node_fts, rowid, node_id, canonical_name,
                                 display_name, safe_summary)
            VALUES ('delete', old.rowid, old.node_id, old.canonical_name,
                    old.display_name, old.safe_summary);
            INSERT INTO node_fts(rowid, node_id, canonical_name, display_name, safe_summary)
            VALUES (new.rowid, new.node_id, new.canonical_name,
                    new.display_name, new.safe_summary);
        END
        """,
        """
        CREATE TABLE IF NOT EXISTS graph_snapshots (
            snapshot_id   TEXT    PRIMARY KEY,
            created_at    REAL    NOT NULL,
            node_count    INTEGER NOT NULL,
            edge_count    INTEGER NOT NULL,
            trigger       TEXT    NOT NULL DEFAULT 'manual',
            db_path       TEXT    NOT NULL DEFAULT ''
        )
        """,
    ),
}


# ── database class ────────────────────────────────────────────────────────────

class CognitiveGraphDB:
    """SQLite-backed temporal cognitive graph.

    One instance per process; callers share it through cognition.py.
    Thread-safe via RLock around every connection use.
    """

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self._path = db_path
        self._lock = threading.RLock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection = self._open()
        self._migrate()
        self.seed_protected_nodes()

    # ── connection ────────────────────────────────────────────────────────────

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # ── migrations ────────────────────────────────────────────────────────────

    def _migrate(self) -> None:
        with self._lock:
            cur_ver = self._conn.execute("PRAGMA user_version").fetchone()[0]
            for ver in sorted(v for v in _MIGRATIONS if v > cur_ver):
                try:
                    for stmt in _MIGRATIONS[ver]:
                        self._conn.execute(stmt)
                    self._conn.execute(f"PRAGMA user_version={ver}")
                    self._conn.commit()
                    log.info("graph migration -> v%d", ver)
                except Exception:
                    self._conn.rollback()
                    raise

    # ── node CRUD ─────────────────────────────────────────────────────────────

    def upsert_node(self, node: CognitiveNode) -> CognitiveNode:
        """Insert or temporally version a node.

        If a live row with the same (canonical_key, assistant_scope) exists and
        its content_hash differs, stamp valid_until on the old row and insert
        a new version. Protected nodes cannot be archived or decayed.
        """
        now = time.time()
        with self._tx() as c:
            existing = c.execute(
                "SELECT node_id, content_hash, protected, version "
                "FROM cognitive_nodes "
                "WHERE canonical_key=? AND assistant_scope=? AND valid_until IS NULL",
                (node.canonical_key, node.assistant_scope.value),
            ).fetchone()

            if existing:
                if existing["content_hash"] == node.content_hash:
                    # touch access time only
                    c.execute(
                        "UPDATE cognitive_nodes SET last_accessed_at=?, access_count=access_count+1 "
                        "WHERE node_id=?",
                        (now, existing["node_id"]),
                    )
                    node.node_id = existing["node_id"]
                    return node

                # ponytail: protected guard — cannot be superseded
                if existing["protected"]:
                    log.warning("attempted to supersede protected node %s — skipped",
                                existing["node_id"])
                    node.node_id = existing["node_id"]
                    return node

                # stamp old version
                c.execute(
                    "UPDATE cognitive_nodes SET valid_until=? WHERE node_id=?",
                    (now, existing["node_id"]),
                )
                node.previous_version_id = existing["node_id"]
                node.version = existing["version"] + 1
                node.node_id = _node_id(node.node_type, node.canonical_key + f"@v{node.version}",
                                        node.assistant_scope)

            node.updated_at = now
            c.execute(
                """INSERT INTO cognitive_nodes VALUES (
                    :node_id,:node_type,:canonical_key,:canonical_name,:display_name,
                    :safe_summary,:assistant_scope,:sensitivity,:importance,:confidence,
                    :activation_baseline,:stability,:protected,:pinned,:archived,
                    :memory_class,:status,:procedure_stage,:created_at,:updated_at,
                    :last_accessed_at,:access_count,:source_type,:source_reference,
                    :content_hash,:metadata_json,:version,:valid_from,:valid_until,
                    :previous_version_id,:procedure_successes,:procedure_failures
                )""",
                {
                    "node_id": node.node_id,
                    "node_type": node.node_type.value,
                    "canonical_key": node.canonical_key,
                    "canonical_name": node.canonical_name,
                    "display_name": node.display_name,
                    "safe_summary": node.safe_summary,
                    "assistant_scope": node.assistant_scope.value,
                    "sensitivity": node.sensitivity.value,
                    "importance": _clamp(node.importance),
                    "confidence": _clamp(node.confidence),
                    "activation_baseline": node.activation_baseline,
                    "stability": _clamp(node.stability),
                    "protected": int(node.protected),
                    "pinned": int(node.pinned),
                    "archived": int(node.archived),
                    "memory_class": node.memory_class.value,
                    "status": node.status.value,
                    "procedure_stage": node.procedure_stage.value if node.procedure_stage else None,
                    "created_at": node.created_at,
                    "updated_at": node.updated_at,
                    "last_accessed_at": node.last_accessed_at,
                    "access_count": node.access_count,
                    "source_type": node.source_type,
                    "source_reference": node.source_reference,
                    "content_hash": node.content_hash,
                    "metadata_json": node.metadata_json,
                    "version": node.version,
                    "valid_from": node.valid_from,
                    "valid_until": node.valid_until,
                    "previous_version_id": node.previous_version_id,
                    "procedure_successes": node.procedure_successes,
                    "procedure_failures": node.procedure_failures,
                },
            )
        return node

    def get_node(self, node_id: str) -> Optional[CognitiveNode]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM cognitive_nodes WHERE node_id=?", (node_id,)
            ).fetchone()
        return self._row_to_node(row) if row else None

    def get_node_by_key(self, canonical_key: str,
                        scope: AssistantScope = AssistantScope.SHARED
                        ) -> Optional[CognitiveNode]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM cognitive_nodes "
                "WHERE canonical_key=? AND assistant_scope=? AND valid_until IS NULL",
                (canonical_key, scope.value),
            ).fetchone()
        return self._row_to_node(row) if row else None

    @staticmethod
    def _row_to_node(row: sqlite3.Row) -> CognitiveNode:
        d = dict(row)
        d["node_type"] = NodeType(d["node_type"])
        d["assistant_scope"] = AssistantScope(d["assistant_scope"])
        d["sensitivity"] = Sensitivity(d["sensitivity"])
        d["memory_class"] = MemoryClass(d["memory_class"])
        d["status"] = NodeStatus(d["status"])
        d["procedure_stage"] = ProcedureStage(d["procedure_stage"]) if d["procedure_stage"] else None
        d["protected"] = bool(d["protected"])
        d["pinned"] = bool(d["pinned"])
        d["archived"] = bool(d["archived"])
        d["relation_type"] = None  # not in this table
        d.pop("relation_type", None)
        return CognitiveNode(**{k: v for k, v in d.items() if k in CognitiveNode.__dataclass_fields__})

    # ── edge CRUD ─────────────────────────────────────────────────────────────

    def upsert_edge(self, edge: CognitiveEdge) -> CognitiveEdge:
        edge.weight = _clamp(edge.weight, -1.0, 1.0)
        now = time.time()
        with self._tx() as c:
            existing = c.execute(
                "SELECT edge_id, protected FROM cognitive_edges WHERE edge_id=?",
                (edge.edge_id,),
            ).fetchone()
            if existing and existing["protected"]:
                log.warning("attempted to update protected edge %s — skipped", edge.edge_id)
                return edge
            edge.updated_at = now
            c.execute(
                """INSERT OR REPLACE INTO cognitive_edges VALUES (
                    :edge_id,:source_node_id,:target_node_id,:relation_type,
                    :assistant_scope,:weight,:confidence,:uncertainty,
                    :positive_evidence,:negative_evidence,:protected,:archived,
                    :created_at,:updated_at,:last_accessed_at,:last_reinforced_at,
                    :valid_from,:valid_until,:provenance,:metadata_json,:version
                )""",
                {
                    "edge_id": edge.edge_id,
                    "source_node_id": edge.source_node_id,
                    "target_node_id": edge.target_node_id,
                    "relation_type": edge.relation_type.value,
                    "assistant_scope": edge.assistant_scope.value,
                    "weight": edge.weight,
                    "confidence": _clamp(edge.confidence),
                    "uncertainty": _clamp(edge.uncertainty),
                    "positive_evidence": edge.positive_evidence,
                    "negative_evidence": edge.negative_evidence,
                    "protected": int(edge.protected),
                    "archived": int(edge.archived),
                    "created_at": edge.created_at,
                    "updated_at": edge.updated_at,
                    "last_accessed_at": edge.last_accessed_at,
                    "last_reinforced_at": edge.last_reinforced_at,
                    "valid_from": edge.valid_from,
                    "valid_until": edge.valid_until,
                    "provenance": edge.provenance,
                    "metadata_json": edge.metadata_json,
                    "version": edge.version,
                },
            )
        return edge

    def get_neighbors(
        self,
        node_id: str,
        scope: Optional[AssistantScope] = None,
        max_hops: int = 1,
        limit: int = 128,
    ) -> list[tuple[CognitiveNode, CognitiveEdge]]:
        """Return (node, edge) pairs reachable in up to max_hops steps."""
        visited: set[str] = {node_id}
        frontier = [node_id]
        results: list[tuple[CognitiveNode, CognitiveEdge]] = []

        for _ in range(max_hops):
            if not frontier or len(results) >= limit:
                break
            placeholders = ",".join("?" * len(frontier))
            scope_clause = "AND e.assistant_scope=?" if scope else ""
            params: list[Any] = list(frontier)
            if scope:
                params.append(scope.value)

            with self._lock:
                rows = self._conn.execute(
                    f"""
                    SELECT n.*, e.edge_id, e.source_node_id, e.target_node_id,
                           e.relation_type, e.assistant_scope AS e_scope,
                           e.weight, e.confidence, e.uncertainty,
                           e.positive_evidence, e.negative_evidence,
                           e.protected AS e_protected, e.archived AS e_archived,
                           e.created_at AS e_created, e.updated_at AS e_updated,
                           e.last_accessed_at AS e_last_acc,
                           e.last_reinforced_at, e.valid_from AS e_vf,
                           e.valid_until AS e_vu, e.provenance, e.metadata_json AS e_meta,
                           e.version AS e_ver
                    FROM cognitive_edges e
                    JOIN cognitive_nodes n ON (
                        (e.target_node_id = n.node_id AND e.source_node_id IN ({placeholders}))
                        OR
                        (e.source_node_id = n.node_id AND e.target_node_id IN ({placeholders}))
                    )
                    WHERE e.archived=0 AND n.archived=0 AND n.valid_until IS NULL
                    {scope_clause}
                    LIMIT ?
                    """,
                    params + params + ([scope.value] if scope else []) + [limit - len(results)],
                ).fetchall()

            next_frontier: list[str] = []
            for row in rows:
                nid = row["node_id"]
                if nid in visited:
                    continue
                visited.add(nid)
                next_frontier.append(nid)
                node = self._row_to_node(row)
                edge = CognitiveEdge(
                    edge_id=row["edge_id"],
                    source_node_id=row["source_node_id"],
                    target_node_id=row["target_node_id"],
                    relation_type=RelationType(row["relation_type"]),
                    assistant_scope=AssistantScope(row["e_scope"]),
                    weight=row["weight"],
                    confidence=row["confidence"],
                    uncertainty=row["uncertainty"],
                    positive_evidence=row["positive_evidence"],
                    negative_evidence=row["negative_evidence"],
                    protected=bool(row["e_protected"]),
                    archived=bool(row["e_archived"]),
                    created_at=row["e_created"],
                    updated_at=row["e_updated"],
                    last_accessed_at=row["e_last_acc"],
                    last_reinforced_at=row["last_reinforced_at"],
                    valid_from=row["e_vf"],
                    valid_until=row["e_vu"],
                    provenance=row["provenance"],
                    metadata_json=row["e_meta"],
                    version=row["e_ver"],
                )
                results.append((node, edge))

            frontier = next_frontier

        return results[:limit]

    # ── embeddings ────────────────────────────────────────────────────────────

    def store_embedding(self, node_id: str, vector: list[float],
                        model: str, content_hash: str) -> None:
        if len(vector) == 0:
            return
        emb_id = f"emb-{node_id}"
        blob = _pack_vec(vector)
        now = time.time()
        with self._tx() as c:
            c.execute(
                """INSERT OR REPLACE INTO node_embeddings
                   (embedding_id, node_id, embedding_model, dimension,
                    content_hash, created_at, model_version, vector)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (emb_id, node_id, model, len(vector), content_hash, now, "1", blob),
            )

    def get_embedding(self, node_id: str) -> Optional[list[float]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT vector FROM node_embeddings WHERE node_id=?", (node_id,)
            ).fetchone()
        return _unpack_vec(row["vector"]) if row else None

    # ── FTS search ────────────────────────────────────────────────────────────

    def fts_search(self, query: str, scope: Optional[AssistantScope] = None,
                   limit: int = 64) -> list[CognitiveNode]:
        if not query.strip():
            return []
        scope_clause = "AND n.assistant_scope=?" if scope else ""
        params: list[Any] = [query, limit]
        if scope:
            params.insert(1, scope.value)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT n.* FROM node_fts f
                JOIN cognitive_nodes n ON n.node_id = f.node_id
                WHERE node_fts MATCH ? {scope_clause}
                  AND n.archived=0 AND n.valid_until IS NULL
                ORDER BY rank
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._row_to_node(r) for r in rows]

    # ── protected-node seeding ────────────────────────────────────────────────

    def seed_protected_nodes(self) -> None:
        now = time.time()

        bhuvan_id = _node_id(NodeType.PERSON, "bhuvan")
        jarvis_id = _node_id(NodeType.ASSISTANT, "jarvis")
        friday_id = _node_id(NodeType.ASSISTANT, "friday")

        for node in [
            CognitiveNode(
                node_id=bhuvan_id, node_type=NodeType.PERSON,
                canonical_key="PERSON:bhuvan", canonical_name="Bhuvan",
                display_name="Bhuvan", safe_summary="Creator of JARVIS and FRIDAY",
                sensitivity=Sensitivity.PERSONAL, importance=1.0, confidence=1.0,
                stability=1.0, protected=True, pinned=True,
                memory_class=MemoryClass.SEMANTIC,
                source_type="system", content_hash=_content_hash("bhuvan"),
                created_at=now, updated_at=now, last_accessed_at=now,
                valid_from=now,
            ),
            CognitiveNode(
                node_id=jarvis_id, node_type=NodeType.ASSISTANT,
                canonical_key="ASSISTANT:jarvis", canonical_name="JARVIS",
                display_name="JARVIS", safe_summary="AI assistant created by Bhuvan",
                sensitivity=Sensitivity.PUBLIC, importance=1.0, confidence=1.0,
                stability=1.0, protected=True, pinned=True,
                assistant_scope=AssistantScope.JARVIS,
                memory_class=MemoryClass.SEMANTIC,
                source_type="system", content_hash=_content_hash("jarvis"),
                created_at=now, updated_at=now, last_accessed_at=now,
                valid_from=now,
            ),
            CognitiveNode(
                node_id=friday_id, node_type=NodeType.ASSISTANT,
                canonical_key="ASSISTANT:friday", canonical_name="FRIDAY",
                display_name="FRIDAY", safe_summary="AI assistant created by Bhuvan",
                sensitivity=Sensitivity.PUBLIC, importance=1.0, confidence=1.0,
                stability=1.0, protected=True, pinned=True,
                assistant_scope=AssistantScope.FRIDAY,
                memory_class=MemoryClass.SEMANTIC,
                source_type="system", content_hash=_content_hash("friday"),
                created_at=now, updated_at=now, last_accessed_at=now,
                valid_from=now,
            ),
        ]:
            # Only insert if not already present — idempotent seed
            with self._lock:
                exists = self._conn.execute(
                    "SELECT 1 FROM cognitive_nodes WHERE node_id=?", (node.node_id,)
                ).fetchone()
            if not exists:
                self.upsert_node(node)

        for src_id, tgt_id, tgt_name in [
            (bhuvan_id, jarvis_id, "jarvis"),
            (bhuvan_id, friday_id, "friday"),
        ]:
            eid = _edge_id(src_id, RelationType.CREATED, tgt_id)
            with self._lock:
                exists = self._conn.execute(
                    "SELECT 1 FROM cognitive_edges WHERE edge_id=?", (eid,)
                ).fetchone()
            if not exists:
                self.upsert_edge(CognitiveEdge(
                    edge_id=eid, source_node_id=src_id, target_node_id=tgt_id,
                    relation_type=RelationType.CREATED,
                    weight=1.0, confidence=1.0, uncertainty=0.0,
                    protected=True, positive_evidence=1,
                    provenance="system:seed",
                    created_at=now, updated_at=now,
                    last_accessed_at=now, last_reinforced_at=now,
                    valid_from=now,
                ))

    # ── maintenance ───────────────────────────────────────────────────────────

    def backup(self, dest: Optional[Path] = None) -> Path:
        if dest is None:
            ts = int(time.time())
            dest = self._path.parent / f"aoca_cognition_backup_{ts}.db"
        with self._lock:
            dest_conn = sqlite3.connect(str(dest))
            self._conn.backup(dest_conn)
            dest_conn.close()
        with self._lock:
            nc = self._conn.execute("SELECT COUNT(*) FROM cognitive_nodes").fetchone()[0]
            ec = self._conn.execute("SELECT COUNT(*) FROM cognitive_edges").fetchone()[0]
            self._conn.execute(
                "INSERT OR REPLACE INTO graph_snapshots VALUES (?,?,?,?,?,?)",
                (f"snap-{int(time.time())}", time.time(), nc, ec, "backup", str(dest)),
            )
            self._conn.commit()
        log.info("graph backup -> %s (%d nodes, %d edges)", dest, nc, ec)
        return dest

    def integrity_check(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute("PRAGMA integrity_check").fetchall()
        results = [r[0] for r in rows]
        if results != ["ok"]:
            log.warning("graph integrity issues: %s", results)
        return results

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass


# ── module-level singleton ────────────────────────────────────────────────────
# ponytail: lazy init — don't open the DB on import, only when first accessed
_db: Optional[CognitiveGraphDB] = None
_db_lock = threading.Lock()


def get_db(db_path: Path = DB_PATH) -> CognitiveGraphDB:
    global _db
    if _db is None:
        with _db_lock:
            if _db is None:
                _db = CognitiveGraphDB(db_path)
    return _db
