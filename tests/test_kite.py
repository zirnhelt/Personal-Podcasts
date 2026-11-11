"""Kagi News (Kite) as a profile source.

Fixtures are trimmed real Kite files from 2026-11-10 (kite_ai.json,
kite_tech.json, kite_index.json). Kite content is CC BY-NC, credit Kagi News.
The network is stubbed: no test reaches kite.kagi.com.
"""

import copy
import json
from pathlib import Path

import pytest

import config_loader
import podcast_generator as pg
import sources

FIXTURES = Path(__file__).parent / "fixtures"
INDEX_URL = "https://news.kagi.com/kite.json"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


AI = _fixture("kite_ai.json")
TECH = _fixture("kite_tech.json")
INDEX = _fixture("kite_index.json")
BIG = AI["clusters"][0]          # 82 outlets, twin of Technology #2
REDIRECT_ONLY = AI["clusters"][1]  # every outlet link is a Google News redirect
MEDIUM = AI["clusters"][2]       # 18 outlets, no twin


class _Resp:
    def __init__(self, url: str, payload) -> None:
        self.url = url
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self):
        if isinstance(self._payload, str):
            raise ValueError("not JSON")
        return self._payload


@pytest.fixture
def kite_net(monkeypatch):
    """Route requests.get to the fixtures; tests edit `routes` to break things."""
    routes = {
        sources.KITE_INDEX_URL: INDEX,
        "https://news.kagi.com/ai.json": AI,
        "https://news.kagi.com/tech.json": TECH,
        "https://news.kagi.com/canada.json": {"clusters": []},
    }
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        payload = routes.get(url, RuntimeError(f"unrouted {url}"))
        if isinstance(payload, Exception):
            raise payload
        # kite.kagi.com redirects; category files resolve against the final URL
        return _Resp(INDEX_URL if url == sources.KITE_INDEX_URL else url, payload)

    monkeypatch.setattr(sources.requests, "get", fake_get)
    return routes, calls


def _profile(**sources_block) -> dict:
    return {"sources": sources_block}


# ── Parsing ─────────────────────────────────────────────────────────────────

def test_cluster_becomes_a_normal_article_with_outlets():
    a = sources._make_kite_article(BIG)
    assert set(a) >= {"title", "url", "summary", "body", "published", "source"}
    assert a["title"] == BIG["title"]
    # linked to a cited outlet's own page, never a Google News redirect
    assert a["url"].startswith("https://techcrunch.com/")
    assert a["source"] == "techcrunch.com"
    assert a["via"] == "Kagi News"
    assert a["_kite_sources"] == 82


def test_summary_is_stripped_and_body_keeps_outlets_inline():
    a = sources._make_kite_article(BIG)
    assert "[" not in a["summary"] and "#1]" not in a["summary"]
    assert "(cnbc.com, techcrunch.com)" in a["body"]          # claim → its outlets
    assert BIG["talking_points"][0].split(":")[0] in a["body"]  # talking points ride along


def test_original_outlet_list_is_kept_including_redirect_only_domains():
    a = sources._make_kite_article(BIG)
    names = [o["name"] for o in a["outlets"]]
    assert len(names) == len(set(names))
    assert {d["domain"] for d in BIG["articles"]} == set(names)
    for o in a["outlets"]:
        assert "news.google.com" not in o["url"]  # credited by name, link only if real


def test_future_publisher_dates_are_clamped():
    story = copy.deepcopy(MEDIUM)
    for art in story["articles"]:
        art["date"] = "2999-01-01T00:00:00+00:00"
    assert sources._make_kite_article(story)["published"] < "2999"


# ── Never mislead: thin clusters ───────────────────────────────────────────

def test_single_source_cluster_is_dropped():
    story = copy.deepcopy(MEDIUM)
    story["unique_domains"] = 1
    story["articles"] = story["articles"][:1]
    assert sources._make_kite_article(story) is None


def test_empty_summary_is_dropped():
    story = dict(MEDIUM, short_summary="   ")
    assert sources._make_kite_article(story) is None


def test_summary_of_only_citation_markers_is_dropped():
    story = dict(MEDIUM, short_summary="[techcrunch.com#1]")
    assert sources._make_kite_article(story) is None


def test_redirect_only_cluster_is_dropped():
    assert all("news.google.com" in a["link"] for a in REDIRECT_ONLY["articles"])
    assert sources._make_kite_article(REDIRECT_ONLY) is None


# ── gather_kite_articles ───────────────────────────────────────────────────

