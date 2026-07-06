#!/usr/bin/env python3
"""
Episode planning — translate a profile's MVP pickers into concrete generation
parameters. This is the seam podcast_generator.py reads instead of the old
day-of-week theme lookup.

Length is the genuinely new knob (the original show was fixed-length). We size
the script from spoken-word rate and derive an article budget from it.
"""

# Conversational two-host TTS lands around this many spoken words per minute.
# Tune against real rendered audio before trusting it.
WORDS_PER_MINUTE = 150

# Minutes of finished audio one article can carry, by depth. Titles-only skims
# many headlines; a deep dive spends real time per story.
_MINUTES_PER_ARTICLE = {"titles": 1.0, "deep_dive": 3.0}


def build_episode_plan(profile):
    """Return a plan dict derived from a validated profile.

    Keys:
        target_words   — script length target (length_minutes * WPM)
        article_count  — how many stories to select
        format         — "news" (roundup only) | "dialog" (deep-dive debate)
        fetch_bodies   — whether to fetch/enrich full article text
    """
    minutes = int(profile["length_minutes"])
    depth = profile["depth"]
    per_article = _MINUTES_PER_ARTICLE[depth]

    return {
        "target_words": minutes * WORDS_PER_MINUTE,
        "article_count": max(1, round(minutes / per_article)),
        "format": profile["cadence"],
        "fetch_bodies": depth == "deep_dive",
    }


if __name__ == "__main__":
    import sys
    from config_loader import load_podcast_profile

    name = sys.argv[1] if len(sys.argv) > 1 else "cariboo-signals"
    plan = build_episode_plan(load_podcast_profile(name))
    print(f"Episode plan for '{name}':")
    for k, v in plan.items():
        print(f"  {k}: {v}")
