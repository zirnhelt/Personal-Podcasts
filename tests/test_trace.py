"""Tests for TRACE standard enforcement."""

import copy
import pytest

import trace as trace_mod
import config_loader


VALID = {
    "producer_url": "https://x/",
    "community": "Test community",
    "ai_generated": True,
    "ai_role": "primary",
    "ai_tools": ["Claude", "OpenAI TTS"],
    "track": "A",
    "disqualified": False,
    "scores": {
        "transparency": 4, "sourceIntegrity": 3, "displacement": 2,
        "consentAttribution": 4, "benefitFlow": 5, "accountability": 2,
    },
    "assessment_date": "2026-01-01",
    "assessed_by": "self",
}


def test_validate_computes_totals_and_verdict():
    t = trace_mod.validate_trace(copy.deepcopy(VALID))
    assert t["total_score"] == 20
    assert t["total_max"] == 20
    assert t["total_pct"] == 100
    assert t["verdict"] == "Exemplary"


def test_missing_trace_block_raises():
    with pytest.raises(ValueError, match="missing a required 'trace'"):
        trace_mod.validate_trace(None)


def test_missing_field_raises():
    bad = copy.deepcopy(VALID)
    del bad["producer_url"]
    with pytest.raises(ValueError, match="producer_url"):
        trace_mod.validate_trace(bad)


def test_wrong_categories_raise():
    bad = copy.deepcopy(VALID)
    del bad["scores"]["benefitFlow"]
    with pytest.raises(ValueError, match="scores must cover"):
        trace_mod.validate_trace(bad)


def test_score_out_of_range_raises():
    bad = copy.deepcopy(VALID)
    bad["scores"]["displacement"] = 9   # max is 2
    with pytest.raises(ValueError, match="displacement"):
        trace_mod.validate_trace(bad)


def test_assert_publishable_blocks_disqualified():
    profile = {"name": "p", "email": "x@y.z", "trace": {**copy.deepcopy(VALID), "disqualified": True}}
    with pytest.raises(ValueError, match="DISQUALIFIED"):
        trace_mod.assert_publishable(profile)


def test_assert_publishable_requires_contact():
    profile = {"name": "p", "trace": copy.deepcopy(VALID)}  # no email
    with pytest.raises(ValueError, match="accountability"):
        trace_mod.assert_publishable(profile)


def test_assert_publishable_passes_valid_profile():
    profile = {"name": "p", "email": "x@y.z", "trace": copy.deepcopy(VALID)}
    assert trace_mod.assert_publishable(profile)["total_score"] == 20


def test_build_xml_contains_namespace_scores():
    lines = "\n".join(trace_mod.build_trace_xml(copy.deepcopy(VALID), "Producer"))
    assert "<trace:assessment" in lines
    assert 'category="benefitFlow"' in lines
    assert "<trace:total" in lines


def test_shipped_profiles_are_publishable():
    """Every shipped profile must carry a valid, non-disqualified assessment."""
    for name in config_loader.list_podcast_profiles():
        config_loader.load_podcast_profile.cache_clear()
        profile = config_loader.load_podcast_profile(name)
        assert trace_mod.assert_publishable(profile)
