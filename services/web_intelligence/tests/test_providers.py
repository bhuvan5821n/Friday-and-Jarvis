"""Provider parsing tests — canned HTML/JSON, no network access.

Network entry points (_http_get) are monkeypatched so these run offline
and deterministically.
"""
import json
import unittest
from unittest import mock

import services.web_intelligence.providers as providers
from services.web_intelligence.providers import ProviderError, _validate_url


DDG_LITE_HTML = """
<html><body><table>
<tr><td><a class="result-link"
  href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.python.org%2F&amp;rut=x">
  Welcome to Python.org</a></td></tr>
<tr><td class="result-snippet">The official home of the Python language.</td></tr>
<tr><td><a class="result-link" href="https://docs.python.org/3/">
  3.13 Documentation</a></td></tr>
<tr><td class="result-snippet">Official Python 3 documentation.</td></tr>
<tr><td><a class="result-link" href="javascript:void(0)">bogus</a></td></tr>
</table></body></html>
"""

DDG_BLOCK_PAGE = "<html><body>anomaly detected, no results</body></html>"


class TestValidateUrl(unittest.TestCase):
    def test_adds_https(self):
        self.assertEqual(_validate_url("example.com/x"),
                         "https://example.com/x")

    def test_keeps_http(self):
        self.assertEqual(_validate_url("http://example.com"),
                         "http://example.com")

    def test_rejects_garbage(self):
        with self.assertRaises(ProviderError):
            _validate_url("not a url at all")


class TestDDGLiteParser(unittest.TestCase):
    def _search(self, html):
        class FakeResp:
            def __init__(self, body): self._b = body
            def read(self): return self._b.encode()
            def __enter__(self): return self
            def __exit__(self, *a): return False
        with mock.patch("urllib.request.urlopen",
                        return_value=FakeResp(html)) as opened:
            results = providers._ddg_html_search("python", 5, 10)
            req = opened.call_args[0][0]
        return results, req

    def test_parses_links_and_snippets(self):
        results, _ = self._search(DDG_LITE_HTML)
        self.assertEqual(len(results), 2)  # javascript: anchor dropped
        self.assertEqual(results[0]["url"], "https://www.python.org/")
        self.assertIn("official home", results[0]["snippet"])
        self.assertEqual(results[1]["url"], "https://docs.python.org/3/")

    def test_unwraps_uddg_redirect(self):
        results, _ = self._search(DDG_LITE_HTML)
        self.assertNotIn("uddg", results[0]["url"])

    def test_block_page_yields_empty_not_fabricated(self):
        results, _ = self._search(DDG_BLOCK_PAGE)
        self.assertEqual(results, [])

    def test_plain_ua_is_used(self):
        # lite.duckduckgo.com blocks partial browser UAs (verified live);
        # the request must carry exactly "Mozilla/5.0".
        _, req = self._search(DDG_LITE_HTML)
        self.assertEqual(req.headers.get("User-agent"), "Mozilla/5.0")

    def test_network_error_returns_empty(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            self.assertEqual(providers._ddg_html_search("q", 5, 5), [])


class TestSearchWebHonesty(unittest.TestCase):
    def test_raises_when_all_providers_empty(self):
        with mock.patch.object(providers, "_ddg_html_search", return_value=[]), \
             mock.patch("actions.web_search._ddg_search", return_value=[]):
            with self.assertRaises(ProviderError):
                providers.search_web("anything")

    def test_fallback_used_only_when_primary_empty(self):
        primary = [{"title": "T", "url": "https://a.example", "snippet": "s"}]
        with mock.patch.object(providers, "_ddg_html_search",
                               return_value=primary), \
             mock.patch("actions.web_search._ddg_search") as legacy:
            results = providers.search_web("q")
            legacy.assert_not_called()
        self.assertEqual(results[0].url, "https://a.example")


class TestYouTubeTranscript(unittest.TestCase):
    """The json3 branch: events[].segs[].utf8 joined into clean text."""

    def _run_with(self, sub_ext, sub_body, meta_extra=None):
        meta = {"title": "T", "channel": "C", "duration_string": "1:00",
                "view_count": 1, "upload_date": "20240101",
                "description": "d", "webpage_url": "https://youtu.be/x",
                "subtitles": {"en": [{"ext": sub_ext, "url": "https://subs"}]},
                "automatic_captions": {}}
        meta.update(meta_extra or {})
        install = mock.Mock(yt_dlp="yt-dlp.exe")
        runner = mock.Mock()
        runner.run.return_value = (0, json.dumps(meta), "")
        with mock.patch.object(providers, "_http_get", return_value=sub_body):
            return providers.read_youtube("https://youtu.be/x", install, runner)

    def test_json3_is_parsed_not_dumped_raw(self):
        body = json.dumps({"events": [
            {"segs": [{"utf8": "All right, "}, {"utf8": "so here we are"}]},
            {"segs": None},
            {"segs": [{"utf8": " at the zoo"}]}]})
        doc = self._run_with("json3", body)
        self.assertIn("All right, so here we are at the zoo",
                      doc.cleaned_text)
        self.assertNotIn('"tStartMs"', doc.cleaned_text)
        self.assertNotIn('{"events"', doc.cleaned_text)
        self.assertTrue(doc.metadata["has_transcript"])

    def test_vtt_is_stripped(self):
        vtt = ("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n"
               "<c>Hello</c> world\n")
        doc = self._run_with("vtt", vtt)
        self.assertIn("Hello world", doc.cleaned_text)
        self.assertNotIn("-->", doc.cleaned_text)

    def test_no_captions_states_it_plainly(self):
        doc = self._run_with("json3", "",
                             meta_extra={"subtitles": {},
                                         "automatic_captions": {}})
        self.assertIn("No captions are available", doc.cleaned_text)
        self.assertFalse(doc.metadata["has_transcript"])

    def test_missing_install_is_honest(self):
        with self.assertRaises(ProviderError):
            providers.read_youtube("https://youtu.be/x", None, mock.Mock())


class TestGitHubParsing(unittest.TestCase):
    def test_repo_url_forms(self):
        for form in ("https://github.com/owner/repo",
                     "github.com/owner/repo.git", "owner/repo"):
            m = providers._GH_REPO.search(form)
            if m:
                self.assertEqual(m.group(1), "owner")
            else:
                self.assertIn("/", form)  # bare owner/repo path

    def test_unparseable_repo_raises(self):
        with self.assertRaises(ProviderError):
            providers.read_github("just words no slash")


if __name__ == "__main__":
    unittest.main()
