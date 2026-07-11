"""Tests for the profile-driven generator: TRACE gates, parsing, publishing.

conftest.py stubs anthropic/openai/pydub/cohere/azure, so no API keys are
needed. These tests exercise the seams that enforce the TRACE standard and
the per-show output layout.
"""

import copy
import json
from datetime import datetime, timedelta

import pytest
from unittest.mock import MagicMock

import podcast_generator as pg
import config_loader
import trace as trace_mod


VALID_TRACE = {
    "producer_url": "https://x/", "community": "c", "ai_generated": True,
    "ai_role": "primary", "ai_tools": ["Claude", "OpenAI TTS"], "track": "A",
    "disqualified": False,
    "scores": {"transparency": 4, "sourceIntegrity": 3, "displacement": 2,
               "consentAttribution": 4, "benefitFlow": 5, "accountability": 2},
    "assessment_date": "2026-01-01", "assessed_by": "self",
}


def _profile(tmp_path=None, **overrides):
    profile = {
        "name": "testshow",
        "title": "Test Show",
        "tagline": "A test podcast",
        "sources": {"keywords": ["testing"]},
        "scope": "global", "region_hint": "",
        "cadence": "dialog", "depth": "deep_dive", "length_minutes": 15,
        "schedule": "weekly", "schedule_day": "sunday",
        "voices": "cariboo_signals", "psa": False,
        "base_url": "https://example.test/shows/testshow/",
        "author": "Tester", "email": "corrections@example.test",
        "trace": trace_mod.validate_trace(copy.deepcopy(VALID_TRACE)),
    }
    profile.update(overrides)
    return profile


@pytest.fixture
def show_in_tmp(tmp_path, monkeypatch):
    """Point the generator's shows/ root at a temp dir."""
    monkeypatch.setattr(pg, "SHOWS_DIR", tmp_path / "shows")
    return tmp_path / "shows"


# ── Publish gate ────────────────────────────────────────────────────────────

class TestPublishGate:
    def test_disqualified_profile_aborts_before_any_write(self, show_in_tmp, monkeypatch):
        profile = _profile()
        profile["trace"] = trace_mod.validate_trace(
            {**copy.deepcopy(VALID_TRACE), "disqualified": True})
        monkeypatch.setattr(config_loader, "load_podcast_profile", lambda n: profile)
        monkeypatch.setattr(pg, "load_podcast_profile", lambda n: profile)

        with pytest.raises(ValueError, match="DISQUALIFIED"):
            pg.generate_episode("testshow")
        # Nothing was written for the show
        assert not (show_in_tmp / "testshow").exists()

    def test_missing_corrections_contact_aborts(self, show_in_tmp, monkeypatch):
        profile = _profile()
        del profile["email"]
        monkeypatch.setattr(pg, "load_podcast_profile", lambda n: profile)
        with pytest.raises(ValueError, match="accountability"):
            pg.generate_episode("testshow")
        assert not (show_in_tmp / "testshow").exists()


# ── Idempotency ─────────────────────────────────────────────────────────────

class TestIdempotency:
    def test_weekly_period_detects_this_weeks_episode(self, show_in_tmp):
        profile = _profile()
        pdir = pg.show_podcasts_dir(profile)
        pdir.mkdir(parents=True)
        today = datetime.now()
        (pdir / f"citations_{today.strftime('%Y-%m-%d')}.json").write_text("{}")
        assert pg.episode_exists_for_period(profile, today) is True

    def test_weekly_period_ignores_last_week(self, show_in_tmp):
        profile = _profile()
        pdir = pg.show_podcasts_dir(profile)
        pdir.mkdir(parents=True)
        last_week = datetime.now() - timedelta(days=8)
        (pdir / f"citations_{last_week.strftime('%Y-%m-%d')}.json").write_text("{}")
        assert pg.episode_exists_for_period(profile, datetime.now()) is False

    def test_daily_period_is_per_day(self, show_in_tmp):
        profile = _profile(schedule="daily")
        pdir = pg.show_podcasts_dir(profile)
        pdir.mkdir(parents=True)
        yesterday = datetime.now() - timedelta(days=1)
        (pdir / f"citations_{yesterday.strftime('%Y-%m-%d')}.json").write_text("{}")
        assert pg.episode_exists_for_period(profile, datetime.now()) is False


# ── Script parsing ──────────────────────────────────────────────────────────

SCRIPT = """
**RILEY:** Welcome to the show, I'm Riley.

**CASEY:** And I'm Casey. Today we're digging into rural broadband.

**DEEP DIVE**

**RILEY:** Let's start with the milestone announcement from the coverage in Example News.

**CASEY:** [pause:500] Right — and according to Example News, the rollout reached ten thousand homes.
"""


