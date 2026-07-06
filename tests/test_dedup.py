"""Tests for dedup_articles module (per-show citations directories)."""

import json
from datetime import datetime, timedelta

import pytest
from unittest.mock import MagicMock

import dedup_articles
from dedup_articles import (
    normalize_title,
    title_similarity,
    format_evolving_story_context,
    cluster_and_rescore_corpus,
    load_recent_citations,
    deduplicate_articles,
)


class TestNormalizeTitle:
    def test_removes_source_tags(self):
        assert normalize_title("[CNN] Breaking News Story") == "breaking news story"

    def test_strips_whitespace(self):
        assert normalize_title("  Hello World  ") == "hello world"

    def test_lowercases(self):
        assert normalize_title("ALL CAPS TITLE") == "all caps title"

    def test_empty_string(self):
        assert normalize_title("") == ""


class TestTitleSimilarity:
    def test_identical_titles(self):
        assert title_similarity("Breaking News", "Breaking News") == 1.0

    def test_completely_different(self):
        assert title_similarity("abc xyz", "123 456") < 0.3

    def test_similar_titles(self):
        sim = title_similarity(
            "Tesla Announces New Battery Tech",
            "Tesla Reveals New Battery Technology"
        )
        assert sim > 0.6

    def test_source_tags_ignored(self):
        sim = title_similarity(
            "[Reuters] AI Breakthrough",
            "[AP News] AI Breakthrough"
        )
        assert sim > 0.8


class TestFormatEvolvingStoryContext:
    def test_empty_list(self):
        assert format_evolving_story_context([]) == ""

    def test_formats_stories(self):
        stories = [{
            "article": {"title": "New Update on AI Law"},
            "original_date": "2026-01-30",
            "original_title": "AI Law Proposed",
            "similarity": 0.75,
        }]
        result = format_evolving_story_context(stories)
        assert "EVOLVING STORIES" in result
        assert "New Update on AI Law" in result
        assert "2026-01-30" in result


def _write_citations(directory, date_str, url="https://example.com/story"):
    payload = {
        "episode": {"date": date_str},
        "segments": {
            "news": {"articles": [{"url": url, "title": f"Story from {date_str}"}]}
        },
    }
    path = directory / f"citations_{date_str}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")


class TestLoadRecentCitations:
    """Window regression tests: the same-weekday episode exactly 7 days ago
    fed by the same rolling feed cache must always be inside the window."""

    def test_exactly_seven_days_ago_included(self, tmp_path):
        date_str = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        _write_citations(tmp_path, date_str)
        citations = load_recent_citations(podcasts_dir=tmp_path)
        assert [c["episode_date"] for c in citations] == [date_str]

    def test_exactly_eight_days_ago_included(self, tmp_path):
        date_str = (datetime.now() - timedelta(days=8)).strftime("%Y-%m-%d")
        _write_citations(tmp_path, date_str)
        assert len(load_recent_citations(podcasts_dir=tmp_path)) == 1

    def test_nine_days_ago_excluded(self, tmp_path):
        date_str = (datetime.now() - timedelta(days=9)).strftime("%Y-%m-%d")
        _write_citations(tmp_path, date_str)
        assert load_recent_citations(podcasts_dir=tmp_path) == []

    def test_shows_are_isolated(self, tmp_path):
        """Two shows' citation histories never mix."""
        show_a = tmp_path / "a"
        show_b = tmp_path / "b"
        show_a.mkdir()
        show_b.mkdir()
        date_str = datetime.now().strftime("%Y-%m-%d")
        _write_citations(show_a, date_str)
        assert len(load_recent_citations(podcasts_dir=show_a)) == 1
        assert load_recent_citations(podcasts_dir=show_b) == []


