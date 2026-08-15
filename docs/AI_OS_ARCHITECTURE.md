# JARVIS AI Operating System — Architecture and Delivery Plan

## Current-state review

JARVIS is a single-process Python/PyQt desktop application. Existing capabilities must remain intact:

- `main.py` owns live voice interaction, Gemini function calls, memory injection, and automation dispatch.
- `ui.py` owns the desktop shell, chat log, file drop-zone, AI Control Center, and an operational Image Studio page.
- `core/ai.py` is the reusable OmniRoute adapter. It already streams SSE, supports a manual model override, retries a fallback chain, records metrics, and exposes routing status.
- `memory/` provides durable memory and optional semantic recall; `agent/` and `actions/` provide task execution and automation.

The codebase has no formal event bus, studio registry, stable plugin contract, or shared multi-file input model. Phase 0 adds those seams without changing any existing call path.

## Target module boundary

```text
PyQt desktop shell / Universal Input
             |                    \
      Studio Intent Router       AI Control Center
             |                    |
        Studio Registry ------> OmniRoute adapter
             |                    |
      Studio plugin <---- Event Bus ----> Memory / Agent / Automation
```

Each studio implements `StudioPlugin` and receives a `StudioRequest`; it returns `StudioResult` and publishes lifecycle events such as `studio.started`, `studio.progress`, and `studio.completed`. Studios own provider-specific requests and artifact history. Core owns routing policy, workspace identity, event delivery, and user-visible routing telemetry.

The current Image Studio will be moved behind this interface in its delivery phase; no existing UI or provider call is removed beforehand.

## Phased implementation plan

| Phase | Deliverable | Acceptance checks | Estimate |
| --- | --- | --- | --- |
| 0 | Contracts, event bus, universal-input normalization, route classifier, architecture docs | Unit tests; existing startup/import smoke test | 2–3 days |
| 1 | Chat Studio: persisted conversations, streaming transcript, folders/pins/search/export, attachments, project context | Implemented baseline; service tests and offscreen PyQt smoke test pass | 2–3 weeks |
| 2 | Image Studio plugin migration: history/favorites/library, model capability adapters, text-to-image | Provider contract tests; image save/load; existing image regression | 2–3 weeks |
| 3 | Document Studio with safe local parsing and PDF/Office exports | Fixture tests; large-file memory/performance checks | 3–4 weeks |
| 4 | Code Studio: repository context/index, terminal execution controls, git/VSC integration | Sandbox tests; repository-scale benchmark; automation regression | 4–6 weeks |
| 5 | Research Studio: sources, citations, reports, multi-document reasoning | Citation integrity tests; source/browser failure tests | 3–4 weeks |
| 6 | Automation Studio plugin migration: scheduled workflows and plugin API | Permission/audit tests; existing action regression | 3–4 weeks |
| 7 | Video, Music, and Voice Studios, each as provider adapters | Provider mocks, artifact history, cancellation/retry | 3–5 weeks each |

Estimates assume one experienced engineer and configured providers. Parallel work is possible only after Phase 1 stabilizes the shared workspace and permissions model.

## Provider and routing policy

OmniRoute remains the conversational/model router. Studio routing chooses *which capability* should receive a request; it does not replace OmniRoute’s model choice. A request can carry a manual studio or model override. Every provider adapter declares capabilities, health, cost/latency hints, cancellation behavior, and fallback eligibility. The AI Control Center reports selected studio, provider/model, latency, stream state, reason, fallback chain, and manual/automatic status—never system prompts.

## Risks and controls

- **Provider differences:** use capability-based adapters and normalize only the common request/result contract.
- **Large or untrusted files:** validate input sizes/types locally, parse in bounded workers, and require explicit user action before uploads or automation.
- **UI responsiveness:** all provider I/O and file parsing stay off the PyQt UI thread; stream updates are batched.
- **Existing voice coupling:** retain the current live-voice path and bridge it to studios with events incrementally, after regression tests.
- **Data/privacy:** keep artifacts and histories workspace-scoped; make cloud uploads, retention, and voice-cloning consent explicit.

## Phase 0 deliverables

- `core/events.py`: resilient in-process event bus.
- `studios/`: plugin contracts, thread-safe registry, and transparent intent classifier.
- `core/universal_input.py`: shared, validated file/folder attachment normalization.
- `tests/test_platform_foundation.py`: contract tests including each natural-language routing example in the brief.

No existing feature is removed or rerouted in Phase 0.

## Phase 1 delivery status

Chat Studio is now the first registered Studio plugin. Its service, UI adapter, and boundaries are documented in `docs/CHAT_STUDIO.md`. The existing live-voice conversation path is intentionally unchanged; this avoids a risky migration while the two modes share memory and the AI Control Center.
