#!/usr/bin/env python3
"""
Ranking — profile-driven story selection for the multi-podcast creator.

Order of operations (cheapest first, per the API cost discipline):
  1. Dedup against the show's own citation history (local, no API)
  2. Batched Haiku relevance scoring built from the profile's topics + scope
     (one call per ~40 articles, compressed titles/summaries, tokens logged)
  3. Evolving-story boost (developing stories rank higher — novelty with context)
  4. Intra-batch duplicate clustering (Cohere embeddings or Haiku fallback)
  5. Optional Cohere rerank for deep dives
  6. Select plan["article_count"] top stories

When no Anthropic client is available the scorer falls back to local keyword
matching so the pipeline still runs (and tests need no API key).
"""

import json
import os
from string import Template

from config_loader import (HAIKU_MODEL, load_prompts_config, message_text,
                           parse_json_object, small_call_params)
from dedup_articles import (
    deduplicate_articles,
    cluster_and_rescore_corpus,
    format_evolving_story_context,
)
import cohere_enrichment

SCORING_MODEL = os.getenv("CLAUDE_SCORING_MODEL") or HAIKU_MODEL
SCORING_BATCH_SIZE = 40      # articles per Haiku call — batch, don't loop
SUMMARY_SNIPPET_CHARS = 120  # prompt compression: titles carry most signal
EVOLVING_BOOST = 2           # developing stories get a bump over cold items


def _profile_topics(profile):
    """Collapse a profile's topical signal into one comma-separated string."""
    parts = list(profile.get("sources", {}).get("keywords", []))
    if profile.get("scope") == "regional" and profile.get("region_hint"):
        parts.append(profile["region_hint"])
    return ", ".join(parts) if parts else profile.get("tagline", profile.get("title", ""))


def _editorial_lens(profile):
    prompts = load_prompts_config()
    if profile.get("scope") == "regional":
        return Template(prompts["editorial_lens_regional"]).safe_substitute(
            region_hint=profile.get("region_hint", "the region"))
    return prompts["editorial_lens_global"]


def _local_relevance_scores(articles, profile):
    """Keyword-overlap fallback scoring when no API client is available."""
    keywords = [k.lower() for k in profile.get("sources", {}).get("keywords", [])]
    words = {w for k in keywords for w in k.split()}
    scores = []
    for a in articles:
        text = f"{a.get('title', '')} {a.get('summary', '')}".lower()
        hits = sum(1 for w in words if w and w in text)
        scores.append(min(10, 3 + hits))  # neutral floor so feeds still rank
    return scores


def _haiku_relevance_scores(articles, profile, client):
    """One batched Haiku call per chunk; returns a 0-10 score per article."""
    prompts = load_prompts_config()
    scores = [None] * len(articles)

    for start in range(0, len(articles), SCORING_BATCH_SIZE):
        chunk = articles[start:start + SCORING_BATCH_SIZE]
        article_list = [
            {
                "index": start + i,
                "title": a.get("title", "")[:160],
                "summary": (a.get("summary", "") or "")[:SUMMARY_SNIPPET_CHARS],
            }
            for i, a in enumerate(chunk)
        ]
        prompt = Template(prompts["relevance_template"]).safe_substitute(
            title=profile.get("title", ""),
            tagline=profile.get("tagline", ""),
            editorial_lens=_editorial_lens(profile),
            topics=_profile_topics(profile),
            article_list=json.dumps(article_list, ensure_ascii=False),
        )
        try:
            response = client.messages.create(
                model=SCORING_MODEL,
                max_tokens=1500,
                messages=[{"role": "user", "content": prompt}],
                **small_call_params(SCORING_MODEL),
            )
            usage = getattr(response, "usage", None)
            print(f"  [api] relevance scoring: {len(chunk)} articles, "
                  f"input_tokens={getattr(usage, 'input_tokens', '?')} "
                  f"output_tokens={getattr(usage, 'output_tokens', '?')}")
            data = parse_json_object(message_text(response))
            for entry in data.get("scores", []):
                idx = entry.get("index")
                if isinstance(idx, int) and 0 <= idx < len(articles):
                    scores[idx] = max(0, min(10, int(entry.get("score", 0))))
        except Exception as e:
            print(f"  ⚠️  Haiku relevance scoring failed for chunk at {start}: {e}")

    # Any article the model skipped (or a failed chunk) gets the local fallback
    if any(s is None for s in scores):
        fallback = _local_relevance_scores(articles, profile)
        scores = [f if s is None else s for s, f in zip(scores, fallback)]
    return scores


def score_articles(articles, profile, client=None):
    """Attach ai_score/_boosted_score to every article (copies, not in place)."""
    if not articles:
        return []
    if client:
        raw_scores = _haiku_relevance_scores(articles, profile, client)
    else:
        print("  ℹ️ No Anthropic client — using local keyword relevance fallback")
        raw_scores = _local_relevance_scores(articles, profile)

    scored = []
    for article, score in zip(articles, raw_scores):
        a = article.copy()
        a["ai_score"] = score
        a["_boosted_score"] = score
        scored.append(a)
    return scored


def rank_and_select(articles, profile, plan, client=None, podcasts_dir=None):
    """Full selection pass: dedup → score → boost → cluster → rerank → top N.

    Returns (selected_articles, evolving_context) where evolving_context is a
    prompt-ready string describing developing stories ("" when none).
    """
    if not articles:
        return [], ""

    # 1. Never re-cover a story this show already cited (local, free)
    fresh, evolving_stories = deduplicate_articles(articles, podcasts_dir=podcasts_dir)
    if not fresh:
        return [], ""

    # 2. Relevance scoring (one batched Haiku call per 40 articles)
    scored = score_articles(fresh, profile, client=client)

    # 3. Developing stories get a novelty-with-context boost
    evolving_urls = {s["article"].get("url", "") for s in evolving_stories}
    for a in scored:
        if a.get("url", "") in evolving_urls:
            a["_boosted_score"] = a["_boosted_score"] + EVOLVING_BOOST
            a["_evolving"] = True

    # 4. Suppress same-story duplicates within today's batch
    scored = cluster_and_rescore_corpus(
        scored, _profile_topics(profile), client=client, model=SCORING_MODEL
    )

    # 5. Optional Cohere rerank for deep dives (returns None when disabled)
    count = plan["article_count"]
    if plan.get("fetch_bodies"):
        reranked = cohere_enrichment.rerank_for_deep_dive(
            _profile_topics(profile), scored, top_n=count
        )
        if reranked is not None:
            selected = reranked[:count]
            evolving_context = format_evolving_story_context(evolving_stories)
            return selected, evolving_context

    # 6. Top N by boosted score
    scored.sort(key=lambda a: a.get("_boosted_score", 0), reverse=True)
    selected = scored[:count]

    evolving_context = format_evolving_story_context(evolving_stories)
    return selected, evolving_context


if __name__ == "__main__":
    import sys
    from pathlib import Path
    from config_loader import load_podcast_profile
    from episode_plan import build_episode_plan
    import sources
    import search

    name = sys.argv[1] if len(sys.argv) > 1 else "sample"
    profile = load_podcast_profile(name)
    plan = build_episode_plan(profile)
    articles = sources.gather_feed_articles(profile) + search.gather_keyword_articles(profile)
    show_podcasts = Path(__file__).parent / "shows" / name / "podcasts"
    selected, evolving = rank_and_select(articles, profile, plan, podcasts_dir=show_podcasts)
    print(f"\nSelected {len(selected)}/{len(articles)} for '{name}':")
    for a in selected:
        print(f"  [{a.get('_boosted_score', '?')}] {a.get('title', '')[:80]}")
