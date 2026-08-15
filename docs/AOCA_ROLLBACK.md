# AOCA Phases 1-3 — Rollback

## Checkpoint

| | |
|---|---|
| Starting commit | `d99055eeae15896d48ca0551b7640506a4fde471` |
| Starting branch | `hermes-nexus-integration` |
| Working branch | `aoca-foundation-phases-1-3` |
| Created | 2026-08-05 |

Working tree at checkout time: clean except one untracked file,
`docs/AOCA_PHASE0_AUDIT.md` (the Phase 0 report). No unrelated user changes
existed, and none were staged or committed.

## Files created

All new code lives under `aoca/`. Nothing outside it was created except this
document and the deliverables report.

```
aoca/__init__.py            aoca/config.py         aoca/tools.py
aoca/safety.py              aoca/trace.py          aoca/events.py
aoca/verify.py              aoca/outcomes.py
aoca/tests/*.py
docs/AOCA_PHASES_1_3_REPORT.md
docs/AOCA_ROLLBACK.md
```

## Files modified

| File | Change |
|---|---|
| `agent/executor.py` | Removed `_run_generated_code`. Registry-driven dispatch; unknown tool raises `ToolNotRegistered`. Step tool no longer defaults to `generated_code`. |
| `agent/planner.py` | `generated_code` rewrite target list unchanged; comment corrected. |
| `actions/desktop.py` | `ctypes` removed from the generated-code sandbox (namespace escape). |
| `actions/open_app.py` | Launch and verification separated; no fixed-sleep success. |
| `main.py` | Policy gate in `_execute_tool`; event publishing; verified `open_app` result; `persona` no longer read from `os.environ`. |
| `.gitignore` | `aoca/**/*.db*`, `aoca/snapshots/` |
| `Jarvis.spec` | `aoca` added to `hiddenimports` |

## How to disable AOCA

Set in `config/api_keys.json` (or as an environment variable, which wins):

```json
{ "AOCA_ENABLED": false }
```

Granular flags, all default true when `AOCA_ENABLED` is true:

```
AOCA_EVENTS_ENABLED
AOCA_VERIFICATION_ENABLED
AOCA_OUTCOME_STORAGE_ENABLED
```

Learning flags exist and are **hard-wired false** in this phase:
`AOCA_LEARNING_ENABLED`, `AOCA_WORLD_MODEL_ENABLED`, `AOCA_PLANNER_ENABLED`,
`AOCA_NEURAL_CORE_ENABLED`.

With `AOCA_ENABLED=false` the assistant runs its original paths: no policy
gate, no events, no verification, no outcome storage.

**Two things stay fixed regardless of the flag**, because they are security
defects rather than features:

1. An unknown tool name can never execute generated Python.
2. `open_app` never returns an invented success sentence.

## How to revert only these changes

```bash
git checkout hermes-nexus-integration      # abandon the branch entirely
```

Or, if the branch was merged:

```bash
git revert -m 1 <merge-sha>
```

Every change is additive or a single-line insertion, so a revert restores
prior behaviour exactly — except the two security fixes above, which should be
re-applied by hand if the branch is reverted:

- `agent/executor.py` — delete `_run_generated_code`, make the unknown-tool
  branch raise instead of falling back.
- `actions/open_app.py:246` — do not `return True` after `time.sleep(1.5)`.

## Data

`aoca/outcomes.db` is created on first verified action. It is gitignored,
contains no secrets or private content, and can be deleted at any time — the
schema is rebuilt on next start. No existing database, memory file, or config
is migrated, rewritten, or read-modify-written by these phases.
