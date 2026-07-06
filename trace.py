#!/usr/bin/env python3
"""
TRACE standard enforcement (https://tracestandard.org/ns/trace/1.0).

Every podcast profile MUST carry a TRACE assessment. This module is the single
enforcement point:

  - validate_trace()      structural validation + total/percentage computation
  - assert_publishable()  the publish gate — refuses to generate an episode for
                          a disqualified show or one that fails to declare AI use
  - build_trace_xml()     the channel-level <trace:assessment> RSS block
  - spoken_disclosure()   a one-line audio disclosure for intro/outro

The six TRACE categories and their maximum scores are fixed by the standard.
"""

import xml.sax.saxutils as saxutils

NAMESPACE = "https://tracestandard.org/ns/trace/1.0"

# Fixed by the standard: category -> maximum score. Total max = 20.
TRACE_CATEGORIES = {
    "transparency": 4,
    "sourceIntegrity": 3,
    "displacement": 2,
    "consentAttribution": 4,
    "benefitFlow": 5,
    "accountability": 2,
}
TRACE_TOTAL_MAX = sum(TRACE_CATEGORIES.values())  # 20

# Fields a profile's trace block must supply (totals are computed, not required).
_REQUIRED_FIELDS = (
    "producer_url", "community", "ai_role", "ai_tools",
    "track", "assessment_date", "assessed_by", "scores",
)


def validate_trace(trace_cfg):
    """Validate a trace block and return it normalised with computed totals.

    Raises ValueError on any structural problem. Computes total_score,
    total_max, total_pct, and a verdict from the six category scores so the
    author only maintains the per-category numbers.
    """
    if not trace_cfg:
        raise ValueError("profile is missing a required 'trace' block (TRACE standard)")

    missing = [f for f in _REQUIRED_FIELDS if f not in trace_cfg]
    if missing:
        raise ValueError(f"trace block missing required fields: {', '.join(missing)}")

    scores = trace_cfg["scores"]
    if set(scores) != set(TRACE_CATEGORIES):
        raise ValueError(
            f"trace scores must cover exactly {sorted(TRACE_CATEGORIES)}; "
            f"got {sorted(scores)}"
        )

    total = 0
    for cat, max_score in TRACE_CATEGORIES.items():
        value = scores[cat]["score"] if isinstance(scores[cat], dict) else scores[cat]
        if not 0 <= value <= max_score:
            raise ValueError(f"trace score '{cat}'={value} out of range 0–{max_score}")
        total += value

    pct = round(100 * total / TRACE_TOTAL_MAX)
    verdict = (
        "Exemplary" if pct >= 85 else
        "Strong" if pct >= 70 else
        "Adequate" if pct >= 50 else
        "Weak"
    )

    normalized = dict(trace_cfg)
    normalized.update({
        "namespace": trace_cfg.get("namespace", NAMESPACE),
        "version": trace_cfg.get("version", "1.0"),
        "ai_generated": trace_cfg.get("ai_generated", True),
        "disqualified": bool(trace_cfg.get("disqualified", False)),
        "total_score": total,
        "total_max": TRACE_TOTAL_MAX,
        "total_pct": pct,
        "verdict": trace_cfg.get("verdict", verdict),
    })
    return normalized


def assert_publishable(profile):
    """Publish gate. Raise if this profile must not generate/publish an episode.

    Enforces the two non-negotiable TRACE conditions:
      1. Transparency — AI generation is declared, and a corrections/accountability
         contact exists so the show can be held to account.
      2. Not disqualified — a disqualified assessment blocks publication outright.
    """
    trace_cfg = validate_trace(profile.get("trace"))

    if trace_cfg["disqualified"]:
        raise ValueError(
            f"Profile '{profile.get('name', '?')}' has a DISQUALIFIED TRACE "
            "assessment — refusing to publish."
        )
    if not trace_cfg["ai_generated"]:
        raise ValueError(
            "TRACE transparency: ai_generated must be true for an AI-generated show."
        )
    if not (profile.get("email") or profile.get("corrections_contact")):
        raise ValueError(
            "TRACE accountability: profile needs an 'email' or 'corrections_contact' "
            "so listeners can request corrections."
        )
    return trace_cfg


def build_trace_xml(trace_cfg, producer_name):
    """Return the channel-level <trace:assessment> XML as a list of lines.

    Ported from podcast_generator._build_trace_channel_xml so the new repo has a
    single home for TRACE emission.
    """
    t = validate_trace(trace_cfg)
    lines = [f'<trace:assessment version="{t["version"]}">']
    lines.append(f'<trace:producer url="{t["producer_url"]}">{saxutils.escape(producer_name)}</trace:producer>')
    lines.append(f'<trace:community>{saxutils.escape(t["community"])}</trace:community>')
    lines.append(f'<trace:ai generated="{"true" if t["ai_generated"] else "false"}" role="{t["ai_role"]}">')
    for tool in t.get("ai_tools", []):
        lines.append(f'<trace:tool>{saxutils.escape(tool)}</trace:tool>')
    lines.append('</trace:ai>')
    lines.append(f'<trace:track>{saxutils.escape(t["track"])}</trace:track>')
    lines.append(f'<trace:disqualified>{"true" if t["disqualified"] else "false"}</trace:disqualified>')
    lines.append('<trace:scores>')
    for cat, max_score in TRACE_CATEGORIES.items():
        raw = t["scores"][cat]
        value = raw["score"] if isinstance(raw, dict) else raw
        lines.append(f'<trace:score category="{cat}" value="{value}" max="{max_score}"/>')
    lines.append('</trace:scores>')
    lines.append(f'<trace:total score="{t["total_score"]}" max="{t["total_max"]}" pct="{t["total_pct"]}"/>')
    lines.append(f'<trace:verdict>{saxutils.escape(t["verdict"])}</trace:verdict>')
    lines.append(f'<trace:assessmentDate>{t["assessment_date"]}</trace:assessmentDate>')
    lines.append(f'<trace:assessedBy>{saxutils.escape(t["assessed_by"])}</trace:assessedBy>')
    lines.append('</trace:assessment>')
    return lines


def spoken_disclosure(trace_cfg):
    """A one-sentence AI-generation disclosure for the episode intro/outro."""
    t = validate_trace(trace_cfg)
    tools = ", ".join(t.get("ai_tools", [])) or "AI tools"
    return (
        f"This episode is AI-generated using {tools}, produced under the TRACE "
        f"standard with a self-assessed transparency score of {t['total_score']} "
        f"out of {t['total_max']}."
    )
