"""Parity test: every web UI's embedded constants must match Python.

Three files mirror config_loader/trace validation constants so browsers (and
the Cloudflare Worker) can validate without importing Python:

  - admin/index.html          <script type="application/json" id="py-parity">
  - webapp/public/index.html  same script tag
  - webapp/worker.js          /* py-parity */ const PY_PARITY = {...}; /* end py-parity */

Python remains the authority; this test fails the build if any mirror drifts.
"""

import json
import re
from pathlib import Path

import pytest

import config_loader
import trace

REPO = Path(__file__).resolve().parent.parent
MIRRORS = [
    REPO / "admin" / "index.html",
    REPO / "webapp" / "public" / "index.html",
    REPO / "webapp" / "worker.js",
]

_HTML_RE = re.compile(
    r'<script type="application/json" id="py-parity">(.*?)</script>', re.DOTALL)
_JS_RE = re.compile(
    r'/\* py-parity \*/\s*const PY_PARITY = (\{.*?\});\s*/\* end py-parity \*/', re.DOTALL)


def _parity_blob(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    match = (_JS_RE if path.suffix == ".js" else _HTML_RE).search(text)
    assert match, f"{path} is missing its py-parity blob"
    return json.loads(match.group(1))


@pytest.fixture(params=MIRRORS, ids=lambda p: str(p.relative_to(REPO)))
def blob(request) -> dict:
    return _parity_blob(request.param)


def test_profile_constants_match_config_loader(blob):
    assert blob["PROFILE_DEFAULTS"] == config_loader.PROFILE_DEFAULTS
    assert blob["PROFILE_ENUMS"] == {
        field: sorted(allowed)
        for field, allowed in config_loader._PROFILE_ENUMS.items()
    }
    assert blob["WEEKDAYS"] == sorted(config_loader._WEEKDAYS)
    # Range literals live inline in load_podcast_profile (config_loader.py:82).
    assert blob["LENGTH_RANGE"] == [5, 30]


def test_trace_constants_match_trace_module(blob):
    assert blob["TRACE_CATEGORIES"] == trace.TRACE_CATEGORIES
    assert blob["TRACE_TOTAL_MAX"] == trace.TRACE_TOTAL_MAX
    assert blob["TRACE_REQUIRED_FIELDS"] == list(trace._REQUIRED_FIELDS)


def test_verdict_thresholds_match_validate_trace(blob):
    """Check the mirrored thresholds against trace.validate_trace itself for
    every achievable total (0..20), instead of trusting a hardcoded copy."""
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


def test_all_mirrors_identical():
    """The three blobs must be byte-for-byte the same data, not just each
    individually valid — one drifting copy is exactly the bug this prevents."""
    blobs = [_parity_blob(p) for p in MIRRORS]
    assert blobs[0] == blobs[1] == blobs[2]
