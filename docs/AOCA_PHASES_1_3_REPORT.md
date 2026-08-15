# AOCA Phases 1-3 Deliverables Report

Branch: `aoca-foundation-phases-1-3`  
Phase 1 commit: `f7e4ed8`  
Date: 2026-08-06

---

## What was built

Phases 1-3 of the ASTRAEUS OMEGA COGNITIVE ARCHITECTURE — safety hardening,
real event infrastructure, and deterministic execution with outcome verification
— layered onto the live JARVIS/FRIDAY codebase without redesigning, replacing,
or simplifying any existing system.

Everything is behind `AOCA_ENABLED`. When the flag is false, existing behaviour
is preserved **except** the two safety fixes that are unconditional (see §2).

---

## 22 Deliverables

### Phase 1 — Safety Kernel

**1. Arbitrary-code execution removed from `agent/executor.py`.**  
The three paths that converted an unknown tool name into a subprocess call
were deleted: `_run_generated_code` (~80 lines), the explicit `generated_code`
branch, and the `step.get("tool", "generated_code")` default. Replaced with
`_call_tool` → `registry.require()` → raises `ToolNotRegistered`. Unknown tool
names now produce a user-facing suggestion and log line; nothing runs.

**2. Second RCE removed from `actions/desktop.py`.**  
`_build_sandbox`, `_execute_generated_code`, and `_ask_gemini_for_desktop_action`
(~120 lines, including a `ctypes`-injecting "sandbox" that was a complete
namespace escape) were deleted. Both call sites now call `_unsupported_task`,
which lists what the desktop tool can actually do.

**3. Deterministic `ToolRegistry` (`aoca/tools.py`).**  
Canonical names + exact aliases. `resolve()` returns `None` on miss or disabled
tool; `require()` raises `ToolNotRegistered` with a suggestion. `suggest()` uses
`difflib` at cutoff 0.75 — advisory only, explicitly never wired to execution.
`normalize()` strips shell metacharacters before any lookup: `; whoami`,
`os.system('x')`, `../../etc/passwd` all return `""`. 16 builtins registered;
`generated_code` is absent. Verified: planner and registry advertise the same 16
names.

**4. Shared safety kernel (`aoca/safety.py`).**  
Deny-by-default. Decision order: UNTRUSTED_CONTENT → deny; unattributed origin
→ deny; `registry.require()` miss → deny; FORBIDDEN → deny; CREATOR_ONLY +
non-local → deny; risk above origin ceiling → deny; invalid params → deny; else
allow (with `confirmation_required` when CONFIRM or CRITICAL). Any exception
from the policy service → deny `policy_error` (fail closed, never optimistic).
No score or weight anywhere in the kernel — nothing a learning layer can move.

**5. Mandatory security test: injected payloads.**  
`InjectedPayloadsCannotExecute` (5 tests) verifies 10 payloads including
`"os.system('whoami')"`, `"__import__('os').system('whoami')"`,
`"generated_code"`, `"eval"`, and shell-metacharacter names. Each payload is
tested both as a planned tool name (`registry.require` / `_call_tool`) and as
text arriving from a webpage (`Origin.UNTRUSTED_CONTENT`). Neither path runs
anything.

**6. Repo-wide dynamic-execution audit.**  
`NoDynamicExecutionInProjectCode` (3 AST-based tests) walks every tracked `.py`
file. Uses `isinstance(node.func, ast.Name)` — not `ast.Attribute` — to avoid
the 47 false positives from `re.compile` and Qt's `app.exec()`. One `__import__`
and four `shell=True` sites are allowlisted with named reasons.

---

### Phase 2 — Event and Trace Infrastructure

**7. Trace context (`aoca/trace.py`).**  
`contextvars`-based, not a mutable global. `TraceContext` is frozen: `trace_id`,
`assistant`, `origin`, `stage`, `span_id`, `parent_span`, `deadline`, `cancel`.
`trace()` and `stage()` context managers; `bind()` wraps a callable with a
context snapshot for explicit handoff to threads/pools. Unbound threads land on
`"untraced"` — visibly orphaned, not silently attributed to another request.

