#!/usr/bin/env python3
"""
Source ingestion — turns a profile's `sources` block into a flat article list.

Replaces the old dependency on the sibling `super-rss-feed` repo: instead of
pulling pre-scored JSON, the personal podcast creator ingests the user's own
feeds directly. Three input kinds, mixable in one profile:

  - opml     : an OPML file listing RSS/Atom feed URLs (parsed with stdlib)
  - rss      : explicit RSS/Atom feed URLs
  - keywords : search terms — NOT fetched here; handed to search.py

Every article is normalised to the shape the scoring/generation pipeline
expects (the same shape super-rss-feed used to emit):

    {"title", "url", "summary", "body", "published", "source"}
"""

import xml.etree.ElementTree as ET
from pathlib import Path

try:
    import feedparser
except ImportError:  # ponytail: keep import soft so tests can stub it
    feedparser = None


def parse_opml(opml_path):
    """Return the list of feed URLs (outline/@xmlUrl) in an OPML file.

    Uses stdlib ElementTree (ponytail rung 2 — no dependency needed). Nested
    <outline> groups are flattened; entries without xmlUrl (folder nodes) are
    skipped.
    """
    path = Path(opml_path)
    if not path.exists():
        return []
    root = ET.parse(path).getroot()
    return [
        el.attrib["xmlUrl"]
        for el in root.iter("outline")
        if el.attrib.get("xmlUrl")
    ]


def _normalize_entry(entry, source):
    """Map one feedparser entry to the pipeline's article dict."""
    return {
        "title": entry.get("title", "").strip(),
        "url": entry.get("link", "").strip(),
        # feedparser exposes the short blurb as `summary`; full text (when the
        # feed carries it) lives in content[].value.
        "summary": entry.get("summary", "").strip(),
        "body": (entry.get("content", [{}])[0].get("value", "")
                 if entry.get("content") else "").strip(),
        "published": entry.get("published", entry.get("updated", "")),
        "source": source,
    }


def fetch_feed(feed_url):
    """Fetch and normalise a single RSS/Atom feed. Returns [] on any failure."""
    if feedparser is None:
        raise RuntimeError("feedparser not installed — add it to requirements.txt")
    try:
        parsed = feedparser.parse(feed_url)
    except Exception as e:  # feedparser is lenient, but network layer can throw
        print(f"  ⚠️  feed fetch failed for {feed_url}: {e}")
        return []
    source = parsed.feed.get("title", feed_url) if getattr(parsed, "feed", None) else feed_url
    return [_normalize_entry(e, source) for e in parsed.entries if e.get("link")]


def _dedup_by_url(articles):
    """Drop duplicate URLs, preserving first-seen order."""
    seen, out = set(), []
    for a in articles:
        url = a.get("url", "")
        if url and url not in seen:
            seen.add(url)
            out.append(a)
    return out


def gather_feed_articles(profile):
    """Collect articles from a profile's OPML + explicit RSS feeds.

    Keyword sources are intentionally excluded here — they are search inputs,
    resolved by search.py. Returns a URL-deduplicated list.
    """
    sources = profile.get("sources", {})
    feed_urls = list(sources.get("rss", []))
    if sources.get("opml"):
        feed_urls.extend(parse_opml(sources["opml"]))

    articles = []
    for url in feed_urls:
        articles.extend(fetch_feed(url))
    return _dedup_by_url(articles)


if __name__ == "__main__":
    import sys
    from config_loader import load_podcast_profile

    name = sys.argv[1] if len(sys.argv) > 1 else "cariboo-signals"
    profile = load_podcast_profile(name)
    arts = gather_feed_articles(profile)
    print(f"Profile '{name}': {len(arts)} feed articles")
    for a in arts[:10]:
        print(f"  - [{a['source']}] {a['title']}")
