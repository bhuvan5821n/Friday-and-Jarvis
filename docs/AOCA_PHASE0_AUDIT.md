# ASTRAEUS OMEGA COGNITIVE ARCHITECTURE — Phase 0 Audit

Project: `d:\ai model` — JARVIS / FRIDAY
Audit date: 2026-08-05
Scope: read-only inspection. Nothing was modified.
Method: 5 parallel code-survey agents + direct inspection of DBs, deps, tests.

---

## 0. Corrections to the brief

Six systems named in the preservation list **do not exist** in this repo. They
cannot be preserved, and the architecture must not assume them.

| Named | Reality |
|---|---|
| RAM Guardian | No such module. `core/monitor.py:15-22` is a threshold *alerter*. It speaks a warning at RAM ≥ 92 %. It frees nothing, kills nothing, unloads nothing. |
| Ollama / local models | Zero local inference. No `ollama`, no `llama_cpp`, no `torch`, no port 11434. "Ollama" is a routing *string* (`core/ai.py:110`) forwarded to OmniRoute, and a UI button (`ui.py:3168`). |
| Gmail (personal + college) | No OAuth, IMAP, SMTP, or Gmail client anywhere. Only `gmail.com` as a domain string in UI automation (`actions/computer_control.py:87`). |
| Wi-Fi sensing | One allowlisted read: `netsh wlan show interfaces` (`remote_control/commands.py:102`). No RSSI history, no presence inference, nothing persisted. |
| FRIDAY wake word | Only the `hey_jarvis` model is loaded (`core/wake_word.py:24`, `main.py:798`). Saying "Friday" wakes nothing. `ui.py:3786` prints "say 'Friday' to wake me" — **that message is false**. |
| Existing state machine (single) | Three disagreeing state vocabularies + one dead enum. See §2. |

Two more claims worth correcting:

- **`ruvector.db` (1.5 MB at repo root) contains zero vectors.** It is not
  SQLite; magic bytes are `redb`. The entire file is one config key declaring
  384-dim cosine. It is an empty preallocated shell, gitignored, referenced by
  no Python or JS in the project.
- **There is no existing neural or cognitive structure in Python.** The pulsing
  rings and particles (`ui.py:288-302`, `ui.py:413-518`, `friday/avatar.py:348`)
  are QPainter animation driven by microphone amplitude. Decoration, not a
  network. The audit found no trainable model, no graph, no bandit, no state
  estimator, and no tool-performance learner in project code.

---

## 1. Existing architecture

**Entry:** `main.py:1420 main()` owns `QApplication` and `app.exec()`
(`main.py:1457`, `:1554`). `ui.py` is never launched directly and has no
`__main__`; `main.py:1459` constructs `JarvisUI`. `launcher/assistant_launcher.py:216`
is a fire-and-exit CLI that either talks IPC to a running instance or cold-starts
`main.py`. `service.py:73` is a 10-second-poll watchdog.

**Runtime shape:** Qt main thread owns all widgets. One daemon thread runs
`asyncio.run(JarvisLive(ui).run())` (`main.py:1536-1549`), inside which a
`TaskGroup` (`main.py:1382`) spawns five tasks: `_send_realtime`, `_listen_audio`,
`_receive_audio`, `_play_audio`, `_auto_sleep`. Blocking tools go to the default
executor (`main.py:981-1018`). Cross-thread entry via
`asyncio.run_coroutine_threadsafe` (`main.py:810`, `:880`).

**`QThread` is never used.** Every worker is a raw daemon `threading.Thread`.
Background→UI always crosses via `pyqtSignal.emit` (Qt auto-queues onto the GUI
thread) — with one exception: `JarvisUI.set_audio_level` (`ui.py:3981-3992`)
writes `self._win.hud.level` directly from the audio thread under a bare
`except`.

**Cognition:** entirely remote. Voice is Gemini Live (`main.py:1368`) with
server-side function calling. Text goes through OmniRoute — `core/ai.py`, an
OpenAI-compatible endpoint at `localhost:20128/v1` (`core/ai.py:49`) external to
this repo. The agent subsystem bypasses OmniRoute entirely and calls
`google.generativeai` directly with hardcoded `gemini-2.5-flash` (`agent/planner.py:175`,
`agent/executor.py:30,131,149,380`, `agent/error_handler.py:81`).

**Size:** 120 Python files, ~24 k LOC excluding venv/build. `main.py` 74 KB,
`ui.py` 188 KB.

---

## 2. Existing state machine

There is no single state machine. Four vocabularies:

