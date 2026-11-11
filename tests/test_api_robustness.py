"""API robustness fixes ported from curated-podcast-generator.

Stub clients only — no network, no API keys, no spend.
"""

from unittest.mock import MagicMock

import pytest

import config_loader
import dedup_articles
import podcast_generator as pg
import ranking

PLAN = {"target_words": 750, "article_count": 1, "format": "dialog", "fetch_bodies": False}
ARTICLES = [{"title": "Big story", "url": "https://x.example/1", "source": "Example News",
             "summary": "sum", "published": ""}]
GOOD = "**RILEY:** Hi there.\n**CASEY:** Hello back."


def _response(text=GOOD, stop_reason="end_turn"):
    r = MagicMock()
    r.content = [MagicMock(type="text", text=text)]
    r.usage = MagicMock(input_tokens=10, output_tokens=10)
    r.stop_reason = stop_reason
    return r


def _client(*responses):
    client = MagicMock()
    client.messages.create.side_effect = list(responses)
    return client


@pytest.fixture
def profile():
    return config_loader.load_podcast_profile("sample")


# ── 1. Truncated / refused output never airs ──────────────────────────────

def test_script_retries_once_on_truncation_with_more_room_and_low_effort(profile):
    client = _client(_response("**RILEY:** Hi", "max_tokens"), _response())
    assert pg.generate_episode_script(profile, PLAN, ARTICLES, client=client) == GOOD
    first, second = (c.kwargs for c in client.messages.create.call_args_list)
    assert second["max_tokens"] > first["max_tokens"]
    assert second["output_config"] == {"effort": "low"}  # SCRIPT_MODEL is Sonnet 5


def test_script_truncated_twice_is_not_published(profile):
    client = _client(_response("a", "max_tokens"), _response("b", "max_tokens"))
    assert pg.generate_episode_script(profile, PLAN, ARTICLES, client=client) is None


def test_script_refusal_is_not_published(profile):
    client = _client(_response("", "refusal"))
    assert pg.generate_episode_script(profile, PLAN, ARTICLES, client=client) is None
    assert client.messages.create.call_count == 1


def test_script_budget_leaves_room_for_thinking(profile):
    client = _client(_response())
    pg.generate_episode_script(profile, PLAN, ARTICLES, client=client)
    # 750 words ≈ 1.1K tokens of script; the old floor (4000) left thinking ~3K.
    assert client.messages.create.call_args.kwargs["max_tokens"] >= 8000


@pytest.mark.parametrize("stop", ["max_tokens", "refusal"])
def test_cut_off_polish_falls_back_to_raw_script(profile, stop):
    # Both host tags present, so the old format check would have accepted it.
    half = "**RILEY:** Hi there.\n**CASEY:** Hel"
    client = _client(_response(half, stop))
    assert pg.polish_script(GOOD, profile, ARTICLES, client=client) == GOOD


def test_clean_polish_is_used(profile):
    polished = "**RILEY:** Hi, polished.\n**CASEY:** Hello, polished."
    client = _client(_response(polished))
    assert pg.polish_script(GOOD, profile, ARTICLES, client=client) == polished


# ── Streaming above the SDK's non-streaming ceiling ────────────────────────

def test_send_claude_streams_large_budgets_only():
    client = MagicMock()
    final = _response()
    client.messages.stream.return_value.__enter__.return_value.get_final_message.return_value = final
    assert pg.send_claude(client, model="m", max_tokens=32000, messages=[]) is final
    client.messages.create.assert_not_called()

    pg.send_claude(client, model="m", max_tokens=pg.STREAM_ABOVE_MAX_TOKENS, messages=[])
    client.messages.create.assert_called_once()


# ── 2. Haiku 5.5 for small calls, thinking off per model ───────────────────

@pytest.mark.parametrize("model, expected", [
    ("claude-haiku-5-5", {"thinking": {"type": "disabled"}}),
    ("claude-sonnet-5", {"thinking": {"type": "disabled"}}),
    ("claude-sonnet-5-5", {"thinking": {"type": "between_tools"}}),
    ("claude-opus-5-5", {"output_config": {"effort": "low"}}),
    ("claude-fable-5-2", {"output_config": {"effort": "low"}}),
    ("claude-haiku-4-5-20251001", {}),
])
def test_small_call_params_follow_the_model(model, expected):
    assert config_loader.small_call_params(model) == expected


def test_scoring_and_clustering_default_to_haiku_5_5():
    assert config_loader.HAIKU_MODEL == "claude-haiku-5-5"
    assert ranking.SCORING_MODEL == config_loader.HAIKU_MODEL


def test_scoring_call_turns_thinking_off(profile):
    client = _client(_response('{"scores": [{"index": 0, "score": 7}]}'))
    assert ranking._haiku_relevance_scores(ARTICLES, profile, client) == [7]
    kwargs = client.messages.create.call_args.kwargs
    assert kwargs["thinking"] == {"type": "disabled"}


# ── 3. JSON replies with fences or trailing prose still parse ─────────────