class TestScriptParsing:
    def test_sections_and_speakers(self):
        segments = pg.parse_script_into_segments(SCRIPT)
        assert len(segments["welcome"]) == 2
        assert len(segments["deep_dive"]) == 2
        assert segments["welcome"][0]["speaker"] == "riley"
        assert segments["deep_dive"][1]["speaker"] == "casey"

    def test_pacing_tag_extracted(self):
        segments = pg.parse_script_into_segments(SCRIPT)
        assert segments["deep_dive"][1]["gap_ms"] == 500
        assert "[pause" not in segments["deep_dive"][1]["text"]

    def test_news_marker_routes_to_news_section(self):
        script = SCRIPT.replace("**DEEP DIVE**", "**NEWS ROUNDUP**")
        segments = pg.parse_script_into_segments(script)
        assert len(segments["news"]) == 2
        assert segments["deep_dive"] == []


# ── Spoken disclosure (TRACE obligation 4) ──────────────────────────────────

class TestSpokenDisclosure:
    def test_disclosure_appended_as_host_line(self):
        profile = _profile()
        out = pg.append_spoken_disclosure("**RILEY:** Hello.", profile)
        disclosure = trace_mod.spoken_disclosure(profile["trace"])
        assert disclosure in out
        assert profile["email"] in out
        # It must be a speaker line so every TTS path renders it
        last_line = out.strip().splitlines()[-1]
        assert last_line.startswith("**")

    def test_disclosure_survives_parsing_into_segments(self):
        profile = _profile()
        script = pg.append_spoken_disclosure(SCRIPT, profile)
        segments = pg.parse_script_into_segments(script)
        all_text = " ".join(s["text"] for sec in segments.values() for s in sec)
        assert "AI-generated" in all_text


# ── Citations (TRACE obligation 5) ──────────────────────────────────────────

class TestCitations:
    def test_citations_file_written_with_articles_and_contact(self, show_in_tmp):
        profile = _profile()
        articles = [
            {"title": "Milestone announcement reached today", "url": "https://x/1",
             "source": "Example News", "published": "2026-07-05"},
        ]
        path = pg.write_citations_file(profile, articles, SCRIPT, "2026-07-05")
        data = json.loads(open(path).read())
        assert data["episode"]["date"] == "2026-07-05"
        assert data["episode"]["corrections_contact"] == profile["email"]
        assert data["segments"]["deep_dive"]["articles"][0]["url"] == "https://x/1"
        # Show notes surface sources and the corrections contact
        assert "https://x/1" in data["episode"]["description"]
        assert profile["email"] in data["episode"]["description"]
        assert "TRACE" in data["episode"]["description"]


# ── RSS emission (TRACE obligations 3 + 6) ──────────────────────────────────

class TestRssFeed:
    def _write_episode(self, profile, date_str):
        pdir = pg.show_podcasts_dir(profile)
        pdir.mkdir(parents=True, exist_ok=True)
        pg.write_citations_file(
            profile,
            [{"title": "Story", "url": "https://x/1", "source": "S", "published": ""}],
            "**RILEY:** Story words here.", date_str)
        # Citations exist but no local mp3: cache size metadata so the feed
        # can include the episode (mp3s aren't always checked out locally).
        cit = pdir / f"citations_{date_str}.json"
        data = json.loads(cit.read_text())
        data["episode"]["audio_file_size"] = 12345
        data["episode"]["audio_duration"] = "15:00"
        cit.write_text(json.dumps(data))

    def test_feed_contains_trace_block_and_namespace(self, show_in_tmp):
        profile = _profile()
        self._write_episode(profile, "2026-07-05")
        feed_path = pg.generate_show_rss_feed(profile)
        xml = open(feed_path).read()
        assert 'xmlns:trace="https://tracestandard.org/ns/trace/1.0"' in xml
        assert "<trace:assessment" in xml
        assert 'category="sourceIntegrity"' in xml
        assert "<trace:verdict>" in xml

    def test_feed_contains_corrections_contact_and_enclosure(self, show_in_tmp):
        profile = _profile()
        self._write_episode(profile, "2026-07-05")
        xml = open(pg.generate_show_rss_feed(profile)).read()
        assert profile["email"] in xml
        assert 'enclosure url="https://example.test/shows/testshow/podcasts/podcast_audio_2026-07-05.mp3"' in xml
        assert 'length="12345"' in xml

    def test_index_html_discloses_trace_and_contact(self, show_in_tmp):
        profile = _profile()
        self._write_episode(profile, "2026-07-05")
        html = open(pg.generate_show_index_html(profile)).read()
        assert "AI-generated" in html
        assert profile["email"] in html
        assert "20/20" in html


