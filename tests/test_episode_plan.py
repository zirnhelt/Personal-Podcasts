"""Tests for length/depth/cadence → generation-parameter mapping."""

import pytest
from episode_plan import build_episode_plan, WORDS_PER_MINUTE


def _profile(**overrides):
    base = {"length_minutes": 15, "depth": "deep_dive", "cadence": "dialog"}
    base.update(overrides)
    return base


def test_target_words_scales_with_length():
    assert build_episode_plan(_profile(length_minutes=5))["target_words"] == 5 * WORDS_PER_MINUTE
    assert build_episode_plan(_profile(length_minutes=30))["target_words"] == 30 * WORDS_PER_MINUTE


def test_deep_dive_fetches_bodies_and_fewer_articles():
    plan = build_episode_plan(_profile(length_minutes=15, depth="deep_dive"))
    assert plan["fetch_bodies"] is True
    assert plan["article_count"] == 5          # 15 / 3.0


def test_titles_skips_bodies_and_more_articles():
    plan = build_episode_plan(_profile(length_minutes=15, depth="titles"))
    assert plan["fetch_bodies"] is False
    assert plan["article_count"] == 15         # 15 / 1.0


def test_format_follows_cadence():
    assert build_episode_plan(_profile(cadence="news"))["format"] == "news"
    assert build_episode_plan(_profile(cadence="dialog"))["format"] == "dialog"


def test_article_count_never_zero():
    assert build_episode_plan(_profile(length_minutes=5, depth="deep_dive"))["article_count"] >= 1
