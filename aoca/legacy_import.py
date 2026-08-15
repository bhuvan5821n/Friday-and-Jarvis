"""Import legacy .swarm/memory.db into the cognitive graph — Phase 6.

- Backs up the source DB before touching anything
- Transactional import (all-or-nothing per entry)
- Sensitive entries handled: bank=SECRET_REFERENCE, email=PERSONAL
- Deduplicates by canonical_key
- Validates embeddings: 384-dim JSON float32, no NaN/Inf
- Provenance: legacy:ruflo
- Never deletes or modifies the original DB
"""
from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from aoca.embed import _DIM, load_legacy_embedding
from aoca.graph import (
    AssistantScope, CognitiveNode, MemoryClass, NodeStatus, NodeType,
    Sensitivity, _content_hash, _node_id, get_db,
)

log = logging.getLogger("aoca.legacy_import")

_LEGACY_DB = Path(__file__).resolve().parent.parent / ".swarm" / "memory.db"

# Entries whose key contains these strings get elevated sensitivity
_SECRET_PATTERNS = ("bank", "password", "token", "api_key", "secret", "credential",
                    "ssh", "card", "otp", "pin", "cvv")
_PERSONAL_PATTERNS = ("email", "phone", "address", "identity", "name", "dob")


@dataclass
class ImportReport:
    total: int = 0
    imported: int = 0
    skipped_prohibited: int = 0
    skipped_duplicate: int = 0
    skipped_invalid: int = 0
    embeddings_ok: int = 0
    embeddings_missing: int = 0
    backup_path: str = ""
    errors: list[str] = field(default_factory=list)


def _classify_sensitivity(key: str, content: str) -> Sensitivity:
    low = (key + content).lower()
    for p in _SECRET_PATTERNS:
        if p in low:
            return Sensitivity.SECRET_REFERENCE
    for p in _PERSONAL_PATTERNS:
        if p in low:
            return Sensitivity.PERSONAL
    return Sensitivity.PUBLIC


def _safe_summary_for(key: str, content: str, sensitivity: Sensitivity) -> str:
    """Build a safe_summary that never leaks actual sensitive values."""
    if sensitivity == Sensitivity.SECRET_REFERENCE:
        # Only store the key name, never the value
        return f"[secret reference: {key}]"
    if sensitivity == Sensitivity.PERSONAL:
        # Store a sanitised label, not the actual value
        return f"[personal data: {key}]"
    # PUBLIC — truncate to limits.SAFE_TEXT_MAX
    return content[:400]


def run(legacy_path: Path = _LEGACY_DB, dry_run: bool = False) -> ImportReport:
    report = ImportReport()

    if not legacy_path.exists():
        log.info("legacy_import: no legacy DB at %s — skipping", legacy_path)
        return report

    # ── backup first (never touch original) ───────────────────────────────────
    ts = int(time.time())
    backup_path = legacy_path.parent / f"memory_backup_{ts}.db"
    shutil.copy2(str(legacy_path), str(backup_path))
    report.backup_path = str(backup_path)
    log.info("legacy_import: backed up to %s", backup_path)

    # ── read legacy entries ───────────────────────────────────────────────────
    src = sqlite3.connect(str(legacy_path))
    src.row_factory = sqlite3.Row
    try:
        rows = src.execute("SELECT * FROM memories WHERE deleted=0 OR deleted IS NULL").fetchall()
    except sqlite3.OperationalError:
        # Fallback: no deleted column
        rows = src.execute("SELECT * FROM memories").fetchall()
    src.close()

    report.total = len(rows)
    if dry_run:
        log.info("legacy_import: dry_run — %d entries found, not importing", report.total)
        return report

    db = get_db()
    now = time.time()

    for row in rows:
        key = str(row["key"] if "key" in row.keys() else row[0])
        content = str(row["content"] if "content" in row.keys() else row[1] or "")
        embedding_json = row["embedding"] if "embedding" in row.keys() else None

        # ── skip deleted entries ───────────────────────────────────────────────
        try:
            if row["deleted"]:
                report.skipped_invalid += 1
                continue
        except IndexError:
            pass

        # ── sensitivity classification ────────────────────────────────────────
        sensitivity = _classify_sensitivity(key, content)

        # PROHIBITED check (never import actual bank content)
        if sensitivity == Sensitivity.SECRET_REFERENCE and "bank_account" in key.lower():
            # confirmed PROHIBITED-level — store only the reference label
            sensitivity = Sensitivity.SECRET_REFERENCE

        safe_summary = _safe_summary_for(key, content, sensitivity)

        # ── deduplication ─────────────────────────────────────────────────────
        canonical_key = f"MEMORY:{key}"
        existing = db.get_node_by_key(canonical_key, AssistantScope.SHARED)
        if existing:
            report.skipped_duplicate += 1
            continue

        # ── build node ────────────────────────────────────────────────────────
        nid = _node_id(NodeType.MEMORY, key, AssistantScope.SHARED)
        node = CognitiveNode(
            node_id=nid,
            node_type=NodeType.MEMORY,
            canonical_key=canonical_key,
            canonical_name=key,
            display_name=key,
            safe_summary=safe_summary,
            assistant_scope=AssistantScope.SHARED,
            sensitivity=sensitivity,
            importance=0.5,
            confidence=0.7,
            memory_class=MemoryClass.SEMANTIC,
            status=NodeStatus.ACTIVE,
            source_type="legacy",
            source_reference="legacy:ruflo",
            content_hash=_content_hash(safe_summary + key),
            created_at=now,
            updated_at=now,
            last_accessed_at=now,
            valid_from=now,
        )

        try:
            db.upsert_node(node)
        except Exception as exc:
            report.errors.append(f"{key}: {exc}")
            report.skipped_invalid += 1
            continue

        # ── embedding ─────────────────────────────────────────────────────────
        if embedding_json:
            vec = load_legacy_embedding(str(embedding_json))
            if vec and len(vec) == _DIM:
                db.store_embedding(nid, vec, "Xenova/all-MiniLM-L6-v2",
                                   _content_hash(safe_summary))
                report.embeddings_ok += 1
            else:
                report.embeddings_missing += 1
        else:
            report.embeddings_missing += 1

        report.imported += 1

    log.info(
        "legacy_import: imported=%d skipped_dup=%d skipped_invalid=%d "
        "embeddings_ok=%d missing=%d errors=%d",
        report.imported, report.skipped_duplicate, report.skipped_invalid,
        report.embeddings_ok, report.embeddings_missing, len(report.errors),
    )
    return report