@pytest.mark.parametrize("reply", [
    '{"scores": [{"index": 0, "score": 7}]}',
    '```json\n{"scores": [{"index": 0, "score": 7}]}\n```',
    '{"scores": [{"index": 0, "score": 7}]}\nHope this helps!',
])
def test_scoring_tolerates_fences_and_trailing_prose(profile, reply):
    client = _client(_response(reply))
    assert ranking._haiku_relevance_scores(ARTICLES, profile, client) == [7]


def test_clustering_tolerates_trailing_prose():
    arts = [{"title": f"Story {i}", "url": f"https://x.example/{i}", "summary": "",
             "ai_score": 5} for i in range(3)]
    reply = '{"clusters": [{"label": "same", "indices": [0, 1]}]}\nThat is all.'
    client = _client(_response(reply))
    out = dedup_articles.cluster_and_rescore_corpus(arts, "topic", client=client)
    assert [a.get("_cluster_suppressed", False) for a in out] == [False, True, False]
    assert client.messages.create.call_args.kwargs["model"] == config_loader.HAIKU_MODEL


def test_parse_json_object_rejects_non_objects():
    for bad in ("", "no json", "[1, 2]"):
        with pytest.raises(ValueError):
            config_loader.parse_json_object(bad)


# ── 4a. Feed-supplied URLs can't reach private addresses ──────────────────

@pytest.mark.parametrize("url", [
    "http://127.0.0.1/admin", "http://127.1/", "http://localhost:8080/",
    "http://169.254.169.254/latest/meta-data/", "http://10.0.0.5/", "http://[::1]/",
    "file:///etc/passwd", "http://user:pw@example.com/", "http://printer.local/",
])
def test_non_public_urls_are_refused(url, monkeypatch):
    monkeypatch.setattr(pg.requests, "get", lambda *a, **k: pytest.fail("must not fetch"))
    assert pg._fetch_article_body(url) == ""


def test_public_name_resolving_privately_is_refused(monkeypatch):
    monkeypatch.setattr(pg.socket, "getaddrinfo",
                        lambda *a, **k: [(None, None, None, None, ("10.1.2.3", 0))])
    monkeypatch.setattr(pg.requests, "get", lambda *a, **k: pytest.fail("must not fetch"))
    assert pg._fetch_article_body("https://innocent.example/a") == ""


def test_redirect_into_a_private_address_is_refused(monkeypatch):
    monkeypatch.setattr(pg.socket, "getaddrinfo",
                        lambda *a, **k: [(None, None, None, None, ("93.184.216.34", 0))])
    hop = MagicMock(is_redirect=True, headers={"location": "http://127.0.0.1/secret"})
    calls = []
    monkeypatch.setattr(pg.requests, "get", lambda url, **k: calls.append(url) or hop)
    assert pg._get_public("https://public.example/a", timeout=1) is None
    assert calls == ["https://public.example/a"]  # the private hop was never requested


def test_public_url_is_fetched(monkeypatch):
    monkeypatch.setattr(pg.socket, "getaddrinfo",
                        lambda *a, **k: [(None, None, None, None, ("93.184.216.34", 0))])
    page = MagicMock(is_redirect=False, text="<p>" + "Real article text. " * 40 + "</p>")
    monkeypatch.setattr(pg.requests, "get", lambda url, **k: page)
    assert pg._fetch_article_body("https://public.example/a").startswith("Real article text.")


# ── 4b. Spend-cap preflight ────────────────────────────────────────────────

class _ApiError(Exception):
    pass


@pytest.mark.parametrize("message, code", [
    ("Error 400: You have reached your specified API usage limits. You will regain "
     "access on 2026-12-01 at 00:00 UTC.", pg.EXIT_USAGE_LIMIT),
    ("Error 400: Your credit balance is too low to access the Anthropic API.",
     pg.EXIT_CREDITS_EXHAUSTED),
])
def test_preflight_exits_on_a_money_wall(message, code):
    client = MagicMock()
    client.messages.create.side_effect = _ApiError(message)
    with pytest.raises(SystemExit) as exc:
        pg.preflight_anthropic(client)
    assert exc.value.code == code


def test_preflight_is_tiny_and_inconclusive_errors_continue():
    client = MagicMock()
    pg.preflight_anthropic(client)
    kwargs = client.messages.create.call_args.kwargs
    assert kwargs["max_tokens"] == 1 and kwargs["model"] == config_loader.HAIKU_MODEL

    client.messages.create.side_effect = _ApiError("503 overloaded")
    pg.preflight_anthropic(client)  # no SystemExit


def test_api_retry_does_not_back_off_on_a_money_wall(monkeypatch):
    monkeypatch.setattr(pg.time, "sleep", lambda s: pytest.fail("no backoff on a wall"))
    calls = []

    def boom():
        calls.append(1)
        raise _ApiError("429 Your credit balance is too low")

    with pytest.raises(_ApiError):
        pg.api_retry(boom)
    assert len(calls) == 1
