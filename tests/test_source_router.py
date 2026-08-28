import json
import tempfile
import datetime as dt
import unittest
from pathlib import Path
from unittest.mock import patch

import source_router
from edition_profiles import resolve_edition_context


class SourceRouterTests(unittest.TestCase):
    def test_unconfigured_routes_are_explicitly_unavailable(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.dict(source_router.os.environ, {"RSS_FEEDS": "", "SOURCE_ROUTER_LIVE": "false"}, clear=False):
                result = source_router.collect(Path(temp))
            status = json.loads((Path(temp) / "source_status.json").read_text(encoding="utf-8"))
            self.assertEqual(result["source_count"], 0)
            self.assertEqual(status["sources"]["x"]["status"], "unavailable")
            self.assertEqual(status["sources"]["exa"]["status"], "unavailable")
            self.assertEqual(status["sources"]["jina"]["status"], "unavailable")

    def test_default_rss_feeds_are_configured_when_env_missing(self):
        with patch.dict(source_router.os.environ, {}, clear=True):
            self.assertGreaterEqual(len(source_router._rss_feeds()), 5)
            self.assertIn("https://openai.com/news/rss.xml", source_router._rss_feeds())

    def test_duplicate_materials_are_filtered_with_reason(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(source_router, "_rss", return_value=[
                {"title": "AI funding", "url": "https://example.test/a", "summary": "same"},
                {"title": "AI funding", "url": "https://example.test/a", "summary": "same"},
            ]), patch.dict(source_router.os.environ, {"RSS_FEEDS": "https://example.test/feed", "SOURCE_ROUTER_LIVE": "false"}, clear=False):
                source_router.collect(Path(temp))
            filtered = json.loads((Path(temp) / "filtered_materials.json").read_text(encoding="utf-8"))
            self.assertEqual(len(filtered), 1)
            self.assertEqual(filtered[0]["reason"], "duplicate_url_or_similarity")

    def test_atom_feeds_are_parsed(self):
        atom = b"""<?xml version="1.0" encoding="utf-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <title>AI market note</title>
            <link href="https://example.test/atom-note" />
            <summary>Atom summary</summary>
            <updated>2026-07-20T00:00:00Z</updated>
          </entry>
        </feed>"""

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return atom

        with patch.dict(source_router.os.environ, {"RSS_ITEMS_PER_FEED": "8"}, clear=False):
            with patch("source_router.urllib.request.urlopen", return_value=FakeResponse()):
                items = source_router._rss("https://example.test/atom.xml")
        self.assertEqual(items[0]["title"], "AI market note")
        self.assertEqual(items[0]["url"], "https://example.test/atom-note")

    def test_future_material_is_excluded_by_edition_cutoff(self):
        context = resolve_edition_context("morning_close_review")
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(source_router, "_rss", return_value=[
                {"title": "before cutoff", "url": "https://example.test/before", "summary": "ok", "published_at": (context.scheduled_cutoff - dt.timedelta(minutes=1)).isoformat()},
                {"title": "after cutoff", "url": "https://example.test/after", "summary": "future", "published_at": (context.scheduled_cutoff + dt.timedelta(minutes=1)).isoformat()},
            ]), patch.dict(source_router.os.environ, {"RSS_FEEDS": "https://example.test/feed", "SOURCE_ROUTER_LIVE": "false"}, clear=False):
                result = source_router.collect(Path(temp), edition="morning_close_review")
            assert result["source_count"] == 1
            assert result["future_items_discarded"] == 1
            status = json.loads((Path(temp) / "source_status.json").read_text(encoding="utf-8"))
            assert status["data_cutoff"] == context.scheduled_cutoff.isoformat()

    def test_material_before_edition_window_is_excluded(self):
        context = resolve_edition_context("morning_close_review")
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(source_router, "_rss", return_value=[
                {"title": "inside window", "url": "https://example.test/current", "summary": "ok", "published_at": (context.source_window_start + dt.timedelta(minutes=1)).isoformat()},
                {"title": "outside window", "url": "https://example.test/old", "summary": "old", "published_at": (context.source_window_start - dt.timedelta(minutes=1)).isoformat()},
            ]), patch.dict(source_router.os.environ, {"RSS_FEEDS": "https://example.test/feed", "SOURCE_ROUTER_LIVE": "false"}, clear=False):
                result = source_router.collect(Path(temp), edition="morning_close_review")
            assert result["source_count"] == 1
            assert result["stale_items_discarded"] == 1
            status = json.loads((Path(temp) / "source_status.json").read_text(encoding="utf-8"))
            assert status["source_window_start"] == context.source_window_start.isoformat()

    def test_shared_github_cache_requires_explicit_opt_in(self):
        context = resolve_edition_context("morning_close_review")
        with tempfile.TemporaryDirectory() as temp:
            cache = Path(temp) / "cache.json"
            cache.write_text(json.dumps({"generated_at": (context.scheduled_cutoff - dt.timedelta(hours=1)).isoformat(), "selected": [{"full_name": "demo/repo"}]}), encoding="utf-8")
            current_run_root = Path(temp) / "current-run"
            with patch.dict(source_router.os.environ, {"GITHUB_SHARED_CACHE_ENABLED": "false"}, clear=False):
                assert source_router._github_cache_is_current(cache, current_run_root, context.scheduled_cutoff) is False
            with patch.dict(source_router.os.environ, {"GITHUB_SHARED_CACHE_ENABLED": "true", "GITHUB_CACHE_MAX_AGE_HOURS": "6"}, clear=False):
                assert source_router._github_cache_is_current(cache, current_run_root, context.scheduled_cutoff) is True

    def test_selected_routes_are_recorded_and_unselected_routes_are_not_run(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(source_router, "_rss", return_value=[{"title": "selected", "url": "https://example.test/selected", "summary": "ok"}]), patch.dict(
                source_router.os.environ,
                {"RSS_FEEDS": "https://example.test/feed", "SOURCE_ROUTER_LIVE": "false"},
                clear=False,
            ):
                source_router.collect(Path(temp), edition="morning_close_review", sources=["rss"])
            status = json.loads((Path(temp) / "source_status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["selected_sources"], ["rss"])
            self.assertEqual(status["sources"]["rss"]["status"], "healthy")
            self.assertEqual(status["sources"]["x"]["status"], "not_selected")
            self.assertEqual(status["sources"]["exa"]["status"], "not_selected")
            self.assertEqual(status["sources"]["jina"]["status"], "not_selected")
            self.assertEqual(status["sources"]["github"]["status"], "not_selected")


if __name__ == "__main__":
    unittest.main()
