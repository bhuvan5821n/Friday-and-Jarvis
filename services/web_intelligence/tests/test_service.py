"""Service + model tests — task state honesty, ranking, cancellation,
event redaction, research composition. Offline (providers are mocked).
"""
import unittest
from unittest import mock

from services.web_intelligence.models import (ResearchAnswer, SearchResult,
                                              WebDocument, WebQuery)
from services.web_intelligence.providers import ProviderError
from services.web_intelligence.service import WebIntelligenceService


def _svc(events=None):
    """Service with Agent Reach absent — must still work for pure-HTTP paths."""
    with mock.patch("services.web_intelligence.service.locate",
                    return_value=None):
        return WebIntelligenceService(
            on_event=(lambda n, d: events.append((n, d)))
            if events is not None else None)


class TestModels(unittest.TestCase):
    def test_document_hash_is_stable(self):
        a = WebDocument(canonical_url="u", title="t", cleaned_text="body")
        b = WebDocument(canonical_url="u2", title="t2", cleaned_text="body")
        self.assertEqual(a.content_hash, b.content_hash)
        self.assertEqual(len(a.content_hash), 16)

    def test_research_answer_serializes(self):
        d = ResearchAnswer(answer="x", confidence="low").to_dict()
        self.assertEqual(d["confidence"], "low")

    def test_query_defaults(self):
        q = WebQuery(query="x")
        self.assertEqual(q.requested_action, "quick_search")
        self.assertEqual(q.user_confirmation_state, "none")


class TestRanking(unittest.TestCase):
    def test_official_sources_rank_first(self):
        svc = _svc()
        blog = SearchResult(title="b", url="https://someblog.example/post")
        official = SearchResult(title="o", url="https://docs.python.org/3/")
        self.assertGreater(svc._rank(official), svc._rank(blog))


class TestQuickSearch(unittest.TestCase):
    def test_success_emits_and_updates_state(self):
        events = []
        svc = _svc(events)
        fake = [SearchResult(title="t", url="https://python.org")]
        with mock.patch("services.web_intelligence.service.providers"
                        ".search_web", return_value=fake):
            out = svc.quick_search(WebQuery(query="python"))
        self.assertEqual(len(out), 1)
        snap = svc.state.snapshot()
        self.assertEqual(snap["phase"], "done")
        self.assertEqual(snap["sources_found"], 1)
        names = [n for n, _ in events]
        self.assertIn("SEARCH_STARTED", names)
        self.assertIn("TASK_COMPLETED", names)

    def test_failure_is_reported_not_swallowed(self):
        events = []
        svc = _svc(events)
        with mock.patch("services.web_intelligence.service.providers"
                        ".search_web", side_effect=ProviderError("rate limit")):
            with self.assertRaises(ProviderError):
                svc.quick_search(WebQuery(query="x"))
        self.assertEqual(svc.state.snapshot()["phase"], "failed")
        self.assertIn("TASK_FAILED", [n for n, _ in events])

    def test_event_details_are_redacted(self):
        events = []
        svc = _svc(events)
        svc._emit("TASK_FAILED", "leaked ghp_" + "A1b2C3d4" * 5)
        self.assertNotIn("ghp_", events[0][1])

    def test_unknown_event_names_are_dropped(self):
        events = []
        svc = _svc(events)
        svc._emit("MADE_UP_EVENT", "x")
        self.assertEqual(events, [])


class TestReadUrlRouting(unittest.TestCase):
    def test_github_urls_route_to_api(self):
        svc = _svc()
        doc = WebDocument(canonical_url="u", title="t", cleaned_text="x",
                          source_type="github")
        with mock.patch("services.web_intelligence.service.providers"
                        ".read_github", return_value=doc) as gh:
            svc.read_url("https://github.com/a/b")
            gh.assert_called_once()

    def test_youtube_without_install_fails_honestly(self):
        svc = _svc()  # locate() -> None, so yt-dlp is unavailable
        with self.assertRaises(ProviderError):
            svc.read_url("https://youtube.com/watch?v=x")
        self.assertIn("yt-dlp", svc.state.snapshot()["last_error"])

    def test_injection_flag_emits_event(self):
        events = []
        svc = _svc(events)
        doc = WebDocument(canonical_url="u", title="t", cleaned_text="x")
        doc.injection_flags = ["asks the agent to ignore its instructions"]
        with mock.patch("services.web_intelligence.service.providers"
                        ".read_webpage", return_value=doc):
            svc.read_url("https://example.com")
        self.assertIn("INJECTION_DETECTED", [n for n, _ in events])


class TestDeepResearch(unittest.TestCase):
    def _doc(self, url, flags=()):
        d = WebDocument(canonical_url=url, title=url, cleaned_text="content " * 50)
        d.injection_flags = list(flags)
        return d

    def test_citations_fencing_and_confidence(self):
        svc = _svc()
        results = [SearchResult(title=f"t{i}", url=f"https://s{i}.example/p")
                   for i in range(4)]
        with mock.patch("services.web_intelligence.service.providers"
                        ".search_web", return_value=results), \
             mock.patch("services.web_intelligence.service.providers"
                        ".read_webpage",
                        side_effect=lambda u, *a, **k: self._doc(u)):
            answer = svc.deep_research(WebQuery(query="q", research_depth=1))
        self.assertGreaterEqual(answer.sources_checked, 3)
        self.assertEqual(len(answer.citations), answer.sources_checked)
        self.assertIn("[UNTRUSTED WEB CONTENT", answer.answer)
        self.assertIn(answer.confidence, ("low", "medium"))  # never "high"
        self.assertTrue(answer.limitations)  # always states limits

    def test_injection_becomes_a_limitation(self):
        svc = _svc()
        results = [SearchResult(title="t", url="https://bad.example/p")]
        with mock.patch("services.web_intelligence.service.providers"
                        ".search_web", return_value=results), \
             mock.patch("services.web_intelligence.service.providers"
                        ".read_webpage",
                        return_value=self._doc("https://bad.example/p",
                                               ["injection"])):
            answer = svc.deep_research(WebQuery(query="q"))
        self.assertTrue(any("untrusted" in l for l in answer.limitations))

    def test_zero_readable_sources_raises(self):
        svc = _svc()
        results = [SearchResult(title="t", url="https://a.example")]
        with mock.patch("services.web_intelligence.service.providers"
                        ".search_web", return_value=results), \
             mock.patch("services.web_intelligence.service.providers"
                        ".read_webpage", side_effect=ProviderError("nope")):
            with self.assertRaises(ProviderError):
                svc.deep_research(WebQuery(query="q"))


class TestLifecycle(unittest.TestCase):
    def test_provider_status_honest_when_missing(self):
        svc = _svc()
        statuses = svc.provider_status()
        self.assertEqual(len(statuses), 1)
        self.assertFalse(statuses[0].available)
        self.assertIn("not installed", statuses[0].detail)

    def test_cancel_interrupts_after_search(self):
        svc = _svc()

        def search_then_cancel(*a, **k):
            svc._cancel.set()
            return [SearchResult(title="t", url="https://a.example")]
        with mock.patch("services.web_intelligence.service.providers"
                        ".search_web", side_effect=search_then_cancel):
            with self.assertRaises(InterruptedError):
                svc.quick_search(WebQuery(query="x"))

    def test_shutdown_reports_zero_children(self):
        svc = _svc()
        report = svc.shutdown()
        self.assertEqual(report["live_children"], 0)
        self.assertEqual(svc.state.snapshot()["phase"], "idle")


if __name__ == "__main__":
    unittest.main()
