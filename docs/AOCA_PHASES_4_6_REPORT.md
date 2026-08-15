# AOCA Phases 4-6 Deliverables Report

Branch: `aoca-cognitive-memory-phases-4-6`  
Base commit: `6819ba4`  
Date: 2026-08-07

---

## What was built

Phases 4-6 of ASTRAEUS OMEGA COGNITIVE ARCHITECTURE: a real persistent cognitive
memory substrate layered onto the live JARVIS/FRIDAY codebase.

All security constraints from the Phase 4-6 specification are enforced
verbatim. `AOCA_LEARNING_ENABLED` is locked off. No world model, planner,
contextual bandit, continual learning, or Neural Core animation was implemented.

---

## 36 Deliverables

### Phase 4 — Temporal Cognitive Graph

**1. `aoca/graph.py` — CognitiveGraphDB.**  
SQLite WAL database at `data/aoca_cognition.db`. Schema v1 with schema-version
migration table (`PRAGMA user_version`). Tables: `cognitive_nodes`,
`cognitive_edges`, `node_embeddings`, `node_fts` (FTS5), `graph_snapshots`.

**2. Enumerations.**  
`NodeType` (27 types), `RelationType` (28 types), `AssistantScope`
(SHARED/JARVIS/FRIDAY/NEXUS), `Sensitivity`
(PUBLIC/PERSONAL/SENSITIVE/SECRET_REFERENCE/PROHIBITED), `MemoryClass`
(WORKING/EPISODIC/SEMANTIC/PROCEDURAL/PREFERENCE/ARCHIVAL), `NodeStatus`,
`ProcedureStage`.

**3. Dataclasses.**  
`CognitiveNode` (31 fields) and `CognitiveEdge` (21 fields) — all required
fields per specification including temporal versioning columns
(`valid_from`/`valid_until`/`previous_version_id`).

**4. Temporal versioning.**  
`upsert_node()` stamps `valid_until` on the old version and inserts a new row
with incremented version. History is preserved without modifying PKs. Protected
nodes (Bhuvan, JARVIS, FRIDAY) cannot be superseded — the upsert silently
returns the original.

**5. Protected node seeding (`seed_protected_nodes()`).**  
Idempotent seed at DB open: Bhuvan (PERSON, protected/pinned, importance=1.0),
JARVIS (ASSISTANT:JARVIS scope), FRIDAY (ASSISTANT:FRIDAY scope). Two protected
`CREATED` edges: Bhuvan→JARVIS, Bhuvan→FRIDAY. Protected edges cannot be
overwritten.

**6. WAL + PRAGMA setup.**  
`journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout=5000`,
`synchronous=NORMAL`. RLock on every connection access for thread safety.

**7. FTS5 virtual table + triggers.**  
`node_fts` with `content='cognitive_nodes'`. INSERT and UPDATE triggers keep
the FTS index in sync automatically.

**8. Embeddings table.**  
`node_embeddings` stores packed float32 blobs (`struct.pack`) separately from
node metadata — loading a node never forces an embedding blob into RAM.

**9. `get_neighbors()` with multi-hop support.**  
BFS up to `max_hops`, visited-set cycle detection, bounded by `limit=128`.

**10. `backup()` and `integrity_check()`.**  
`backup()` uses `sqlite3.Connection.backup()` (online backup API) and records a
snapshot row. `integrity_check()` wraps `PRAGMA integrity_check`.

**11. Deterministic helper functions.**  
`_node_id()` — SHA-1 of `type:key:scope`, prefixed `n-`.  
`_edge_id()` — SHA-1 of `src:rel:tgt`, prefixed `e-`.  
`_content_hash()` — SHA-256 truncated to 16 hex chars.  
`get_db()` — lazy module-level singleton; does not open the DB on import.

---

### Phase 4 — Embedding Service

**12. `aoca/embed.py` — MiniLM ONNX embedding service.**  
Uses `node_modules/@xenova/transformers/.cache/Xenova/all-MiniLM-L6-v2/onnx/
model_quantized.onnx` via `onnxruntime`. Lazy-loaded on first call; if the model
is unavailable, `encode()` returns `None` and semantic retrieval degrades
gracefully (FTS + graph proximity still work). Thread-safe (one Lock around
session).  
Mean-pooling over token embeddings with attention-mask weighting. Unit-norm
output.

**13. `cosine_similarity()` with safety guards.**  
Returns 0.0 on `None`, zero vector, or dimension mismatch. NaN guard via
identity comparison.

**14. `load_legacy_embedding()`.**  
Parses JSON float32 arrays from `.swarm/memory.db`. Validates: list, 384-dim,
all finite. Returns `None` on any malformed input.

---

### Phase 5 — Hybrid Retrieval

**15. `aoca/retrieval.py` — two-stage retrieval.**  
Stage A: FTS5 full-text search (limit 64) + graph neighbourhood of anchor nodes
(max 2 hops) + recency fallback (most-recently-accessed). Prohibited and
SECRET_REFERENCE nodes are filtered before Stage B.  
Stage B: Mathematical ranking R_i with all 12 components:

