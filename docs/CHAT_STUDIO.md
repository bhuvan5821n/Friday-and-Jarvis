# Chat Studio

Chat Studio is JARVIS's first AI Studio plugin. It preserves the existing live-voice conversation path while providing a separate persistent text workspace backed by OmniRoute.

## Included

- SSE streaming through the existing `core.ai.OmniModel` adapter.
- Durable local conversations in `Studios/Chat/conversations.json` (created on first use).
- Multiple chats, automatic titles, pinning, folders, search, and Markdown export.
- Markdown transcript rendering and code-fence support in the desktop UI.
- File attachments validated by the shared universal-input layer; image files are sent as vision inputs, while other attachment metadata is retained with the message for their dedicated studio/parser phase.
- Memory and project identity are included in each Chat Studio system context when available.
- `studio.started`, `studio.completed`, and `studio.failed` events through `core.events.bus`.

## Deliberate boundaries

Voice stays on the existing Gemini Live session, including wake-word and automation/tool calls. Chat Studio does not replace it. Rich parsing for PDFs, Office files, archives, and folders is scheduled with Document and Code Studios; the current chat attachment flow preserves those files and makes their type visible without uploading or parsing them implicitly.
