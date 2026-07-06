"""Tests for source ingestion (OPML parsing + RSS normalisation)."""

import types
import sources


OPML = """<?xml version="1.0"?>
<opml version="2.0"><body>
  <outline text="Group">
    <outline type="rss" xmlUrl="https://a.example/feed.xml"/>
    <outline type="rss" xmlUrl="https://b.example/feed.xml"/>
  </outline>
  <outline text="FolderWithoutFeed"/>
</body></opml>
"""


def test_parse_opml_flattens_and_skips_folders(tmp_path):
    f = tmp_path / "feeds.opml"
    f.write_text(OPML)
    assert sources.parse_opml(f) == [
        "https://a.example/feed.xml",
        "https://b.example/feed.xml",
    ]


def test_parse_opml_missing_file_returns_empty(tmp_path):
    assert sources.parse_opml(tmp_path / "nope.opml") == []


def test_normalize_entry_maps_fields():
    entry = {
        "title": " Hello ",
        "link": " https://x.example/a ",
        "summary": "blurb",
        "content": [{"value": "full body"}],
        "published": "2026-07-01",
    }
    a = sources._normalize_entry(entry, "Src")
    assert a == {
        "title": "Hello",
        "url": "https://x.example/a",
        "summary": "blurb",
        "body": "full body",
        "published": "2026-07-01",
        "source": "Src",
    }


def test_gather_feed_articles_dedups_by_url(monkeypatch, tmp_path):
    f = tmp_path / "feeds.opml"
    f.write_text(OPML)

    def fake_parse(url):
        return types.SimpleNamespace(
            feed={"title": f"Feed {url[-10:]}"},
            entries=[{"title": "Same", "link": "https://dup.example/1", "summary": "s"}],
        )

    monkeypatch.setattr(sources, "feedparser", types.SimpleNamespace(parse=fake_parse))
    profile = {"sources": {"opml": str(f), "rss": ["https://c.example/feed.xml"]}}
    arts = sources.gather_feed_articles(profile)
    # three feeds all yield the same URL → one article after dedup
    assert len(arts) == 1
    assert arts[0]["url"] == "https://dup.example/1"


def test_fetch_feed_swallows_errors(monkeypatch):
    def boom(url):
        raise RuntimeError("network down")

    monkeypatch.setattr(sources, "feedparser", types.SimpleNamespace(parse=boom))
    assert sources.fetch_feed("https://x.example/feed.xml") == []