def test_gather_collapses_twins_and_drops_thin(kite_net):
    arts = sources.gather_kite_articles(_profile(kite=["AI", "Technology"]))
    titles = [a["title"] for a in arts]
    # AI#1 is Technology#2's twin: kept once, as the more widely covered telling
    assert BIG["title"] in titles
    assert TECH["clusters"][0]["title"] not in titles
    assert REDIRECT_ONLY["title"] not in titles
    assert MEDIUM["title"] in titles
    assert len(arts) == 2


def test_category_files_resolve_against_the_redirected_index(kite_net):
    _, calls = kite_net
    sources.gather_kite_articles(_profile(kite=["AI"]))
    assert calls == [sources.KITE_INDEX_URL, "https://news.kagi.com/ai.json"]


def test_no_kite_categories_means_no_requests(kite_net):
    _, calls = kite_net
    assert sources.gather_kite_articles(_profile(rss=[])) == []
    assert calls == []


# ── Failure degrades, never aborts ─────────────────────────────────────────

def test_index_failure_returns_empty(kite_net):
    routes, _ = kite_net
    routes[sources.KITE_INDEX_URL] = ConnectionError("down")
    assert sources.gather_kite_articles(_profile(kite=["AI"])) == []


def test_one_bad_category_keeps_the_others(kite_net):
    routes, _ = kite_net
    routes["https://news.kagi.com/ai.json"] = "<html>oops</html>"  # bad JSON
    arts = sources.gather_kite_articles(_profile(kite=["AI", "Technology"]))
    assert [a["title"] for a in arts] == [TECH["clusters"][0]["title"]]


def test_category_missing_from_todays_index_is_skipped(kite_net):
    arts = sources.gather_kite_articles(_profile(kite=["Smart Home", "AI"]))
    assert len(arts) == 2


def test_feed_gather_survives_a_kite_outage(kite_net, monkeypatch):
    routes, _ = kite_net
    routes[sources.KITE_INDEX_URL] = TimeoutError("slow")
    monkeypatch.setattr(sources, "fetch_feed", lambda url: [
        {"title": "Local story", "url": "https://local.example/a", "summary": "",
         "body": "", "published": "", "source": "Local"}])
    arts = sources.gather_feed_articles(_profile(rss=["https://local.example/feed"], kite=["AI"]))
    assert [a["title"] for a in arts] == ["Local story"]


def test_feed_gather_merges_kite_with_feeds(kite_net, monkeypatch):
    monkeypatch.setattr(sources, "fetch_feed", lambda url: [
        {"title": "Local story", "url": "https://local.example/a", "summary": "",
         "body": "", "published": "", "source": "Local"}])
    arts = sources.gather_feed_articles(_profile(rss=["https://local.example/feed"], kite=["AI"]))
    assert len(arts) == 3


# ── Validation ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("kite, message", [
    ("AI", "list of category names"),
    (["AI", 3], "list of category names"),
    (["AI", " "], "list of category names"),
    (["AI", "AI"], "duplicate"),
    ([f"c{i}" for i in range(config_loader.KITE_MAX_CATEGORIES + 1)], "max"),
    (["AI", "Artificial Intelligence"], "unknown categories"),
])
def test_validation_errors(kite, message):
    with pytest.raises(ValueError, match=message):
        config_loader._validate_kite_sources("t", kite)


def test_validation_accepts_known_names_and_absent_field():
    config_loader._validate_kite_sources("t", None)
    config_loader._validate_kite_sources("t", [])
    config_loader._validate_kite_sources("t", ["AI", "Canada", "Canada | Quebec"])


def test_unknown_names_pass_when_there_is_no_snapshot(monkeypatch):
    monkeypatch.setattr(sources, "load_kite_category_cache", lambda: None)
    config_loader._validate_kite_sources("t", ["Brand New Category"])


def test_load_podcast_profile_rejects_a_bad_kite_list(tmp_path, monkeypatch):
    sample = json.loads((config_loader.PROFILES_DIR / "sample.json").read_text())
    sample["sources"]["kite"] = ["AI", "AI"]
    (tmp_path / "bad.json").write_text(json.dumps(sample))
    monkeypatch.setattr(config_loader, "PROFILES_DIR", tmp_path)
    config_loader.load_podcast_profile.cache_clear()
    try:
        with pytest.raises(ValueError, match="duplicate"):
            config_loader.load_podcast_profile("bad")
    finally:
        config_loader.load_podcast_profile.cache_clear()