class TestDeduplicateArticles:
    def test_url_dedup_filters_covered_stories(self, tmp_path):
        date_str = datetime.now().strftime("%Y-%m-%d")
        _write_citations(tmp_path, date_str, url="https://example.com/covered")
        articles = [
            {"title": "Covered story", "url": "https://example.com/covered"},
            {"title": "Fresh story", "url": "https://example.com/fresh"},
        ]
        fresh, _ = deduplicate_articles(articles, podcasts_dir=tmp_path)
        assert [a["url"] for a in fresh] == ["https://example.com/fresh"]

    def test_empty_history_passes_everything(self, tmp_path):
        articles = [{"title": "T", "url": "https://x/1"}]
        fresh, evolving = deduplicate_articles(articles, podcasts_dir=tmp_path)
        assert fresh == articles
        assert evolving == []


def _make_client(clusters):
    """Return a mock Anthropic client whose messages.create returns the given clusters."""
    response = MagicMock()
    response.content = [MagicMock(type="text", text=json.dumps({"clusters": clusters}))]
    client = MagicMock()
    client.messages.create.return_value = response
    return client


class TestClusterAndRescore:
    def _articles(self):
        return [
            {"title": "EFF quits X", "summary": "EFF leaves X platform", "_boosted_score": 80, "ai_score": 80},
            {"title": "EFF leaves Twitter", "summary": "EFF departs X", "_boosted_score": 60, "ai_score": 60},
            {"title": "EFF drops X account", "summary": "EFF no longer on X", "_boosted_score": 50, "ai_score": 50},
            {"title": "Unrelated tech story", "summary": "Something else", "_boosted_score": 75, "ai_score": 75},
        ]

    def test_no_client_returns_unchanged(self):
        articles = self._articles()
        result = cluster_and_rescore_corpus(articles, "Tech", client=None)
        assert result is articles

    def test_empty_articles_returns_unchanged(self):
        result = cluster_and_rescore_corpus([], "Tech", client=_make_client([]))
        assert result == []

    def test_no_clusters_returns_unchanged_scores(self):
        articles = self._articles()
        client = _make_client([])
        result = cluster_and_rescore_corpus(articles, "Tech", client=client)
        for orig, res in zip(articles, result):
            assert res["_boosted_score"] == orig["_boosted_score"]

    def test_canonical_article_score_unchanged(self):
        articles = self._articles()
        client = _make_client([{"label": "EFF quitting X", "indices": [0, 1, 2]}])
        result = cluster_and_rescore_corpus(articles, "Tech", client=client)
        assert result[0]["_boosted_score"] == 80
        assert not result[0].get("_cluster_suppressed")

    def test_duplicates_are_penalized(self):
        articles = self._articles()
        client = _make_client([{"label": "EFF quitting X", "indices": [0, 1, 2]}])
        result = cluster_and_rescore_corpus(articles, "Tech", client=client)
        assert result[1]["_boosted_score"] == max(1, int(60 * 0.3))
        assert result[2]["_boosted_score"] == max(1, int(50 * 0.3))
        assert result[1]["_cluster_suppressed"] is True
        assert result[2]["_cluster_suppressed"] is True

    def test_singleton_articles_unaffected(self):
        articles = self._articles()
        client = _make_client([{"label": "EFF quitting X", "indices": [0, 1, 2]}])
        result = cluster_and_rescore_corpus(articles, "Tech", client=client)
        assert result[3]["_boosted_score"] == 75
        assert not result[3].get("_cluster_suppressed")

    def test_invalid_json_fallback_returns_unchanged(self):
        response = MagicMock()
        response.content = [MagicMock(type="text", text="this is not json {{{{")]
        client = MagicMock()
        client.messages.create.return_value = response
        articles = self._articles()
        result = cluster_and_rescore_corpus(articles, "Tech", client=client)
        for orig, res in zip(articles, result):
            assert res["_boosted_score"] == orig["_boosted_score"]

    def test_original_articles_not_mutated(self):
        articles = self._articles()
        client = _make_client([{"label": "EFF quitting X", "indices": [0, 1, 2]}])
        cluster_and_rescore_corpus(articles, "Tech", client=client)
        assert articles[1]["_boosted_score"] == 60
        assert not articles[1].get("_cluster_suppressed")