| Component | Symbol | Weight |
|-----------|--------|--------|
| Semantic similarity | S | 0.30 |
| FTS rank | L | 0.15 |
| Graph proximity | G | 0.10 |
| Memory class bonus | M | 0.10 |
| Confidence | C | 0.10 |
| Recency (exp decay, 24h half-life) | Rcy | 0.05 |
| Access frequency (log-scaled) | F | 0.05 |
| Importance | P | 0.05 |
| Activation baseline | A | 0.05 |
| Uncertainty penalty | U | 0.05 |
| Cross-scope mismatch penalty | X | 0.05 |
| Archival penalty | Z | 0.05 |

**16. `CognitiveRetrievalResult`.**  
Dataclass with `node`, `score`, `semantic_sim`, `fts_rank`, `graph_proximity`,
and human-readable `explanation` string showing all 12 component values.

---

### Phase 5 — Spreading Activation

**17. `aoca/activation.py` — bounded spreading activation.**  
Seed activations: `a_i^(0) = clip(q_i × importance_i × confidence_i × scope_factor, 0, 1)`.  
Propagation: `sigmoid(parent_activation × edge.weight) × depth_decay^depth × importance × confidence`.  
Bounds: `max_depth=3`, `max_nodes=128`, `max_edges=512`, `epsilon=0.001`,
`depth_decay=0.6`, `sigmoid_gain=4.0`.  
Visited-set cycle detection. Energy stability check: any activation > 1.0+ε
triggers `cognitive.activation.unstable` event and clamping.  
Returns `ActivationResult` with activations dict, counts, and `stable` flag.

---

### Phase 6 — Memory Hierarchy

**18. `aoca/memory.py` — WorkingMemory + MemoryStore.**  
`WorkingMemory`: bounded in-process ring (64 slots, 5-minute TTL). TTL eviction
on read. Deduplication by `node_id`. Drops lowest-importance slot on overflow.  
`MemoryStore`: thin facade over `CognitiveGraphDB` for the five durable tiers.
`store()` → persists node + optional embedding. `link()` → upserts a typed edge.
`record_procedure_outcome()` → updates success/failure counters and advances
`ProcedureStage` (CANDIDATE→OBSERVED at 1 success → REPEATED at 3 → TRUSTED
at 5).

---

### Phase 6 — Memory Admission

**19. `aoca/admission.py` — MemoryAdmissionPolicy.**  
Deterministic pipeline:
1. Privacy filter (Phase 2 `sanitize()`) runs first — always.
2. PROHIBITED sensitivity → hard REJECT, emits event, no node returned.
3. SECRET_REFERENCE → `safe_summary` truncated to 80 chars.
4. Deduplication check → REJECT with `duplicate_of`.
5. Contradiction check → REQUIRE_USER_CONFIRMATION with `contradicts`.
6. Memory value V_i (10 components, Bayesian-smoothed confidence).
7. V < 0.10 → REJECT. V < 0.25 → WORKING_ONLY. V < 0.45 → EPISODIC.
8. PROCEDURE type → PROCEDURAL_CANDIDATE. PREFERENCE type → PREFERENCE.
9. V ≥ 0.45 → SEMANTIC.

**20. Memory value formula V_i.**  
10 components: relevance (0.20), novelty (0.15), importance (0.15), confidence
(0.10), recency (0.10), stability (0.05), emotional salience (0.05), source
reliability (0.10), retrieval count log-scaled (0.05), goal alignment (0.05).
Clipped [0, 1].

**21. Bayesian-smoothed confidence.**  
Beta-posterior mean: `(α + prior + pos) / (α + β + prior + 1 + pos + neg)`.

---

### Phase 6 — Consolidation

**22. `aoca/consolidation.py` — idle-only background service.**  
Runs only when `_busy` is clear. Stops immediately on `set_busy()` or `stop()`.
Cycle operations:
- Archive candidates: nodes with `importance < 0.15` or `last_accessed_at` older
  than 30 days, protected=0.
- Near-duplicate detection: cosine similarity ≥ 0.95 pairs queued for review
  (not auto-merged).
- Orphan edge cleanup: edges referencing deleted nodes.
- DB quick-check: `PRAGMA quick_check(1)`.

**23. `set_busy()` / `clear_busy()` called from CognitiveService.**  
Every interactive retrieval sets busy before and clears after, preventing
consolidation from interfering with live responses.

---

### Phase 6 — Legacy Import

**24. `aoca/legacy_import.py` — transactional import from `.swarm/memory.db`.**  
- Backs up source DB before any write (`shutil.copy2`).
- Reads 28 active entries (skips `deleted=1`).
- Sensitivity classification: key/content substring match → PUBLIC / PERSONAL /
  SECRET_REFERENCE.
- `bank_account_details` → SECRET_REFERENCE, `safe_summary` = `[secret reference: key]`.
- `identity/email` → PERSONAL, `safe_summary` = `[personal data: key]`.
- Validates embeddings: 384-dim, all finite JSON float32.
- Provenance: `legacy:ruflo`.
- Deduplicates by `canonical_key` before writing.
- Returns `ImportReport` with counts.
- Never modifies or deletes original DB.