| Layer | Where | States |
|---|---|---|
| Runtime labels (bare strings, no enum) | emitted via `ui.set_state()` | `THINKING` `main.py:957,1376,1416`; `LISTENING` `:970,1159,1402`; `SPEAKING` `:825` |
| Lifecycle/IPC | `core/lifecycle_server.py:36,55` | `STARTING` → `READY` (`main.py:1550`) → `SHUTTING_DOWN` (`main.py:1490`) |
| FRIDAY visuals | `friday/states.py:32-52` | 19 states, default `idle` (`:54`) |
| **Dead** | `core/jarvis_app.py:46 JarvisState` | `HIDDEN/LISTENING/THINKING/CONVERSATION` — nothing constructs `JarvisApp`; `ui.py:2046` sets it to `None` and both call sites are `getattr(...)` no-ops. ~530 unreachable lines. |

All transitions funnel through `ui.py:3716 MainWindow._apply_state()`.

**Defect found:** two runtime→visual maps disagree on the same input string.
`friday/state_controller.py:11-16` has 10 entries and maps `MUTED→sleeping`;
`friday/bridge.py:20-40` has 19 and maps `MUTED→muted`. Both are invoked on the
same string at `ui.py:3723` and `:3726`.

---

## 3. Existing event flow

**A real event bus exists and is almost entirely unused.** `core/events.py:30
EventBus` is genuine: `RLock`-guarded (`:35`), `subscribe()` returns an
unsubscribe closure (`:37-50`), `publish()` isolates handler exceptions and
returns failures (`:52-63`), `"*"` wildcard (`:56`), frozen `Event` dataclass with
`topic/payload/source/correlation_id/occurred_at` (`:18-24`), module singleton at
`:70`.

Total production usage: three lines in `Studios/chat.py:254,266,270`. **Zero
`bus.subscribe()` calls outside `tests/test_platform_foundation.py`.** It
publishes into the void.

Everything else is PyQt signals used as thread-marshalling, not as a bus — each
emitted by one owner, connected to one slot in the same class. `MainWindow`
declares 14 signals at `ui.py:1948-1961`, wired at `:2013-2026`. `FridayBridge`
declares 10 at `friday/bridge.py:50-59`, wired in one function at `:157-168`.

**Consequence for AOCA:** there is no existing stream of typed events to observe.
Layer 1 must *create* the event flow, not adapt one.

---

## 4. Existing learning capabilities

Honest tally: **one primitive adaptive weight, and nothing else.**

- `memory/memory_manager.py:65-69` `_score() = (importance, min(freq,9), updated)`
  with frequency reinforcement on fact re-surface (`:163-167`). This is the only
  learning signal written by project code.
- `core/rate_limiter.py:13-37` counts per-tool calls with cooldowns and burst
  windows. It counts; it never learns. In-memory only, lost on restart.
- `Logs/ai_metrics.jsonl` (77 lines) — append-only per-call
  `{model, served, provider, task, ok, latency, bytes, error, ts}` written by
  `core/ai.py:283-291`, read newest-first by `history()` (`:244`). **This is a
  ready-made per-model success/latency reward signal.** Best reuse target in the
  repo.

**Real-shaped but inert:** `.swarm/memory.db` `causal_edges` has 14 rows, every
one `similarity=0.0, uplift=NULL, confidence=0.3, sample_size=NULL` —
default-stamped, no inference ran. `episodes` (25) and `reasoning_patterns` (25)
are auto-mirrored side effects of `ruflo memory store`: `task`/`approach` are
literally the memory content string, `reward=0.0, success=0`.

**Empty schemas that match AOCA needs exactly** (0 rows, 0 code):
`graph_edges` (source, target, relation, weight, confidence, decay_rate,
last_reinforced), `learning_experiences` (state, action, reward, next_state —
SARSA-shaped), `learning_policies` (q_values, visit_counts, avg_rewards),
`skills` (success_rate, uses, avg_reward, avg_latency_ms), `skill_links`,
`facts`, `consolidated_memories`, `memory_scores`.

These belong to `ruflo` (a node package), not to this project. See §8.

---

## 5. Existing memory capabilities

**A. `memory/long_term.json` — the real store.** Atomic write via
`mkstemp`+`os.replace` under a module lock (`memory/memory_manager.py:107-137`).
Six fixed categories (`:28-35`). Entry shape `{value, updated, freq, importance}`.
Value dedupe (`:72-91`), budgeted eviction to 6000 chars with identity evicted
last (`:94-105`), injected into the system prompt capped at 4000 chars (`:224-268`).

Current contents: 5 identity, 2 preferences, 1 project, 1 relationship, 12
notes — **8 of the notes are duplicate RAM-alert spam** (`memory/long_term.json:77-124`).
Dedupe keys on the exact value string, so "Memory usage is at 92 percent" and
"…at 93%" never merge. This is the existing importance model failing in
production, and it is the strongest single argument for Layer 4.

**B. `.swarm/memory.db` — 942 KB SQLite, 38 tables, written exclusively by node.**
No Python file in the repo imports `sqlite3` (grep: zero hits). Python reaches it
only by shelling out to `node node_modules/ruflo/bin/ruflo.js`
(`memory/ruflo_bridge.py:34-48`). 30 `memory_entries` rows, all with 384-dim
embeddings from `Xenova/all-MiniLM-L6-v2` running locally in-process under node
(no API key, no network). This is a **genuine vector memory** — the one real
cognitive asset present.

