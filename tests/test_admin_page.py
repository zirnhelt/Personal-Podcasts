"""Parity test: the admin page's embedded constants must match Python.

admin/index.html mirrors config_loader/trace validation constants in a
single JSON blob (<script id="py-parity">) so the browser UI can validate
without a server. Python remains the authority; this test fails the build
if the mirror drifts.
"""

import json
import re
from pathlib import Path

import config_loader
import trace

ADMIN_PAGE = Path(__file__).resolve().parent.parent / "admin" / "index.html"


def _parity_blob() -> dict:
    html = ADMIN_PAGE.read_text(encoding="utf-8")
    match = re.search(
        r'<script type="application/json" id="py-parity">(.*?)</script>',
        html,
        re.DOTALL,
    )
    assert match, "admin/index.html is missing the #py-parity JSON blob"
    return json.loads(match.group(1))


def test_profile_constants_match_config_loader():
    blob = _parity_blob()
    assert blob["PROFILE_DEFAULTS"] == config_loader.PROFILE_DEFAULTS
    assert blob["PROFILE_ENUMS"] == {
        field: sorted(allowed)
        for field, allowed in config_loader._PROFILE_ENUMS.items()
    }
    assert blob["WEEKDAYS"] == sorted(config_loader._WEEKDAYS)
    # Range literals live inline in load_podcast_profile (config_loader.py:82).
    assert blob["LENGTH_RANGE"] == [5, 30]


def test_trace_constants_match_trace_module():
    blob = _parity_blob()
    assert blob["TRACE_CATEGORIES"] == trace.TRACE_CATEGORIES
    assert blob["TRACE_TOTAL_MAX"] == trace.TRACE_TOTAL_MAX
    assert blob["TRACE_REQUIRED_FIELDS"] == list(trace._REQUIRED_FIELDS)


def test_verdict_thresholds_match_validate_trace():
    """Check the mirrored thresholds against trace.validate_trace itself for
    every achievable total (0..20), instead of trusting a hardcoded copy."""
    blob = _parity_blob()
    thresholds = blob["VERDICT_THRESHOLDS"]

    def js_verdict(total: int) -> str:
        pct = round(100 * total / blob["TRACE_TOTAL_MAX"])
        return next(label for cutoff, label in thresholds if pct >= cutoff)

    base = {
        "producer_url": "https://example.test/",
        "community": "test",
        "ai_role": "primary",
        "ai_tools": ["Claude"],
        "track": "B",
        "assessment_date": "2026-01-01",
        "assessed_by": "self",
    }
    for total in range(trace.TRACE_TOTAL_MAX + 1):
        remaining = total
        scores = {}
        for cat, cat_max in trace.TRACE_CATEGORIES.items():
            scores[cat] = min(remaining, cat_max)
            remaining -= scores[cat]
        verified = trace.validate_trace({**base, "scores": scores})
        assert js_verdict(total) == verified["verdict"], f"total={total}"