---

### Phase 6 — Cognitive Service Facade

**25. `aoca/cognition.py` — CognitiveService.**  
Single entry point integrating all subsystems.
- `get_context()`: spreading activation on anchors → two-stage retrieval →
  working memory summaries → `CognitiveContext`. Context budget: 5 (simple) /
  10 (project) / 20 (search). Emits `cognitive.context.retrieved`.
- `admit()`: runs admission policy → generates embedding → stores to graph.
- `remember_fact()`: one-line convenience to admit a SEMANTIC CONCEPT node.
- `start()` / `stop()`: manages consolidation service lifecycle.
- All methods set/clear `busy` around the consolidation service.

**26. `CognitiveContext.to_prompt_block()`.**  
Formats working summaries and ranked retrieval results as a `[Cognitive Memory]`
block for injection into the system prompt. Empty context returns `""`.

---

### Runtime Wiring

**27. `main.py` — cognitive context injected before model call.**  
In `_build_config()`, if `AOCA_RETRIEVAL_ENABLED` is set, `get_cognitive_service()`
fetches context for the current prompt (using `mem_str` as query) and inserts the
`to_prompt_block()` result between legacy memory and the system prompt. Wrapped in
try/except so any failure is silent — cognitive context is always additive.

**28. `Jarvis.spec` updated.**  
`hiddenimports` extended with all 9 new `aoca.*` modules:
`aoca.graph`, `aoca.embed`, `aoca.memory`, `aoca.retrieval`, `aoca.activation`,
`aoca.admission`, `aoca.consolidation`, `aoca.legacy_import`, `aoca.cognition`.

---

### Tests

**29-35. Test suites (27 mandatory tests across 4 files).**

| Suite | Tests | Mandatory IDs | Result |
|-------|-------|---------------|--------|
| `test_phase4_graph.py` | 12 | 1, 2, 20, 21 | OK |
| `test_phase5_retrieval.py` | 16 | 3, 4, 11-15 | OK |
| `test_phase6_memory.py` | 15 | 5-10, 16-19 | OK |
| `test_phase46_integration.py` | 8 | 22-27 | OK |

**36. Full regression baseline.**

| Suite | Tests | Result |
|-------|-------|--------|
| `test_phase4_graph.py` | 12 | OK |
| `test_phase5_retrieval.py` | 16 | OK |
| `test_phase6_memory.py` | 15 | OK |
| `test_phase46_integration.py` | 8 | OK |
| `test_phase1_safety.py` | 27 | OK |
| `test_phase2_events.py` | 41 | OK |
| `test_phase3_verification.py` | 30 | OK |
| `test_integration.py` | 12 | OK |
| **Total** | **161** | **OK** |

---

## Security constraints — compliance checklist

| Constraint | Status |
|------------|--------|
| `AOCA_LEARNING_ENABLED` locked off | ✓ `_LOCKED_OFF` in config.py — returns False regardless |
| PROHIBITED sensitivity never stored | ✓ Hard REJECT in `admission.py` before any DB write |
| Privacy filter before every admission | ✓ First line of `MemoryAdmissionPolicy.evaluate()` |
| SECRET_REFERENCE summary truncated to 80 chars | ✓ `admission.py` |
| Bank account details → SECRET_REFERENCE only | ✓ `legacy_import.py` |
| No world model / planner / bandit / Neural Core | ✓ Absent; locked off in config |
| No procedural memory from assumed result | ✓ `record_procedure_outcome()` requires explicit call; no auto-advancement |
| Webpage/email content never changes policy | ✓ No inbound content path to config or safety kernel |
| No self-modifying code | ✓ No source rewrites, no dynamic exec |
| Do not emit success merely because function returned | ✓ `cognitive.admission.accepted` only fires after full policy pass |
| Protected nodes/edges immutable | ✓ `upsert_node()` / `upsert_edge()` guard + test coverage |
| Creator identity (Bhuvan→JARVIS/FRIDAY) protected | ✓ Seeded as protected=True, edge protected=True |

---

## Files created or modified

| File | Action |
|------|--------|
| `aoca/graph.py` | completed (CognitiveGraphDB class appended) |
| `aoca/embed.py` | created |
| `aoca/memory.py` | created |
| `aoca/retrieval.py` | created |
| `aoca/activation.py` | created |
| `aoca/admission.py` | created |
| `aoca/consolidation.py` | created |
| `aoca/legacy_import.py` | created |
| `aoca/cognition.py` | created |
| `aoca/tests/test_phase4_graph.py` | created |
| `aoca/tests/test_phase5_retrieval.py` | created |
| `aoca/tests/test_phase6_memory.py` | created |
| `aoca/tests/test_phase46_integration.py` | created |
| `aoca/config.py` | modified (Phase 4-6 flags) |
| `main.py` | modified (cognitive context injection in `_build_config`) |
| `Jarvis.spec` | modified (9 new hiddenimports) |
| `data/` | directory created (target for `aoca_cognition.db`) |
| `docs/AOCA_PHASES_4_6_REPORT.md` | created |
