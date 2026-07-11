"""Tests for the Brave/Kagi search layer."""

import types
import search


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_brave_mapping(monkeypatch):
    payload = {"web": {"results": [
        {"title": "T", "url": "https://x/1", "description": "D"},
    ]}}
    monkeypatch.setattr(search.requests, "get", lambda *a, **k: _Resp(payload))
    out = search.brave_search("q", "key")
    assert out == [{"title": "T", "url": "https://x/1", "description": "D"}]


def test_kagi_mapping_filters_non_results(monkeypatch):
    payload = {"data": [
        {"t": 0, "title": "T", "url": "https://x/1", "snippet": "S"},
        {"t": 1, "list": ["related", "searches"]},          # not a result
        {"t": 0, "title": "NoUrl"},                          # dropped, no url
    ]}
    monkeypatch.setattr(search.requests, "get", lambda *a, **k: _Resp(payload))
    out = search.kagi_search("q", "key")
    assert out == [{"title": "T", "url": "https://x/1", "description": "S"}]


def test_scoped_query_regional_vs_global():
    regional = {"scope": "regional", "region_hint": "Cariboo, BC"}
    glob = {"scope": "global", "region_hint": "Cariboo, BC"}
    assert search._scoped_query("broadband", regional) == "broadband Cariboo, BC"
    assert search._scoped_query("broadband", glob) == "broadband"


def test_search_all_merges_and_dedups(monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "b")
    monkeypatch.setenv("KAGI_API_KEY", "k")
    monkeypatch.setattr(search, "brave_search",
                        lambda q, key, count=5: [{"title": "A", "url": "https://dup/1", "description": ""}])
    monkeypatch.setattr(search, "kagi_search",
                        lambda q, key, count=5: [{"title": "A2", "url": "https://dup/1", "description": ""},
                                                 {"title": "B", "url": "https://x/2", "description": ""}])
    profile = {"search": {"brave": True, "kagi": True}}
    out = search.search_all("q", profile)
    assert [r["url"] for r in out] == ["https://dup/1", "https://x/2"]


def test_disabled_engine_is_skipped(monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "b")
    monkeypatch.setenv("KAGI_API_KEY", "k")
    called = {"brave": False, "kagi": False}
    monkeypatch.setattr(search, "brave_search",
                        lambda *a, **k: called.__setitem__("brave", True) or [])
    monkeypatch.setattr(search, "kagi_search",
                        lambda *a, **k: called.__setitem__("kagi", True) or [])
    search.search_all("q", {"search": {"brave": True, "kagi": False}})
    assert called == {"brave": True, "kagi": False}


def test_missing_key_skips_provider(monkeypatch):
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("KAGI_API_KEY", raising=False)
    # No keys → no providers run, no crash.
    assert search.search_all("q", {"search": {"brave": True, "kagi": True}}) == []


def test_search_all_caches_repeated_queries(monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "b")
    monkeypatch.delenv("KAGI_API_KEY", raising=False)
    calls = {"n": 0}

    def fake_brave(q, key, count=5):
        calls["n"] += 1
        return [{"title": "A", "url": "https://x/1", "description": ""}]

    monkeypatch.setattr(search, "brave_search", fake_brave)
    profile = {"search": {"brave": True, "kagi": False}}
    first = search.search_all("q", profile)
    second = search.search_all("q", profile)
    assert calls["n"] == 1
    assert first == second
    # Different count is a different cache entry
    search.search_all("q", profile, count=3)
    assert calls["n"] == 2


def test_brave_call_limit_skips_after_cap(monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_CALL_LIMIT", "1")
    monkeypatch.setenv("BRAVE_SEARCH_COOLDOWN_SECS", "0")
    payload = {"web": {"results": [{"title": "T", "url": "https://x/1", "description": "D"}]}}
    monkeypatch.setattr(search.requests, "get", lambda *a, **k: _Resp(payload))
    assert search.brave_search("q1", "key") != []
    assert search.brave_search("q2", "key") == []  # over the cap → skipped


def test_brave_pace_sleeps_within_cooldown(monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_COOLDOWN_SECS", "5")
    slept = []
    monkeypatch.setattr(search.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(search.time, "monotonic", lambda: 100.0)
    search._BRAVE_STATE["last_call"] = 98.0  # 2s ago, cooldown 5s → wait 3s
    search._brave_pace()
    assert slept == [3.0]


def test_gather_keyword_articles_shape(monkeypatch):
    monkeypatch.setattr(search, "search_all",
                        lambda q, p, count=5: [{"title": "T", "url": "https://x/1", "description": "D"}])
    profile = {"scope": "global", "sources": {"keywords": ["ai"]}}
    arts = search.gather_keyword_articles(profile)
    assert arts[0]["source"] == "search:ai"
    assert set(arts[0]) == {"title", "url", "summary", "body", "published", "source"}