**8. Privacy filter (`aoca/privacy.py`).**  
Allowlist-first (≈50 names). Unknown field → dropped, no pattern needed.
`DENIED_FIELDS` (≈40 names) is a redundant second gate that survives any future
allowlist widening. String redaction reuses `services.web_intelligence.security.
redact_secrets` plus 4 supplementary patterns (hex tokens, JWTs, OTP/PIN/CVV,
PEM private keys). `sanitize()` never raises. `ALLOWED_FIELDS ∩ DENIED_FIELDS = ∅`
enforced by test.

**9. Non-blocking event bus (`aoca/events.py`).**  
Bounded `queue.Queue` (default 512), one daemon dispatcher thread `aoca-events`.
`publish()` never blocks and never raises. Under overload: TELEMETRY is dropped,
CONSEQUENCE (policy decisions, failures, outcomes) is preserved via `_evict_one`.
`CognitiveEvent.create()` sanitizes the payload at construction so no content
can exist in memory. `EventState` machine with 11 states; illegal transitions
are logged and counted, not silently swallowed.

**10. Pipeline instrumentation.**  
Five real call sites instrumented:
- `main.py` `_execute_tool` → `request.received`, `policy.refused`,
  `request.routed`, `action.executing`, `action.executed`/`action.failed`
- `agent/executor.py` `_call_tool` → `action.executing`, `action.executed`,
  `action.failed`
- `services/web_intelligence/tool.py` → `web.started`, `web.finished`
- `actions/browser_control.py` → `browser.started`, `browser.finished`
- `memory/memory_manager.py` `update_memory` → `memory.written` (count only,
  no content)

**11. Trace survives the `run_in_executor` boundary.**  
`_TracedLoop` shim in `main.py` wraps the asyncio event loop so every
`run_in_executor` call binds the current trace context before crossing into the
worker thread. 40 call sites covered by one 12-line class instead of 40 edits.
Confirmed by `test_traced_loop_wrapper_carries_context` and
`test_unwrapped_executor_would_have_lost_it`.

---

### Phase 3 — Execution and Outcome Verification

**12. Execution/verification contracts (`aoca/verify.py`).**  
`ExecutionResult` says what the executor did. `VerificationResult` says what was
observed afterwards. `FinalActionOutcome.combine()` is the only place an outcome
is decided — in order: not started → FAILED; not completed → UNVERIFIED; early
exit → STARTED_THEN_EXITED; not observed → FAILED; else SUCCEEDED. UNVERIFIED ≠
SUCCEEDED. `FinalActionOutcome.learnable` returns True only for SUCCEEDED,
FAILED, STARTED_THEN_EXITED — never UNVERIFIED.

**13. Process identity by `(pid, create_time)` key.**  
`ProcessIdentity.key = (pid, int(create_time * 1000))`. `alive()` re-checks
`create_time`, not just pid. A reused Windows pid cannot impersonate the
original process.

**14. Verifier registry.**  
`register_verifier` / `verify` / `has_verifier`. `verify()` on an unknown name
or disabled flag returns `VerificationResult.unavailable()` — not a success.
No verifier returns True unconditionally; asserted by
`test_no_registered_verifier_returns_true_unconditionally`.

**15. `application_open` verifier.**  
Bounded polling (`VERIFY_POLL_SECONDS = 0.25`, `APP_LAUNCH_TIMEOUT_SECONDS = 15`),
not a fixed sleep. Distinguishes: new process started (SUCCEEDED); already
running before launch (`already_running` evidence, reported separately); started
then exited (`early_exit` evidence, STARTED_THEN_EXITED); nothing appeared
(FAILED). Process identified by `(pid, create_time)` throughout.

**16. `process_stopped` and `file_operation` verifiers.**  
Bounded polling for process exit. File presence/absence check for file
operations.

**17. `open_app` false-success path closed.**  
The `return f"Opened {app_name}."` sentence is gone. `open_app()` now:
snapshots processes before launch, unpacks `(started, method)`, calls
`verify("application_open", ...)`, and returns `combine(...).message` — one of
four honest sentences. The `already_running` case returns its own sentence.
Confirmed by `test_open_app_still_refuses_to_invent_success` which holds even
when `AOCA_ENABLED=False`.