# ── System prompt (TRACE obligation 5: editorial rules) ────────────────────

class TestSystemPrompt:
    def test_prompt_instructs_citation_and_hedging(self):
        profile = _profile()
        plan = {"target_words": 2250, "article_count": 5,
                "format": "dialog", "fetch_bodies": True}
        prompt = pg.build_system_prompt(profile, plan)
        low = prompt.lower()
        assert "cite" in low
        assert "hedge" in low
        assert "attribute" in low or "attribution" in low
        assert "never fabricate" in low
        assert str(plan["target_words"]) in prompt
        assert profile["email"] in prompt

    def test_news_cadence_uses_roundup_structure(self):
        profile = _profile(cadence="news", depth="titles")
        plan = {"target_words": 750, "article_count": 5,
                "format": "news", "fetch_bodies": False}
        prompt = pg.build_system_prompt(profile, plan)
        assert "NEWS ROUNDUP" in prompt
        assert "DEEP DIVE" not in prompt

    def test_psa_details_included_when_provided(self):
        profile = _profile(psa=True)
        plan = {"target_words": 2250, "article_count": 5,
                "format": "dialog", "fetch_bodies": True}
        psa_info = {"org_name": "Food Bank", "org_description": "Feeds people",
                    "org_website": "https://fb.example", "psa_angle": None}
        prompt = pg.build_system_prompt(profile, plan, psa_info=psa_info)
        assert "COMMUNITY SPOTLIGHT" in prompt
        assert "Food Bank" in prompt


# ── Script generation wiring ────────────────────────────────────────────────

class TestScriptGeneration:
    def _client(self, text="**RILEY:** Hi. **CASEY:** Hello."):
        response = MagicMock()
        response.content = [MagicMock(type="text", text=text)]
        response.usage = MagicMock(input_tokens=10, output_tokens=10)
        client = MagicMock()
        client.messages.create.return_value = response
        return client

    def test_sources_and_evolving_context_reach_the_prompt(self):
        profile = _profile()
        plan = {"target_words": 2250, "article_count": 1,
                "format": "dialog", "fetch_bodies": True}
        client = self._client()
        articles = [{"title": "Big story", "url": "https://x/1",
                     "source": "Example News", "summary": "sum", "published": ""}]
        pg.generate_episode_script(profile, plan, articles,
                                   evolving_context="**EVOLVING STORIES** note",
                                   client=client)
        kwargs = client.messages.create.call_args.kwargs
        user_prompt = kwargs["messages"][0]["content"]
        assert "Big story" in user_prompt
        assert "EVOLVING STORIES" in user_prompt
        assert "cite" in kwargs["system"].lower()

    def test_polish_falls_back_when_format_broken(self):
        profile = _profile()
        client = self._client(text="no speaker tags at all")
        articles = []
        original = "**RILEY:** Hi.\n**CASEY:** Hello."
        assert pg.polish_script(original, profile, articles, client=client) == original


class TestApiTotals:
    def test_log_api_call_accumulates_numeric_counts(self, capsys):
        pg._API_TOTALS.clear()
        pg._log_api_call("claude-script", "input_tokens", 100)
        pg._log_api_call("claude-script", "input_tokens", 50)
        pg._log_api_call("claude-polish", "input_tokens", "?")  # unknown usage
        assert pg._API_TOTALS["claude-script"] == {"calls": 2, "input_tokens": 150}
        assert pg._API_TOTALS["claude-polish"] == {"calls": 1}
        pg._print_api_totals()
        out = capsys.readouterr().out
        assert "[api totals]" in out
        assert "claude-script calls=2 input_tokens=150" in out

    def test_print_api_totals_silent_when_empty(self, capsys):
        pg._API_TOTALS.clear()
        pg._print_api_totals()
        assert "[api totals]" not in capsys.readouterr().out

    def test_generate_episode_resets_totals_and_prints_summary(
            self, monkeypatch, capsys):
        pg._API_TOTALS["stale-service"] = {"calls": 9}

        def fake_run(profile_name, force=False, dry_run=False):
            pg._log_api_call("claude-haiku", "input_tokens", 42)
            return True

        monkeypatch.setattr(pg, "_generate_episode", fake_run)
        assert pg.generate_episode("sample", dry_run=True) is True
        assert "stale-service" not in pg._API_TOTALS
        out = capsys.readouterr().out
        assert "claude-haiku calls=1 input_tokens=42" in out
