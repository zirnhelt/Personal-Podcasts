#!/usr/bin/env python3
"""
Source ingestion — turns a profile's `sources` block into a flat article list.

Replaces the old dependency on the sibling `super-rss-feed` repo: instead of
pulling pre-scored JSON, the personal podcast creator ingests the user's own
feeds directly. Three input kinds, mixable in one profile:

  - opml     : an OPML file listing RSS/Atom feed URLs (parsed with stdlib)
  - rss      : explicit RSS/Atom feed URLs
  - keywords : search terms — NOT fetched here; handed to search.py
  - kite     : Kagi News (Kite) category names, e.g. ["AI", "Canada"]. Each
               cluster is already merged across outlets and summarised; it
               enters as one article whose body is that summary.

Every article is normalised to the shape the scoring/generation pipeline
expects (the same shape super-rss-feed used to emit):

    {"title", "url", "summary", "body", "published", "source"}

Kite articles add two keys every other stage ignores: `outlets` (the cluster's
original reporting, [{"name", "url"}]) and `via` ("Kagi News"), which the
generator uses for attribution (TRACE source integrity + CC BY-NC credit).
"""

import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

import requests

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


# ── Kagi News (Kite) ────────────────────────────────────────────────────────
# Ported from super-rss-feed (super_rss_curator_json.py _kite_* helpers), as
# plain dicts. Kite publishes kite.json (category name → file) plus one JSON
# file per category, refreshed daily at 12:00 UTC. Free and keyless.
KITE_INDEX_URL = "https://kite.kagi.com/kite.json"
KITE_MIN_SOURCES = 2            # never mislead: a "cluster" of one outlet is not one
KITE_MAX_PER_CATEGORY = 12      # Kite publishes about 12 clusters per category
KITE_TIMEOUT_S = 15
KITE_VIA = "Kagi News"
KITE_ATTRIBUTION = "Kagi News (CC BY-NC)"
KITE_CATEGORIES_CACHE = Path(__file__).parent / "config" / "kite_categories.json"
_KITE_HEADERS = {"User-Agent": "PersonalPodcastCreator/1.0 (personal use)"}

# Kite cites each claim inline as "[domain#n]": the n-th article from that
# domain in the cluster's `articles` list. Runs of them ("[a#1][b#2]") are one group.
_KITE_CITATION_RE = re.compile(r'\s*((?:\[[\w.-]+#\d+\])+)')
_KITE_CITE_RE = re.compile(r'\[([\w.-]+)#(\d+)\]')
# Kite hands about half its outlet links over as Google News redirects.
_AGGREGATOR_HOSTS = ("news.google.com",)


def _kite_text(text: str, attribute: bool) -> str:
    """Kite prose with its citation markers stripped, or turned into "(domain, ...)".

    The summary shows stripped prose; the body keeps the outlets inline so the
    hosts can attribute each claim to whoever reported it (TRACE source integrity).
    """
    def _group(match: re.Match) -> str:
        if not attribute:
            return ""
        domains = list(dict.fromkeys(d for d, _ in _KITE_CITE_RE.findall(match.group(1))))
        return f" ({', '.join(domains)})"
    return _KITE_CITATION_RE.sub(_group, text or "").strip()


def _kite_outlet_url(article: dict) -> str:
    """The article's own http(s) link, or '' for a redirect or a non-web scheme."""
    link = (article.get("link") or "").strip()
    parts = urlparse(link)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    if parts.netloc.lower().removeprefix("www.") in _AGGREGATOR_HOSTS:
        return ""
    return link


def _kite_primary_article(story: dict) -> dict:
    """The cluster article the summary cites first; `articles[0]` is often not it.

    Kite orders `articles` by fetch, not relevance. The summary's first citation
    names the article the headline was written from. Redirect-only links are
    passed over: a story is linked to an outlet or not linked at all.
    """
    articles = [a for a in story.get("articles") or []
                if isinstance(a, dict) and _kite_outlet_url(a)]
    for domain, n in _KITE_CITE_RE.findall(story.get("short_summary") or ""):
        from_domain = [a for a in articles if (a.get("domain") or "").lower() == domain.lower()]
        if from_domain:
            return from_domain[min(int(n), len(from_domain)) - 1]
    return articles[0] if articles else {}


def _kite_outlets(story: dict) -> list[dict]:
    """Every outlet in the cluster once, [{"name", "url"}], first real link kept.

    A domain whose only link is a redirect is still listed (url "") — it did the
    reporting and is credited by name.
    """
    outlets: dict[str, str] = {}
    for a in story.get("articles") or []:
        if not isinstance(a, dict) or not a.get("domain"):
            continue
        name = a["domain"].lower().removeprefix("www.")
        if not outlets.get(name):
            outlets[name] = _kite_outlet_url(a)
    return [{"name": n, "url": u} for n, u in outlets.items()]


def _kite_source_count(story: dict) -> int:
    """Distinct outlets behind a cluster (Kite's own count, else counted)."""
    return int(story.get("unique_domains") or 0) or len(_kite_outlets(story))