Access pattern is the problem: subprocess-per-call, ~1 s synchronous search,
results parsed by **splitting ASCII table pipes** (`memory/ruflo_bridge.py:76-82`).
Writes are fire-and-forget on an unbounded daemon thread per fact (`:61-64`),
each spawning a full node process.

**C. `.claude/memory.db`** — same schema, 11 tables, all zero rows. Dead, and
**committed to git** while its sibling is gitignored.

**Scoping: none.** `memory/long_term.json` has no assistant key anywhere.
`memory/ruflo_bridge.py:26 NAMESPACE = "jarvis"` is hardcoded for store, delete
and search — FRIDAY's facts are written into a namespace literally named
"jarvis". Memory injection is persona-blind (`main.py:896-897`).

---

## 6. Reusable components

Ranked by value.

1. **`core/events.py` EventBus** — correct, tested, thread-safe, unused. Adopt as
   the Layer 1 transport unchanged. Zero-edit attach: `bus.subscribe("*", handler)`.
2. **`Logs/ai_metrics.jsonl` + `core/ai.py:283-291`** — existing per-call outcome
   telemetry. Becomes the initial bandit reward signal without writing a collector.
3. **`remote_control/security/` + `commands.py` + `confirmation.py`** — a
   deterministic, fail-closed, well-tested safety kernel already exists for the
   remote path. **Layer 0 should generalize this code, not invent a parallel one.**
   Allowlist `commands.py:75-115`, `shell=False` enforced `:150`, fail-closed
   `:142`, single-use 120 s confirmation tokens `confirmation.py:34-39`,
   secret-scrubbing JSONL audit `audit.py:40-59`.
4. **`.swarm/memory.db` vector memory** — 384-dim MiniLM embeddings, already
   populated. Reuse the *data*; replace the ASCII-table shell-out with direct
   `sqlite3` reads from Python.
5. **`Studios/registry.py:10-39`** — a real plugin registry with a `Protocol`
   contract (`Studios/contracts.py:60`) and `replace=True` support. The intended
   extension point; works.
6. **`launcher/process_registry.py`** — PID-reuse-safe process ownership with two
   independent proofs before terminate (`:130-162`, `:237`). Reuse directly for
   Layer 9 verification of "did the app actually open / close".
7. **`friday/bridge.py`** — the best-designed seam in the repo; 10 typed signals,
   one `connect()`. Model Layer 12's telemetry surface on it.
8. **`core/lifecycle_server.py:27`** — authenticated loopback command dict
   (HMAC token in `runtime/lifecycle_token`). Add AOCA control commands here.

**Do not build on:** `core/jarvis_app.py` (dead), `Studios/router.py`
(`StudioIntentRouter` instantiated nowhere), `services/web_intelligence/security.py:81-117`
(`ACTION_RISK`/`requires_confirmation` defined but imported by nothing — dead
policy).

---

## 7. Missing components

Against the 12-layer target:

| Layer | Status |
|---|---|
| 0 Safety kernel | **Half exists.** Remote path is solid. Local voice path has *only* a rate limiter (`main.py:939-947`); it throttles, never denies on danger. `shutdown_jarvis` does `os._exit(0)` with no confirmation (`main.py:1130`). Power ops check `confirmed=yes` — **but the flag is supplied by the LLM in its own tool args** (`actions/computer_settings.py:634-640`), so it is an LLM decision, not a gate. |
| 1 Event ingestion | Bus exists, no typed events, no `trace_id`, no producers. |
| 2 State estimation | Absent. No filter, no belief, no latent state. |
| 3 Temporal core | Absent. |
| 4 Memory hierarchy | Flat JSON + vector store. No episodic/procedural/preference/associative separation, no decay, no consolidation. |
| 5 Knowledge graph | Absent in Python. Empty `graph_edges` schema on the node side. |
| 6 Expert router | Absent. Model routing is a keyword table (`core/ai.py:114-127`); tool dispatch is two divergent if/elif chains. |
| 7 World model | Absent. |
| 8 Planner | Partial — `agent/planner.py` is an LLM prompt producing a step list. No constraints, no risk, no replanning. |
| 9 Execution verification | **Absent, and this is the highest-value gap.** Success is assumed from no-exception. `main.py:982` returns `r or f"Opened {app_name}."` — the tool reports success from a *string default*. `actions/open_app.py:246` returns `True` after `subprocess.Popen(shell=True)` + `time.sleep(1.5)`, despite `psutil` being imported at `:7`. |
| 10 Continual learning | Absent. |
| 11 Uncertainty | Absent. `core/ai.py:262-279` `_routing_reason()` returns a hand-coded confidence percentage — a hardcoded string, not a measurement. |
| 12 Visualization | Decoration only. |

