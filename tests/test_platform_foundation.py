import tempfile
import unittest
from pathlib import Path

from core.events import Event, EventBus
from core.universal_input import classify_path, normalize_paths
from Studios.contracts import StudioManifest, StudioRequest, StudioResult
from Studios.registry import StudioRegistry
from Studios.router import StudioIntentRouter
from Studios.chat import ChatStudio
from core.attachments import AttachmentPipeline


class _Plugin:
    manifest = StudioManifest("sample", "Sample", "1.0.0", ("test",))

    def handle(self, request):
        return StudioResult("completed", request.prompt)


class PlatformFoundationTests(unittest.TestCase):
    def test_event_bus_isolates_unhealthy_subscribers(self):
        bus, received = EventBus(), []
        bus.subscribe("studio.started", lambda event: received.append(event.payload["id"]))
        bus.subscribe("studio.started", lambda event: (_ for _ in ()).throw(RuntimeError("bad plugin")))
        failures = bus.publish(Event("studio.started", {"id": "image"}, "test"))
        self.assertEqual(received, ["image"])
        self.assertEqual(len(failures), 1)

    def test_registry_rejects_duplicate_plugins(self):
        registry = StudioRegistry()
        registry.register(_Plugin())
        self.assertEqual(registry.get("sample").manifest.name, "Sample")
        with self.assertRaises(ValueError):
            registry.register(_Plugin())

    def test_requested_examples_route_to_expected_studios(self):
        router = StudioIntentRouter()
        cases = {
            "Create a logo for my coffee shop": "image",
            "Generate a cinematic trailer": "video",
            "Compose relaxing piano music": "music",
            "Summarize this PDF": "document",
            "Build a React website": "code",
        }
        for prompt, expected in cases.items():
            self.assertEqual(router.route(StudioRequest(prompt=prompt)).studio_id, expected)

    def test_universal_input_handles_files_and_folders(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            code = root / "demo.py"; code.write_text("print('ok')")
            self.assertEqual(classify_path(code), "code")
            attachments = normalize_paths([code, root])
            self.assertEqual([item.kind for item in attachments], ["code", "folder"])

    def test_chat_persists_streamed_conversation_and_exports_it(self):
        class FakeChat(ChatStudio):
            def _generate(self, request, conversation, on_delta):
                on_delta("Hello")
                on_delta(" world")
                return "Hello world"

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            chat = FakeChat(root / "conversations.json")
            chunks = []
            result = chat.stream_response(StudioRequest(prompt="Hi"), chunks.append)
            self.assertEqual(result.status, "completed")
            self.assertEqual("".join(chunks), "Hello world")
            self.assertEqual(len(chat.active()["messages"]), 2)
            exported = chat.export_markdown(chat.active_id)
            self.assertIn("Hello world", exported.read_text(encoding="utf-8"))
            self.assertEqual(len(FakeChat(root / "conversations.json").conversations()), 1)

    def test_pipeline_sniffs_content_and_keeps_image_context_for_follow_up(self):
        class FakeChat(ChatStudio):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs); self.requests = []
            def _generate(self, request, conversation, on_delta):
                self.requests.append(self._request_parts(conversation, request.prompt))
                return "ready"

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            image = root / "not-an-image.dat"
            from PIL import Image
            Image.new("RGB", (12, 8), "red").save(image, format="PNG")
            source = root / "mystery.bin"; source.write_text("def hello():\n    return 'jarvis'\n")
            archive = root / "upload.bin"
            import zipfile
            with zipfile.ZipFile(archive, "w") as zf: zf.writestr("notes.txt", "hello")
            attachments = normalize_paths([image, source, archive])
            self.assertEqual([item.kind for item in attachments], ["image", "code", "archive"])
            contexts = AttachmentPipeline().process(attachments)
            self.assertEqual([item.status for item in contexts], ["ready", "ready", "ready"])
            self.assertEqual(contexts[0].metadata["width"], 12)
            chat = FakeChat(root / "conversations.json")
            chat.stream_response(StudioRequest(prompt="What is this?", attachments=[attachments[0]]))
            chat.stream_response(StudioRequest(prompt="What color is it?"))
            self.assertEqual(len(chat.requests[1]), 2)  # text plus original image bytes
            self.assertIn("ATTACHMENT 1", chat.requests[1][0])


if __name__ == "__main__":
    unittest.main()
