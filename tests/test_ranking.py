"""Tests for ranking.py — profile-driven story selection."""

import json

from unittest.mock import MagicMock

import ranking


PROFILE = {
    "name": "t",
    "title": "Test Show",
    "tagline": "About testing",
    "scope": "global",
    "sources": {"keywords": ["rural broadband", "wildfire tech"]},
}
PLAN = {"target_words": 2250, "article_count": 2, "format": "dialog", "fetch_bodies": True}


def _articles():
    return [
        {"title": "Rural broadband expansion hits milestone", "summary": "fiber", "url": "https://x/1"},
        {"title": "Wildfire detection drones deployed", "summary": "drones", "url": "https://x/2"},
        {"title": "Celebrity gossip roundup", "summary": "gossip", "url": "https://x/3"},
    ]


def _scoring_client(scores):
    """Mock Anthropic client returning the given index->score mapping."""
    payload = {"scores": [{"index": i, "score": s} for i, s in scores.items()]}
    response = MagicMock()
    response.content = [MagicMock(type="text", text=json.dumps(payload))]
    response.usage = MagicMock(input_tokens=100, output_tokens=50)
    client = MagicMock()
    client.messages.create.return_value = response
    return client


def test_local_fallback_scores_keyword_matches_higher():
    scores = ranking._local_relevance_scores(_articles(), PROFILE)
    assert scores[0] > scores[2]
    assert scores[1] > scores[2]


def test_score_articles_uses_batched_haiku_scores():
    client = _scoring_client({0: 9, 1: 8, 2: 1})
    scored = ranking.score_articles(_articles(), PROFILE, client=client)
    assert [a["ai_score"] for a in scored] == [9, 8, 1]
    # One batch → one API call (cost discipline: batch, don't loop)
    assert client.messages.create.call_count == 1


def test_scoring_prompt_contains_profile_topics():
    client = _scoring_client({0: 5, 1: 5, 2: 5})
    ranking.score_articles(_articles(), PROFILE, client=client)
    prompt = client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "rural broadband" in prompt
    assert "Test Show" in prompt


def test_rank_and_select_picks_top_scored(tmp_path):
    client = _scoring_client({0: 9, 1: 8, 2: 1})
    selected, _ = ranking.rank_and_select(
        _articles(), PROFILE, PLAN, client=client, podcasts_dir=tmp_path)
    assert len(selected) == 2
    titles = [a["title"] for a in selected]
    assert "Celebrity gossip roundup" not in titles


def test_rank_and_select_boosts_evolving_stories(tmp_path):
    # Yesterday's citations contain a similar title with a different URL →
    # evolving story; its +2 boost should carry it past an equal-scored rival.
    from datetime import datetime, timedelta
    date_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    (tmp_path / f"citations_{date_str}.json").write_text(json.dumps({
        "episode": {"date": date_str},
        "segments": {"news": {"articles": [
            {"url": "https://old/1", "title": "Rural broadband expansion hits milestone"}
        ]}},
    }))
    client = _scoring_client({0: 5, 1: 5, 2: 5})
    plan = dict(PLAN, article_count=1, fetch_bodies=False)
    selected, evolving_context = ranking.rank_and_select(
        _articles(), PROFILE, plan, client=client, podcasts_dir=tmp_path)
    assert selected[0]["title"] == "Rural broadband expansion hits milestone"
    assert "EVOLVING STORIES" in evolving_context


def test_rank_and_select_drops_already_covered_urls(tmp_path):
    from datetime import datetime
    date_str = datetime.now().strftime("%Y-%m-%d")
    (tmp_path / f"citations_{date_str}.json").write_text(json.dumps({
        "episode": {"date": date_str},
        "segments": {"news": {"articles": [
            {"url": "https://x/1", "title": "Rural broadband expansion hits milestone"}
        ]}},
    }))
    client = _scoring_client({0: 9, 1: 8})
    selected, _ = ranking.rank_and_select(
        _articles(), PROFILE, PLAN, client=client, podcasts_dir=tmp_path)
    assert all(a["url"] != "https://x/1" for a in selected)


def test_no_client_falls_back_to_local_scoring(tmp_path):
    selected, _ = ranking.rank_and_select(
        _articles(), PROFILE, PLAN, client=None, podcasts_dir=tmp_path)
    assert len(selected) == 2
    titles = [a["title"] for a in selected]
    assert "Celebrity gossip roundup" not in titles


def test_failed_scoring_chunk_falls_back_locally():
    client = MagicMock()
    client.messages.create.side_effect = Exception("API down")
    scored = ranking.score_articles(_articles(), PROFILE, client=client)
    assert all(isinstance(a["ai_score"], int) for a in scored)