**No tool registry.** `main.py:932-1165 _execute_tool()` is a ~185-line `if/elif`
on `fc.name`. `agent/executor.py:174-247 _call_tool()` is a second, divergent
chain with a different tool set — and its unknown-tool fallback is
`_run_generated_code` (`:246`), which writes LLM-authored Python to a temp file
and runs it via `subprocess.run([sys.executable, tmp])` (`:81`) **with no gate at
all**. `TOOL_DECLARATIONS` (`main.py:215-780`) and `PLANNER_PROMPT`
(`agent/planner.py:17-166`) are two more hand-maintained copies of the tool list.
Four sources of truth for one set of tools.

---

## 8. Dependency risks

Interpreter: **Python 3.14.5**. 104 packages installed.

| Needed | Status |
|---|---|
| numpy 2.5.0, scipy 1.18.0, scikit-learn 1.9.0, networkx 3.6.1 | **Already installed.** Sufficient for Layers 2, 5, 6, and the bandit. |
| torch | **Absent.** 2.13.0 is available for 3.14. |
| sentence-transformers, faiss, chromadb, sqlalchemy | Absent. |

Risks:

1. **Python 3.14 is new.** Torch 2.13 has cp314 wheels, but the default PyPI
   wheel on Windows is CPU-only — CUDA requires the PyTorch index URL. A
   silently-CPU torch on an RTX 3050 will look like a performance mystery later.
2. **Torch is a ~2.5 GB install and imports in ~2 s.** On a 16 GB machine sharing
   RAM with Chrome, Playwright and a Gemini Live stream, importing torch on the
   hot path is a startup regression. It must be lazy-imported behind a capability
   flag, never at module scope.
3. **numpy is currently used only for audio/video DSP** (`main.py:16`,
   `core/wake_word.py:10`, `ui.py:1924`, three action files). Introducing it for
   linear algebra is new territory for this codebase — no existing numerical
   conventions to follow.
4. **The embedding model lives on the node side.** Reusing it from Python means
   either keeping the node shell-out or adding `onnxruntime`-based MiniLM in
   Python (`onnxruntime` 1.27 is already installed for openWakeWord — this is the
   cheap path).
5. **`remote_control/tests/test_hermes_plugin_gate.py` fails on
   `ModuleNotFoundError: yaml`** — it imports through `D:\hermes ai\hermes-agent`,
   an out-of-repo dependency. One test error out of 228 is caused by an external
   project's missing dep.
6. **No `pytest`.** All tests are `unittest`. Do not introduce pytest.
7. `requirements.txt` starts with a UTF-8 BOM and does not pin any version.

---

## 9. RAM and VRAM risks

Measured hardware assumption: i5-12450HX, 16 GB RAM, RTX 3050 6 GB.

1. **VRAM is never measured.** `nvidia-smi` is called for *utilization percent*
   only (`friday/data.py:159-177`, `ui.py:147-165`). There is no free-VRAM
   reading anywhere, so Layer 8's VRAM constraint has no input signal today.
2. **Nothing is ever unloaded** because nothing local is ever loaded. The
   "one heavy local model at a time" rule has no existing violator — but also no
   existing enforcement point.
3. **`memory/ruflo_bridge.py:61-64` spawns an unbounded daemon thread per fact,
   each launching a full node process.** A burst of saves is a burst of node
   processes contending on one WAL file. Under AOCA, memory writes will become
   far more frequent — this will amplify an existing flaw into a real one.
4. Playwright launches against **real user browser profiles**
   (`actions/browser_control.py:95`). A live Chrome plus a browser-automation
   Chrome plus torch plus the Gemini Live stream is the realistic worst case, and
   it is already close to 16 GB.
5. `ui.py:98-134 _SysMetrics` polls cpu/mem/net/gpu/temp on a 3.0 s tick from a
   background thread. Volatile, never stored — free telemetry for Layer 2 if
   persisted.
6. **The existing RAM alert path already pollutes memory** — `core/monitor.py`
   alerts flow into `long_term.json` as notes and never merge (§5). Any AOCA
   ingestion layer must not repeat this.

---

## 10. Database risks

1. **`foreign_keys=0` on both SQLite DBs.** `causal_edges.from_memory_id`,
   `exp_edges.src_node_id`, `skill_links.parent_skill_id` are unenforced
   integers. Deleting a memory silently orphans edges.
2. **No project-owned migration system.** A `migration_state` table exists with
   0 rows; `.swarm/schema.sql` is ruflo's bootstrap. Two DBs have already
   diverged (38 tables vs 11) with no reconciliation. `metadata.schema_version`
   is a string *ruflo* writes.
3. **Read-modify-write race in the JSON store.** `load_memory()` re-reads and
   re-parses the whole file on every `update_memory` (`memory_manager.py:185`)
   and again in `forget` (`:279`). The lock covers file I/O, not the transaction
   — two threads can lose an update.
