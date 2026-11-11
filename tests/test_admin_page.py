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
import shutil
import subprocess
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
    assert blob["KITE_MAX_CATEGORIES"] == config_loader.KITE_MAX_CATEGORIES


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


# sources.kite inputs both validators must agree on (accept vs reject). Names
# are snapshot-known so Python's extra known-name check doesn't decide the case.
_KITE_CASES = [
    None, [], ["AI"], ["AI", "Canada"], "AI", ["AI", 3], ["AI", ""], ["AI", " "],
    ["AI", "AI"], {"AI": 1},
    ["AI", "Canada", "World", "USA", "Business", "Technology", "Science", "Sports"],
    ["AI", "Canada", "World", "USA", "Business", "Technology", "Science", "Sports", "Gaming"],
]


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_worker_kite_validation_matches_python():
    """Behavioural parity: run the worker's kiteSourceErrors under node."""
    script = (
        'const m = await import("./webapp/worker.js");'
        f"const cases = {json.dumps(_KITE_CASES)};"
        "console.log(JSON.stringify(cases.map((c) => m.kiteSourceErrors(c).length === 0)));"
    )
    out = subprocess.run(["node", "--input-type=module", "-e", script], cwd=REPO,
                         capture_output=True, text=True, check=True, timeout=30)
    js_ok = json.loads(out.stdout)

    def py_ok(kite) -> bool:
        try:
            config_loader._validate_kite_sources("parity", kite)
            return True
        except ValueError:
            return False

    assert js_ok == [py_ok(c) for c in _KITE_CASES]
    assert js_ok.count(True) == 5 and js_ok.count(False) == 7  # both branches exercised


def test_ui_pages_carry_the_same_kite_validator():
    """The two browser pages duplicate kiteSourceErrors; keep them identical."""
    fn = re.compile(r"function kiteSourceErrors\(kite\) \{.*?\n\}", re.DOTALL)
    bodies = [fn.search(p.read_text(encoding="utf-8")).group(0) for p in MIRRORS[:2]]
    worker = fn.search(MIRRORS[2].read_text(encoding="utf-8")).group(0)
    assert bodies[0] == bodies[1] == worker.replace("PY_PARITY.", "P.")