def test_category_cache_refresh_only_adds(tmp_path, monkeypatch):
    cache = tmp_path / "kite_categories.json"
    cache.write_text(json.dumps(["Retired Category"]))
    monkeypatch.setattr(sources, "KITE_CATEGORIES_CACHE", cache)
    monkeypatch.setattr(sources, "fetch_kite_index",
                        lambda: (INDEX_URL, {"AI": "ai.json", "Canada": "canada.json"}))
    assert sources.refresh_kite_category_cache() == 3
    assert json.loads(cache.read_text()) == ["AI", "Canada", "Retired Category"]


def test_committed_snapshot_covers_the_fixture_index():
    known = sources.load_kite_category_cache()
    assert known and {c["name"] for c in INDEX["categories"]} <= known


# ── TRACE: attribution and original-outlet citations ──────────────────────

@pytest.fixture
def kite_article():
    return sources._make_kite_article(BIG)


@pytest.fixture
def feed_article():
    return {"title": "Valley council approves trail plan", "url": "https://local.example/t",
            "summary": "s", "body": "", "published": "2026-11-10", "source": "Local Paper"}


@pytest.fixture
def profile():
    return config_loader.load_podcast_profile("sample")


def test_prompt_names_original_outlets_and_the_credit_rule(kite_article, feed_article):
    block = pg._sources_block([kite_article, feed_article])
    assert "Credit the outlet named in parentheses" in block
    assert "Kagi News summary of 82 outlets" in block
    names = [o["name"] for o in kite_article["outlets"]]
    assert f"Reported by: {', '.join(names[:5])}" in block
    many = dict(kite_article, outlets=[{"name": f"o{i}.example", "url": ""} for i in range(9)])
    assert "o4.example and 4 more" in pg._sources_block([many])


def test_prompt_unchanged_without_kite(feed_article):
    assert "Kagi News" not in pg._sources_block([feed_article])


def test_show_notes_and_citations_credit_kagi_and_outlets(tmp_path, monkeypatch, profile,
                                                          kite_article, feed_article):
    monkeypatch.setattr(pg, "show_podcasts_dir", lambda p: tmp_path)
    script = f"**RILEY:** {kite_article['title']} and {feed_article['title']}."
    path = pg.write_citations_file(profile, [kite_article, feed_article], script, "2026-11-11")
    data = json.loads(Path(path).read_text())

    assert data["attributions"] == ["Kagi News (CC BY-NC)"]
    notes = data["episode"]["description"]
    assert "Kagi News (CC BY-NC)" in notes
    assert kite_article["outlets"][0]["name"] in notes
    assert profile["email"] in notes                      # accountability intact
    assert "TRACE transparency" in notes                  # transparency intact

    arts = data["segments"]["deep_dive"]["articles"]
    kite_row = next(a for a in arts if a.get("via"))
    assert kite_row["outlets"] == kite_article["outlets"]  # the full original list
    assert kite_row["source"] == "techcrunch.com"
    assert "via" not in next(a for a in arts if not a.get("via"))


def test_no_kagi_credit_when_no_kite_story_was_cited(tmp_path, monkeypatch, profile, feed_article):
    monkeypatch.setattr(pg, "show_podcasts_dir", lambda p: tmp_path)
    path = pg.write_citations_file(profile, [feed_article], f"**RILEY:** {feed_article['title']}",
                                   "2026-11-11")
    data = json.loads(Path(path).read_text())
    assert "attributions" not in data
    assert "Kagi" not in data["episode"]["description"]


def test_spoken_disclosure_credits_kagi_only_when_used(profile, kite_article, feed_article):
    with_kite = pg.append_spoken_disclosure("**RILEY:** Hi.", profile, [kite_article])
    without = pg.append_spoken_disclosure("**RILEY:** Hi.", profile, [feed_article])
    assert "Story summaries from Kagi News." in with_kite
    assert "Kagi" not in without
    for out in (with_kite, without):                      # disclosure + contact survive
        assert profile["email"] in out


def test_deep_dive_uses_the_kite_body_without_fetching(monkeypatch, profile, kite_article):
    monkeypatch.setattr(pg, "_fetch_article_body",
                        lambda url: pytest.fail("Kite body must not be re-fetched"))
    monkeypatch.setattr(pg.search, "search_all",
                        lambda *a, **k: pytest.fail("Kite body must not be Brave-enriched"))
    pg.enrich_articles_with_bodies([kite_article], profile)
    assert kite_article["_body"].startswith(kite_article["body"][:50])