4. **Silent failure everywhere.** `ruflo_bridge._run()` swallows CLI errors and
   returns `""` (`:46-48`); `_log_metric` swallows all (`core/ai.py:290`);
   `_mirror_to_ruflo` swallows all (`memory_manager.py:207`). **A permanently
   broken vector mirror is indistinguishable from a healthy one.** Any AOCA
   persistence must surface write failures.
5. WAL is on for `.swarm/memory.db` — set by ruflo, not by this project.
6. `.claude/memory.db` (empty, 11 tables) is committed to git.
7. No unparameterized SQL in Python — because there is no SQL in Python at all.

---

## 11. Security risks

Ranked by exploitability.

1. **`agent/executor.py:29-108 _run_generated_code`** — LLM writes Python, it is
   written to a temp file and executed via `subprocess.run([sys.executable, tmp])`.
   No gate, no sandbox, no allowlist. It is the **fallback for any unknown tool**
   (`:246`). This is arbitrary code execution reachable from a model hallucinating
   a tool name.
2. **Web page text reaches the model unfenced.** `services/web_intelligence/tool.py:92`
   returns 8000 raw characters of page text as a tool result, fed back at
   `main.py:1067-1072`. `scan_for_injection` (`security.py:51`) and
   `fence_untrusted` (`:60`) exist — but fencing is applied **only** in
   `deep_research` (`service.py:292`). `read_url`, search titles/snippets
   (`tool.py:74-83`), RSS, GitHub READMEs and YouTube transcripts all deliver
   unfenced.
3. **`actions/browser_control.py:674-675`** returns 4000 chars of Playwright
   `body` text with **no injection scan and no fencing at all** — it bypasses
   `web_intelligence` entirely.
4. **Injected content can reach durable memory in one hop.** The `save_memory`
   tool (`main.py:967` → `memory_manager.update_memory:182`) lets the model
   choose key and value, and the value is re-injected into every future system
   prompt (`:224`). A poisoned page becomes a permanent instruction.
5. **The LLM can rewrite its own identity.** The `change_voice` tool
   (`main.py:1108-1128`) mutates `config/api_keys.json["persona"]` with no
   confirmation.
6. **`os.environ["persona"]` shadows the config** (`main.py:106-108`) — an
   environment variable named `persona` silently pins assistant identity.
7. **LLM-supplied confirmation flags** — `actions/computer_settings.py:634-640`
   accepts `confirmed=yes` from the model's own tool arguments for restart and
   shutdown.
8. `actions/desktop.py:83-101 _execute_generated_code` — `exec()` of LLM code in
   a restricted-builtins dict. Sandbox-by-namespace; escapable.

**Clean by contrast, and worth copying:** the NEXUS audit log stores what was
done and never message bodies or model replies (`audit.py:11-13`,
`router.py:116-119`); the process registry holds no secrets
(`process_registry.py:9`); `friday/data.py:1-40` returns `None` rather than
fabricating unmeasurable values.

---

## 12. Proposed mathematical architecture

The brief specifies twelve layers including Mamba-style selective SSMs, Neural
ODEs, Dreamer-style variational world models, CVaR-constrained MPC, EWC, and
modern Hopfield retrieval. All are implementable. Not all of them **earn their
keep on this system in this order**, and the audit changes the sequencing.

The decisive finding is §7 Layer 9: **nothing is verified today.** Every learned
quantity in the brief — bandit reward `R_t`, Hebbian `Δw_ij`, memory value `V_i`,
world-model outcome `p(r_t|s_t)`, calibration — is defined as a function of
*verified* outcome. With no verification, every one of those equations is fed
`success = "no exception was raised"`. Building Layers 5-11 before Layer 9 means
building a learner whose training signal is a constant.

So the mathematics is staged by what its inputs actually exist:

**Tier A — the input signals do not exist yet; build these first.**

- *Layer 0 hard mask.* `m_k(x) ∈ {0,1}`, applied as
  `p_safe(k|x) = m_k(x)p(k|x) / Σ_j m_j(x)p(j|x)`, all-zero ⇒ refuse.
  Implemented as a deterministic allowlist generalized from
  `remote_control/commands.py`. No learned score can raise a zero.
- *Layer 1 typed `CognitiveEvent` + `trace_id`*, published on the existing bus.
- *Layer 9 verification predicates* — process exists with a fresh create time,
  file exists with matching size/hash, measured free RAM after cleanup. Then the
  outcome function
  `R_t = 0.24E + 0.20V + 0.16A + 0.12Q + 0.10P + 0.08T + 0.06C + 0.04H`,
  with silence ≠ acceptance (conservative default for `A`).

**Tier B — cheap, high-value, numpy/scipy only, no torch.**

- *Layer 2 EKF over numerical telemetry* (RAM, CPU, latency, network). Predict
  `μ⁻ = f(μ,u)`, `Σ⁻ = FΣFᵀ + Q`; correct with a **Cholesky solve, never an
  explicit inverse** — `K = Σ⁻Hᵀ S⁻¹` computed as `cho_solve`. Joseph-form
  covariance update to preserve symmetry over long runs. `scipy` is installed.