def _kite_published(primary: dict) -> str:
    """The primary outlet's date, clamped to now (Kite passes future dates through)."""
    now = datetime.now(timezone.utc)
    try:
        when = datetime.fromisoformat((primary.get("date") or "").replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
    except ValueError:
        when = now
    return min(when, now).isoformat()


def _make_kite_article(story: dict) -> dict | None:
    """One Kite cluster as a pipeline article, or None if it would mislead.

    Dropped: fewer than KITE_MIN_SOURCES outlets, an empty summary, no title,
    or no real outlet link to point listeners at.
    """
    title = (story.get("title") or "").strip()
    summary = _kite_text(story.get("short_summary", ""), attribute=False)
    if not title or not summary or _kite_source_count(story) < KITE_MIN_SOURCES:
        return None
    primary = _kite_primary_article(story)
    url = _kite_outlet_url(primary)
    if not url:
        return None
    parts = [_kite_text(story.get("short_summary", ""), attribute=True)]
    parts += [_kite_text(p, attribute=True) for p in story.get("talking_points") or []
              if isinstance(p, str)]
    return {
        "title": title,
        "url": url,
        "summary": summary,
        "body": " ".join(p for p in parts if p),
        "published": _kite_published(primary),
        "source": urlparse(url).netloc.lower().removeprefix("www."),
        "outlets": _kite_outlets(story),
        "via": KITE_VIA,
        "_kite_sources": _kite_source_count(story),
    }


def _kite_event_key(category: str, story: dict) -> tuple:
    """Kite files one event under several categories and says so in `twin_of`."""
    twin = story.get("twin_of")
    if isinstance(twin, dict) and twin.get("main_category"):
        return (twin["main_category"], twin.get("main_cluster_number"))
    return (category, story.get("cluster_number"))


def _kite_get_json(url: str):
    resp = requests.get(url, timeout=KITE_TIMEOUT_S, headers=_KITE_HEADERS)
    resp.raise_for_status()
    return resp, resp.json()


def fetch_kite_index() -> tuple[str, dict[str, str]]:
    """(resolved index URL, {category name: file}) from kite.json. Raises on failure.

    The resolved URL matters: kite.kagi.com redirects, and category files are
    relative to wherever the index actually lives.
    """
    resp, data = _kite_get_json(KITE_INDEX_URL)
    files = {c["name"]: c["file"] for c in data.get("categories") or []
             if isinstance(c, dict) and c.get("name") and c.get("file")}
    return getattr(resp, "url", KITE_INDEX_URL) or KITE_INDEX_URL, files


def load_kite_category_cache() -> set[str] | None:
    """Known Kite category names from the committed snapshot, or None if absent."""
    try:
        return set(json.loads(KITE_CATEGORIES_CACHE.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return None


def refresh_kite_category_cache() -> int:
    """Merge kite.json's current names into the snapshot; returns the new total.

    Union, never replace: Kite only lists a category in a batch that has stories
    for it, so a replace could make a valid profile fail validation. Run by hand
    (`python sources.py --refresh-kite-categories`), never during a generation
    run — CI only commits shows/, and a dirty config/ would break its rebase.
    """
    _, files = fetch_kite_index()
    names = sorted((load_kite_category_cache() or set()) | set(files))
    KITE_CATEGORIES_CACHE.write_text(json.dumps(names, indent=1, ensure_ascii=False) + "\n",
                                     encoding="utf-8")
    return len(names)


def gather_kite_articles(profile) -> list[dict]:
    """Articles from a profile's Kite categories. Never raises.

    Any failure — index, one category file, bad JSON — is logged and costs only
    that source; the episode carries on with OPML/RSS/keyword articles.
    Free: one GET for the index plus one per category, no API tokens.
    """
    wanted = (profile.get("sources") or {}).get("kite") or []
    if not wanted:
        return []
    try:
        index_url, files = fetch_kite_index()
    except Exception as e:  # network, HTTP status, JSON — all degrade the same way
        print(f"  ⚠️  Kite index fetch failed ({e}) — continuing without Kagi News")
        return []

    best: dict[tuple, dict] = {}
    dropped = 0
    for name in wanted:
        if name not in files:
            print(f"  ⚠️  Kite: category {name!r} not in today's index — skipped")
            continue
        try:
            _, data = _kite_get_json(urljoin(index_url, quote(files[name])))
            clusters = data.get("clusters") or []
        except Exception as e:
            print(f"  ⚠️  Kite/{name} fetch failed ({e}) — skipped")
            continue
        for story in clusters[:KITE_MAX_PER_CATEGORY]:
            if not isinstance(story, dict):
                continue
            article = _make_kite_article(story)
            if article is None:
                dropped += 1
                continue
            key = _kite_event_key(name, story)
            if key not in best or article["_kite_sources"] > best[key]["_kite_sources"]:
                best[key] = article
    articles = _dedup_by_url(list(best.values()))
    print(f"  📰 Kite: {len(articles)} clusters from {len(wanted)} categories "
          f"({dropped} thin or unlinked dropped)")
    return articles


def gather_feed_articles(profile):
    """Collect articles from a profile's OPML + explicit RSS feeds + Kite categories.

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
    articles.extend(gather_kite_articles(profile))
    return _dedup_by_url(articles)

if __name__ == "__main__":
    import sys
    from config_loader import load_podcast_profile

    if sys.argv[1:] == ["--refresh-kite-categories"]:
        print(f"Kite category snapshot: {refresh_kite_category_cache()} names "
              f"in {KITE_CATEGORIES_CACHE}")
        sys.exit(0)
    name = sys.argv[1] if len(sys.argv) > 1 else "sample"
    profile = load_podcast_profile(name)
    arts = gather_feed_articles(profile)
    print(f"Profile '{name}': {len(arts)} feed articles")
    for a in arts[:10]:
        print(f"  - [{a['source']}] {a['title']}")
