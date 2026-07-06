"""Tests for the multi-podcast profile loader in config_loader.py."""

import json
import pytest

import config_loader


@pytest.fixture(autouse=True)
def _clear_cache():
    """load_podcast_profile is lru_cached; reset between tests."""
    config_loader.load_podcast_profile.cache_clear()
    yield
    config_loader.load_podcast_profile.cache_clear()


# A minimal valid TRACE block — required on every profile since enforcement.
_TRACE = {
    "producer_url": "https://x/", "community": "c", "ai_generated": True,
    "ai_role": "primary", "ai_tools": ["Claude"], "track": "A", "disqualified": False,
    "scores": {"transparency": 4, "sourceIntegrity": 3, "displacement": 2,
               "consentAttribution": 4, "benefitFlow": 5, "accountability": 2},
    "assessment_date": "2026-01-01", "assessed_by": "self",
}


def _write_profile(tmp_path, monkeypatch, name, data):
    profiles = tmp_path / "profiles"
    profiles.mkdir(exist_ok=True)
    data.setdefault("trace", _TRACE)   # supply TRACE unless the test overrides it
    (profiles / f"{name}.json").write_text(json.dumps(data))
    monkeypatch.setattr(config_loader, "PROFILES_DIR", profiles)
    return profiles


def test_loads_shipped_sample_profile():
    p = config_loader.load_podcast_profile("sample")
    assert p["title"] == "Sample Signals"
    assert p["cadence"] == "dialog"
    assert p["name"] == "sample"
    assert p["email"]  # accountability contact must ship with the sample


def test_defaults_fill_missing_pickers(tmp_path, monkeypatch):
    _write_profile(tmp_path, monkeypatch, "minimal", {"title": "X", "sources": {}})
    p = config_loader.load_podcast_profile("minimal")
    assert p["scope"] == config_loader.PROFILE_DEFAULTS["scope"]
    assert p["length_minutes"] == config_loader.PROFILE_DEFAULTS["length_minutes"]
    assert p["schedule_day"] == "sunday"
    assert p["psa"] is False


def test_invalid_enum_raises(tmp_path, monkeypatch):
    _write_profile(tmp_path, monkeypatch, "bad", {"title": "X", "cadence": "monologue"})
    with pytest.raises(ValueError, match="cadence"):
        config_loader.load_podcast_profile("bad")


@pytest.mark.parametrize("minutes", [4, 31, 0])
def test_length_out_of_range_raises(tmp_path, monkeypatch, minutes):
    _write_profile(tmp_path, monkeypatch, "len", {"title": "X", "length_minutes": minutes})
    with pytest.raises(ValueError, match="length_minutes"):
        config_loader.load_podcast_profile("len")


def test_bad_schedule_day_raises(tmp_path, monkeypatch):
    _write_profile(tmp_path, monkeypatch, "day",
                   {"title": "X", "schedule": "weekly", "schedule_day": "someday"})
    with pytest.raises(ValueError, match="schedule_day"):
        config_loader.load_podcast_profile("day")


def test_missing_trace_block_refuses_to_load(tmp_path, monkeypatch):
    profiles = tmp_path / "profiles"
    profiles.mkdir(exist_ok=True)
    (profiles / "notrace.json").write_text(json.dumps({"title": "X"}))
    monkeypatch.setattr(config_loader, "PROFILES_DIR", profiles)
    with pytest.raises(ValueError, match="trace"):
        config_loader.load_podcast_profile("notrace")


def test_unknown_profile_raises_with_available_list(tmp_path, monkeypatch):
    _write_profile(tmp_path, monkeypatch, "real", {"title": "X"})
    with pytest.raises(FileNotFoundError, match="real"):
        config_loader.load_podcast_profile("ghost")


def test_list_profiles(tmp_path, monkeypatch):
    _write_profile(tmp_path, monkeypatch, "a", {"title": "A"})
    _write_profile(tmp_path, monkeypatch, "b", {"title": "B"})
    assert config_loader.list_podcast_profiles() == ["a", "b"]