- *Layer 6 router.* Logits
  `l_k = w_kᵀc + b_k + success_k − failure_k − latency_k − resource_k − risk_k`,
  softmax, hard mask, top-K (K=1 deterministic, K≤3 research). Bootstrapped from
  `Logs/ai_metrics.jsonl`, which already contains `ok` and `latency` per call.
- *Contextual bandit.* Bayesian linear regression per tool,
  `θ_k ~ N(μ_k, Σ_k)`, Thompson sampling, closed-form conjugate updates — no
  gradients, no torch. **Exploration disabled** for delete/send/power/credential
  actions, and updates applied only on verified outcomes.
- *Layer 4 memory value and decay.* The weighted `V_i` sum and closed-form
  `m_i(t+Δt) = m_i(t)e^{-λΔt} + ∫…` per memory class; no decay for creator
  identity, safety rules, pinned facts. Retention as a greedy
  value-density knapsack (`V_i − λ_c c_i − λ_r r_i − λ_q q_i` per byte), not an
  exact solver.
- *Layer 5 graph.* Typed temporal multigraph in SQLite, `−1 ≤ w_ij ≤ 1`.
  Spreading activation with the specified caps (depth 3, ≤128 nodes, ≤32
  neighbours, ε=0.001) — sparse matrix-vector products in `scipy.sparse`, which
  is exactly what a bounded subgraph needs. Hebbian updates
  `Δw⁺ = η⁺a_ia_jR(1−|w|)`, `η⁺=0.01`, `η⁻=0.02`, `λ_w=0.0005`.
- *Layer 11 uncertainty.* The bounded weighted sum `U_total` and
  `Confidence = e^{−kU} · VerificationStrength · PolicyValidity`, with `k`
  fitted by isotonic regression on held-out historical outcomes (`sklearn` is
  installed). No displayed confidence above what calibration supports.
- *Causal separation.* Correlation / temporal sequence / suspected / verified as
  distinct edge grades, each carrying evidence count and confounder warning.
  `ASSOCIATED_WITH` is never silently promoted to `CAUSES`.

**Tier C — requires torch; justify each before adding.**

- *Layer 3 attention over the bounded working context* — worth it, but the
  context is ≤128 items, so this is a 128×128 softmax. numpy does this in
  microseconds. Torch is not required until it is trained.
- *Layer 5 relation-aware graph attention* (`e_ij` with `W_r r_ij`, `W_t τ_ij`) —
  needs training, hence torch, hence Tier C.
- *Layer 8 test-time memory `M_W`* with surprise `‖∇_W L‖`, novelty-gated
  `η_t`, momentum `m_t`, and norm projection `Π_B(W) = W·min(1, B/‖W‖_F)` — a
  genuinely small MLP. Torch, CPU, bounded parameter count, snapshot before every
  update.
- *Layer 7 variational world model* — the last thing to build, and only after
  `world_model_transitions` has real rows from verified executions.
- *Selective SSM (Layer 3B) and Neural-ODE dynamics (Layer 3C)* — **defer.** A
  selective SSM earns its keep on long token streams; this system's temporal
  state is a few hundred bounded events per day. An ODE solver for urgency and
  confidence drift is a first-order linear decay wearing a solver. Both are
  specified, both remain in the design, and both should be built only when a
  measured deficiency in the exponential-decay baseline justifies the solver.
  Ship the closed-form decay first; it is the same equation with `f` linear.
- *Layer 8 CVaR MPC.* `J_risk = E[L] + λCVaR_α(L) + λ_u U`, horizon ≤8, ≤16
  candidate plans, execute only the first safe action then re-observe.
  Sampling-based CVaR over 16 candidates is a sort and a mean — no torch.

**Stability.** Every adaptive subsystem gets `V(x) = xᵀPx` with a per-update
check `ΔV ≤ −α‖x‖² + bounded input`; on violation: reduce learning rate, project
into the safe set, roll back, pause, log. Practical numerical check — not claimed
as a formal proof, since the assumptions are not satisfied.

**Assistant scope** is a mandatory column (`SHARED|JARVIS|FRIDAY|NEXUS`) on every
node, edge, memory, trace and statistic, captured **once at turn start** into a
request-scoped context object. The cognitive layer must never call
`_get_persona()` itself — §11 item 5-6 shows the value can change mid-turn.

---

## 13. Exact files to create

All under a new top-level package `aoca/`. No existing file is renamed or moved.

**Phase 1 — safety and events**
```
aoca/__init__.py                  capability flags, lazy imports, version
aoca/config.py                    all limits/thresholds as data (no hardcoding)
aoca/events.py                    CognitiveEvent dataclass, trace_id, factories
aoca/safety/kernel.py             deterministic mask m_k(x), refusal shapes
aoca/safety/policy.py             action classes, confirmation requirements
aoca/safety/scopes.py             AssistantScope enum + request-scoped context
aoca/ingest/adapters.py           bus subscriber → CognitiveEvent normalizer
aoca/tests/test_safety_kernel.py  unittest
aoca/tests/test_events.py         unittest
```

