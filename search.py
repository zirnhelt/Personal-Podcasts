#!/usr/bin/env python3
"""
Web search layer — Brave + Kagi, scope-aware.

Two jobs:
  1. Resolve a profile's keyword sources into candidate articles.
  2. Enrich deep dives with live web results (used by podcast_generator.py).

Both providers normalise to {"title", "url", "description"}. Results are merged
and deduplicated by URL. Keys come from the environment:
    BRAVE_SEARCH_API_KEY, KAGI_API_KEY
A provider is skipped (not errored) when its key is missing or the profile
disables it, so a profile can run on either engine alone.
"""

import os
import time
import requests

# Per-run controls (ported from the upstream curated-podcast-generator perf
# work): a query cache so repeated searches in one run cost nothing, and
# pacing/capping for Brave, whose free tier allows 1 request/second and a
# monthly quota shared across every show in the generation matrix.
_SEARCH_CACHE: dict[tuple[str, int], list] = {}
_BRAVE_STATE = {"last_call": 0.0, "calls": 0}


def _brave_allowed() -> bool:
    """Per-run Brave call cap via BRAVE_SEARCH_CALL_LIMIT (0 = unlimited)."""
    limit = int(os.getenv("BRAVE_SEARCH_CALL_LIMIT", "0"))
    if limit and _BRAVE_STATE["calls"] >= limit:
        print(f"  Brave call limit ({limit}) reached — skipping Brave for this run")
        return False
    return True


def _brave_pace() -> None:
    """Sleep so consecutive Brave calls honour BRAVE_SEARCH_COOLDOWN_SECS."""
    cooldown = float(os.getenv("BRAVE_SEARCH_COOLDOWN_SECS", "1.0"))
    wait = _BRAVE_STATE["last_call"] + cooldown - time.monotonic()
    if wait > 0:
        time.sleep(wait)


def brave_search(query, api_key, count=5):
    """Call Brave Search. Returns [{title,url,description}], [] on failure."""
    if not _brave_allowed():
        return []
    _brave_pace()
    _BRAVE_STATE["last_call"] = time.monotonic()
    _BRAVE_STATE["calls"] += 1
    try:
        resp = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": api_key,
            },
            params={"q": query, "count": count, "search_lang": "en", "safesearch": "moderate"},
            timeout=10,
        )
        resp.raise_for_status()
        return [
            {"title": r.get("title", ""), "url": r.get("url", ""), "description": r.get("description", "")}
            for r in resp.json().get("web", {}).get("results", [])
        ]
    except Exception as e:
        print(f"  Brave search failed for '{query[:50]}': {e}")
        return []


def kagi_search(query, api_key, count=5):
    """Call Kagi Search. Returns [{title,url,description}], [] on failure.

    Kagi returns objects with t==0 for search results (t==1 are related
    searches); we keep only the former.
    """
    try:
        resp = requests.get(
            "https://kagi.com/api/v0/search",
            headers={"Authorization": f"Bot {api_key}"},
            params={"q": query, "limit": count},
            timeout=10,
        )
        resp.raise_for_status()
        return [
            {"title": r.get("title", ""), "url": r.get("url", ""), "description": r.get("snippet", "")}
            for r in resp.json().get("data", [])
            if r.get("t") == 0 and r.get("url")
        ]
    except Exception as e:
        print(f"  Kagi search failed for '{query[:50]}': {e}")
        return []


def _scoped_query(term, profile):
    """Append the region hint when the profile is regional, else leave global."""
    if profile.get("scope") == "regional":
        hint = profile.get("region_hint", "").strip()
        if hint:
            return f"{term} {hint}"
    return term


def _dedup_by_url(results):
    seen, out = set(), []
    for r in results:
        url = r.get("url", "")
        if url and url not in seen:
            seen.add(url)
            out.append(r)
    return out


def search_all(query, profile, count=5):
    """Run every enabled+keyed provider for one query; merge and dedup by URL.

    Results are cached per (query, count) for the life of the run — each run
    serves one profile, so the profile isn't part of the key.
    """
    key = (query, count)
    if key in _SEARCH_CACHE:
        return list(_SEARCH_CACHE[key])
    engines = profile.get("search", {"brave": True, "kagi": True})
    results = []
    brave_key = os.getenv("BRAVE_SEARCH_API_KEY")
    kagi_key = os.getenv("KAGI_API_KEY")
    if engines.get("brave") and brave_key:
        results.extend(brave_search(query, brave_key, count))
    if engines.get("kagi") and kagi_key:
        results.extend(kagi_search(query, kagi_key, count))
    _SEARCH_CACHE[key] = _dedup_by_url(results)
    return list(_SEARCH_CACHE[key])


def gather_keyword_articles(profile, count=5):
    """Resolve a profile's keyword sources into pipeline article dicts.

    Each keyword becomes a scope-adjusted query; web results are mapped to the
    same shape as feed articles ({title,url,summary,body,published,source}) so
    they merge cleanly with sources.gather_feed_articles().
    """
    keywords = profile.get("sources", {}).get("keywords", [])
    articles = []
    for term in keywords:
        for r in search_all(_scoped_query(term, profile), profile, count):
            articles.append({
                "title": r["title"],
                "url": r["url"],
                "summary": r["description"],
                "body": "",
                "published": "",
                "source": f"search:{term}",
            })
    return _dedup_by_url(articles)


if __name__ == "__main__":
    import sys
    from config_loader import load_podcast_profile

    name = sys.argv[1] if len(sys.argv) > 1 else "cariboo-signals"
    profile = load_podcast_profile(name)
    arts = gather_keyword_articles(profile)
    print(f"Profile '{name}': {len(arts)} keyword-search articles")
    for a in arts[:10]:
        print(f"  - [{a['source']}] {a['title']}")