**18. Outcome store (`aoca/outcomes.py`).**  
SQLite + WAL. Schema version via `PRAGMA user_version`; migrations are
append-only SQL tuples. Three indices: `trace_id`, `(tool, outcome)`,
`(learnable, occurred_at)`. Columns are numbers, booleans, and enum values —
no free text. `learnable` is written at insert time from
`FinalActionOutcome.learnable`. `_prune()` keeps the table below
`OUTCOME_ROWS_MAX = 50_000`. `record()` never raises.

**19. Mandatory reality tests.**  
`RealityTests` (5 psutil-gated tests) use actual subprocesses:
- Nonexistent app → "isn't running" (never "Opened")
- A real long-lived python process → SUCCEEDED, learnable
- A process that exits immediately → not SUCCEEDED, no "Opened"
- `process_stopped` on a killed process → expected_state_observed
- Verifier completes within its declared timeout

**20. Flag-off guarantees.**  
`UnsafeFallbackIsOffRegardlessOfTheFlag` (3 tests): unknown tool still raises
`ToolNotRegistered`; `open_app` still refuses to invent success; outcome store
writes nothing — all with `AOCA_ENABLED=False`.

---

### Cross-cutting

**21. `Jarvis.spec` updated.**  
`hiddenimports` extended with all 8 `aoca.*` modules so a PyInstaller build
does not silently fall back to unprotected code paths when the AOCA package is
not found.

**22. Test counts and regression baseline.**

| Suite | Tests | Result |
|-------|-------|--------|
| `aoca/tests/test_phase1_safety.py` | 27 | OK |
| `aoca/tests/test_phase2_events.py` | 41 | OK |
| `aoca/tests/test_phase3_verification.py` | 30 | OK |
| `aoca/tests/test_integration.py` | 12 | OK |
| `tests/` (pre-existing) | 13 | OK |
| `services/web_intelligence/tests/` (pre-existing) | 56 | OK |
| `remote_control/tests/` (pre-existing) | 228 | 1 pre-existing error (yaml not installed in out-of-repo hermes path), no regressions |
| **Total AOCA** | **110** | **OK** |

---

## Resource impact

| Metric | Value |
|--------|-------|
| Import footprint (all 8 modules) | 5.5 MB |
| Publish throughput (flag on) | ~14 000 calls/s (0.07 ms each) |
| Disabled emit (flag off) | ~370 000 calls/s (0.003 ms each) |
| RAM reserve enforced | 2 GB min (`AOCA_RAM_RESERVE_GB`) |

The disabled path is 27× faster than the enabled one, so the overhead is paid
only when the flag is on.

---

## What is NOT in Phases 1-3

Per the mandate, the following are explicitly absent and will not be added
until every mandatory safety test passes for their respective phases:

- Cognitive graph, Hebbian learning, contextual bandit
- World model, planner, Neural Core animation
- Continual learning (EWC, replay, distillation)
- Neural Core visualization, fake neural activity
- JARVIS/FRIDAY personality changes
- UI redesign

---

## Files created or modified

| File | Action |
|------|--------|
| `aoca/__init__.py` | created |
| `aoca/config.py` | created |
| `aoca/tools.py` | created |
| `aoca/safety.py` | created |
| `aoca/trace.py` | created |
| `aoca/privacy.py` | created |
| `aoca/events.py` | created |
| `aoca/verify.py` | created |
| `aoca/outcomes.py` | created |
| `aoca/tests/test_phase1_safety.py` | created |
| `aoca/tests/test_phase2_events.py` | created |
| `aoca/tests/test_phase3_verification.py` | created |
| `aoca/tests/test_integration.py` | created |
| `agent/executor.py` | modified — RCE removed, registry dispatch, instrumentation |
| `actions/desktop.py` | modified — second RCE removed |
| `actions/open_app.py` | modified — false-success path closed, verification wired |
| `actions/browser_control.py` | modified — instrumented |
| `services/web_intelligence/tool.py` | modified — instrumented |
| `memory/memory_manager.py` | modified — instrumented |
| `main.py` | modified — trace wiring, `_TracedLoop`, instrumentation |
| `Jarvis.spec` | modified — `aoca.*` hiddenimports |
| `.gitignore` | modified — `aoca/*.db*` |
| `docs/AOCA_ROLLBACK.md` | created |
| `docs/AOCA_PHASE0_AUDIT.md` | pre-existing |
