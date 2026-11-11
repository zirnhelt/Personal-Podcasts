#!/usr/bin/env python3
"""
Configuration loader — loads shared config and per-show podcast profiles.

The multi-podcast creator is entirely config-driven: everything a show needs
lives in its profile (config/profiles/<name>.json) plus the shared host pack
(config/hosts.json) and prompt templates (config/prompts.json). No show
strings are hard-coded in the pipeline.
"""

import json
from functools import lru_cache
from pathlib import Path

CONFIG_DIR = Path(__file__).parent / "config"
PROFILES_DIR = CONFIG_DIR / "profiles"


@lru_cache(maxsize=1)
def load_hosts_config():
    """Load host personalities and settings (cached)."""
    with open(CONFIG_DIR / "hosts.json", 'r') as f:
        return json.load(f)


@lru_cache(maxsize=1)
def load_prompts_config():
    """Load shared Claude prompt templates (cached)."""
    with open(CONFIG_DIR / "prompts.json", 'r') as f:
        return json.load(f)


# MVP picker fields and their allowed values / defaults. Single source of truth
# for validation so the generator, tests, and CLI stay in sync.
PROFILE_DEFAULTS = {
    "scope": "regional",        # "regional" | "global"
    "cadence": "dialog",        # "news" | "dialog"
    "depth": "deep_dive",       # "titles" | "deep_dive"
    "length_minutes": 15,       # 5–30
    "schedule": "weekly",       # "daily" | "weekly"
    "schedule_day": "sunday",   # weekday name, used when schedule == "weekly"
    "voices": "cariboo_signals",
    "psa": False,               # include a community-spotlight PSA segment
}
_PROFILE_ENUMS = {
    "scope": {"regional", "global"},
    "cadence": {"news", "dialog"},
    "depth": {"titles", "deep_dive"},
    "schedule": {"daily", "weekly"},
}
# Kagi News (Kite) categories per profile: 8 × ~12 clusters ≈ 96 candidates,
# about three batched Haiku scoring calls. Mirrored in the web UIs' py-parity.
KITE_MAX_CATEGORIES = 8
_WEEKDAYS = {
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
}


@lru_cache(maxsize=None)
def load_podcast_profile(name="sample"):
    """Load a single podcast recipe from config/profiles/<name>.json (cached per name).

    A profile collapses every MVP picker (topic sources, scope, cadence, depth,
    length, schedule, voices) into one file so the same generator can drive many
    personal podcasts. Missing picker fields fall back to PROFILE_DEFAULTS;
    enum fields are validated so a typo fails fast rather than silently changing
    the episode format.
    """
    path = PROFILES_DIR / f"{name}.json"
    if not path.exists():
        available = ", ".join(p.stem for p in PROFILES_DIR.glob("*.json")) or "none"
        raise FileNotFoundError(
            f"No podcast profile '{name}' in {PROFILES_DIR} (available: {available})"
        )
    with open(path, "r") as f:
        profile = json.load(f)

    merged = {**PROFILE_DEFAULTS, **profile}
    for field, allowed in _PROFILE_ENUMS.items():
        if merged[field] not in allowed:
            raise ValueError(
                f"Profile '{name}' field '{field}'={merged[field]!r} "
                f"must be one of {sorted(allowed)}"
            )
    if not 5 <= int(merged["length_minutes"]) <= 30:
        raise ValueError(
            f"Profile '{name}' length_minutes={merged['length_minutes']} out of range 5–30"
        )
    if merged["schedule"] == "weekly" and merged["schedule_day"] not in _WEEKDAYS:
        raise ValueError(
            f"Profile '{name}' schedule_day={merged['schedule_day']!r} "
            f"must be a weekday name"
        )

    _validate_kite_sources(name, (merged.get("sources") or {}).get("kite"))

    # TRACE enforcement: no profile loads without a structurally valid assessment.
    # The publish gate (trace.assert_publishable) is applied by the generator
    # before an episode is written; here we fail fast on a malformed block.
    import trace as trace_mod
    merged["trace"] = trace_mod.validate_trace(merged.get("trace"))

    merged["name"] = name
    return merged


def _validate_kite_sources(name, kite):
    """sources.kite: unique non-empty strings, at most KITE_MAX_CATEGORIES, and —
    when the committed snapshot (config/kite_categories.json) exists — names Kite
    actually publishes, so a typo fails at load instead of silently fetching nothing.
    """
    if kite is None:
        return
    if not isinstance(kite, list) or not all(isinstance(c, str) and c.strip() for c in kite):
        raise ValueError(f"Profile '{name}' sources.kite must be a list of category names")
    if len(set(kite)) != len(kite):
        raise ValueError(f"Profile '{name}' sources.kite has duplicate categories")
    if len(kite) > KITE_MAX_CATEGORIES:
        raise ValueError(
            f"Profile '{name}' sources.kite has {len(kite)} categories "
            f"(max {KITE_MAX_CATEGORIES})"
        )
    import sources  # local import: sources imports nothing from here, keep it that way
    known = sources.load_kite_category_cache()
    unknown = sorted(set(kite) - known) if known is not None else []
    if unknown:
        raise ValueError(
            f"Profile '{name}' sources.kite has unknown categories {unknown} "
            f"(names must match kite.json exactly; if Kite added them, run "
            f"`python sources.py --refresh-kite-categories`)"
        )


def list_podcast_profiles():
    """Return the names of all available profiles (config/profiles/*.json)."""
    if not PROFILES_DIR.exists():
        return []
    return sorted(p.stem for p in PROFILES_DIR.glob("*.json"))


def get_voice_for_host(host_key):
    """Get TTS voice for a host."""
    return load_hosts_config()[host_key]["voice"]


def get_azure_voice_for_host(host_key):
    """Get Azure Neural TTS voice name for a host."""
    return load_hosts_config()[host_key]["azure_voice"]


def get_voice_instructions_for_host(host_key):
    """Get OpenAI TTS delivery/emotion instructions for a host."""
    return load_hosts_config()[host_key]["voice_instructions"]


def get_speed_for_host(host_key):
    """Get OpenAI TTS speed multiplier for a host (defaults to 1.0)."""
    return load_hosts_config()[host_key].get("speed", 1.0)


def message_text(response) -> str:
    """Concatenate all text blocks from an Anthropic message response.

    Reasoning-capable models (e.g. claude-sonnet-5) may lead with a
    ThinkingBlock, so response.content[0] is not guaranteed to be a text block.
    Join every text block instead of indexing, which crashes on non-text leads.
    """
    return "".join(block.text for block in response.content if block.type == "text")


if __name__ == "__main__":
    print("Available podcast profiles:")
    for prof_name in list_podcast_profiles():
        p = load_podcast_profile(prof_name)
        print(f"  - {prof_name}: {p['title']} "
              f"({p['scope']}/{p['cadence']}/{p['depth']}, "
              f"{p['length_minutes']} min, {p['schedule']})")