**Phase 2 — persistence**
```
aoca/db/schema.sql                project-owned schema, FKs ON, WAL
aoca/db/store.py                  connection mgmt, migrations, integrity check
aoca/db/migrations/0001_init.py
aoca/tests/test_store.py
```

**Phase 3-4 — estimation and memory**
```
aoca/estimate/ekf.py              Cholesky-solve EKF, Joseph update
aoca/estimate/telemetry.py        psutil + nvidia-smi free-VRAM reader
aoca/memory/hierarchy.py          working/episodic/semantic/procedural/preference
aoca/memory/value.py              V_i, decay, knapsack retention
aoca/memory/vector.py             direct sqlite3 read of MiniLM embeddings
aoca/memory/associative.py        Hopfield refinement over top-N candidates
aoca/tests/test_ekf.py  test_memory_value.py  test_associative.py
```

**Phase 5-7 — graph, router, verification**
```
aoca/graph/model.py               typed temporal multigraph
aoca/graph/activation.py          bounded spreading activation (scipy.sparse)
aoca/graph/hebbian.py             clipped weight updates
aoca/route/experts.py             masked sparse router
aoca/route/bandit.py              Bayesian linear TS, conjugate updates
aoca/verify/predicates.py         process/file/ram/url verification
aoca/verify/outcome.py            R_t and penalties
aoca/tests/…                      one per module
```

**Phase 8-11 — world model, planner, learning, metacognition**
```
aoca/world/model.py               lazy-torch, capability-gated
aoca/plan/mpc.py                  receding horizon, CVaR, constraints
aoca/learn/continual.py           EWC + replay + distill, promote/rollback
aoca/learn/testtime.py            M_W with norm projection
aoca/meta/uncertainty.py          U_total, calibration
aoca/meta/contradiction.py        C score, CONTRADICTS edges
aoca/stability/lyapunov.py        ΔV guard applied to every adaptive update
```

**Phase 12 — UI**
```
aoca/ui/neural_core_page.py       new QStackedWidget page (index 7)
aoca/ui/telemetry_bridge.py       pyqtSignal surface, modelled on friday/bridge.py
aoca/ui/explain.py                score breakdown panel
```

**Phase 13 — controls**
```
aoca/control/commands.py          pause/resume learning, pin, forget, snapshot
```

---

## 14. Exact files requiring modification

Minimal and additive. Each is one insertion, no rewrites.

| File | Change | Why |
|---|---|---|
| `main.py:947` | one call to `aoca.safety.kernel.gate(name, args, ctx)` after the rate-limit block, before `:949` | **Highest-coverage line in the codebase** — every voice tool passes here. Refusal shape already exists at `:944-947`. |
| `agent/executor.py:174` | same gate at the top of `_call_tool` | This subsystem bypasses `_execute_tool` entirely and its unknown-tool fallback is arbitrary code execution. Without this the gate is a bypass, not a gate. |
| `agent/executor.py:246` | unknown tool → refuse instead of `_run_generated_code` | Closes §11 item 1. |
| `main.py` (~10 lines) | `bus.publish()` on: wake, transcript, tool start, tool result, state change, error | Layer 1 needs producers; the bus has none. |
| `memory/memory_manager.py:182` | one guard in `update_memory` | Single choke point covering every path into durable memory **and** the vector mirror (`remember` and `_mirror_to_ruflo` both route through it). Closes §11 item 4. |
| `services/web_intelligence/tool.py:86-102` | fence in `_fmt_document` / `_fmt_results` | Closes injection flows for `read_url`, search, RSS, GitHub, YouTube at once. |
| `actions/browser_control.py:674` | scan + fence page text | Bypasses `web_intelligence`; needs its own guard. |
| `actions/computer_settings.py:634` | ignore the LLM's `confirmed` arg; require a kernel-issued token | Reuses `remote_control/confirmation.py`. |
| `main.py:1108` (`change_voice`) | require confirmation to change persona | Closes §11 item 5. |
| `main.py:106` | do not read `persona` from `os.environ` | Closes §11 item 6. |
| `ui.py:2247` | `self._pages.addWidget(self._build_neural_core_page())  # 7` | One line; existing pages 0-6 unchanged. |
| `ui.py:3786`, `main.py:1399` | correct the wake-word text | It currently tells the user a false thing. |
| `core/wake_word.py:24` | accept a model name; load `hey_jarvis` **and** a FRIDAY model when present | Only if a FRIDAY wake model is obtained; otherwise leave and fix the message. |
| `friday/state_controller.py:11` | reconcile with `friday/bridge.py:20` | Two maps disagree on `MUTED`. |
| `requirements.txt` | pin the new deps; strip the BOM | |
| `.gitignore` | add `aoca/**/*.db`, `aoca/snapshots/` | |
| `Jarvis.spec` | add `aoca` to `hiddenimports` | PyInstaller will not find a lazily-imported package. |

**Not modified:** `ui.py` layout, all 7 existing pages, `friday/*` visuals,
`SpeechCore/*`, `launcher/*`, `remote_control/*` (reused, not changed),
`core/ai.py` routing, every existing tool implementation.

---

## 15. Phased implementation plan

Each phase ends with green `unittest` and a working app. No phase begins before
the previous one's safety tests pass.

| Phase | Deliverable | Gate to proceed |
|---|---|---|
| **0** | This report | Accepted |
| **1** | Layer 0 kernel + Layer 1 events + gate wired into both dispatch chains | Forbidden action blocked in tests; app behaves identically for permitted actions; TEST 10 passes |
| **2** | `aoca/db` with FKs, WAL, migrations, integrity check, backup | Restart-survival test; corrupt-DB recovery test |
| **3** | **Layer 9 verification first** — predicates + `R_t`, plus free-VRAM telemetry | `open_app` reports failure when the app did not open. This is the phase that makes everything after it meaningful. |
| **4** | Layer 2 EKF over telemetry | Filter stays bounded over a 24 h replay; no NaN, no covariance collapse |
| **5** | Layer 4 memory hierarchy, value, decay, scoping | TEST 3 (persistence). The RAM-note duplication in `long_term.json` stops recurring. |
| **6** | Layer 5 graph + spreading activation + Hebbian | TEST 4 (associative recall); node/edge caps enforced under load |
| **7** | Layer 6 router + bandit, bootstrapped from `ai_metrics.jsonl` | TEST 5 (bounded growth), TEST 6 (failure adaptation, no permanent ban), TEST 10 again |
| **8** | Layer 11 uncertainty + contradiction resolution | TEST 7 (ambiguous command → clarification, no action) |
| **9** | Layer 8 planner, CVaR, receding horizon | TEST 9 (replan on observation) |
| **10** | Layer 12 Neural Core UI page from real telemetry | Idle overhead ≤200 MB and ≤2 % CPU; nothing animates when nothing is active |
| **11** | Torch tier: test-time memory, graph attention, world model | TEST 8 (predicted vs actual RAM/latency); TEST 11 (no regression on protected tasks); promotion pipeline with rollback |
| **12** | Consolidation, archival, budget, snapshot/restore | Idle-only execution; pauses instantly on user input |
| **13** | Hardening: injection, longevity, RAM, regression, identity | Full suite green; 72 h soak |

**Budgets, all in `aoca/config.py`, none hardcoded:** always-active extra RAM
≤300 MB, visualizer ≤200 MB, active nodes ≤128, active edges ≤512, planning
horizon ≤8, candidate plans ≤16, one heavy local model, min free RAM 2 GB
(preferred 3 GB), min free VRAM 0.5 GB.

---

## 16. Rollback strategy

**Per-phase.** Each phase is one branch off `hermes-nexus-integration`, one
merge commit, and the modification list in §14 is small enough that
`git revert <merge>` restores the previous behaviour exactly. No phase deletes
or rewrites existing code, so a revert can never lose assistant functionality.

**Kill switch.** `aoca/__init__.py` exposes `AOCA_ENABLED` (env var +
`config/api_keys.json`). When false, every insertion point in §14 short-circuits
to the original code path on the first line. The gate call becomes
`return None`; the bus publishes are no-ops. **The app must run correctly with
the entire package disabled** — that is a Phase 1 test, not an afterthought.

**Data.** DB writes are transactional. Daily incremental backup, weekly verified
snapshot, schema migrations paired with a down-migration, and `PRAGMA
integrity_check` before every snapshot is marked good. `memory/long_term.json`
keeps its existing atomic-replace write and is backed up before any AOCA
migration touches it.

**Models.** Three slots — CURRENT, CANDIDATE, ROLLBACK. A candidate is trained on
a copy, evaluated on new samples **and** protected regression tasks **and**
safety-policy tests **and** JARVIS/FRIDAY identity tests, compared on latency and
RAM, and promoted only when every threshold passes with a minimum sample count.
The previous model is retained. Any Lyapunov violation triggers automatic
rollback and pauses learning.

**Manual controls.** "Pause all learning", "restore snapshot", "reset router
learning", "reset one tool's statistics", and "run integrity check" are Phase 13
deliverables, but **"pause all learning" ships in Phase 1** alongside the kernel
— before there is anything to pause. It is the operator's brake, and it must
predate the engine.

---

## Recommendation

Build Phases 1-3 next, in that order, and stop there for review.

Phase 3 is the one that changes the system's honesty: today
`actions/open_app.py:246` returns `True` after a `sleep(1.5)`, and `main.py:982`
manufactures a success string when the tool returned nothing. Until an action's
outcome is measured, every equation in this document is being trained on the
proposition that nothing ever fails.
