#!/usr/bin/env python3
"""
Personal Podcast Creator — profile-driven episode pipeline.

One codebase, many independent AI podcasts: every show is defined by a single
profile in config/profiles/<name>.json. The pipeline is:

    load profile → TRACE publish gate → episode plan → ingest (feeds + search)
    → rank/select → optional body enrichment → script generation → polish
    → TTS + audio assembly → citations + transcripts → RSS with TRACE block

TRACE (https://tracestandard.org/ns/trace/1.0) is enforced, not optional:
  - a profile without a valid TRACE block will not even load (config_loader)
  - trace.assert_publishable() runs before anything is generated or written
  - the RSS carries a channel-level <trace:assessment> block
  - the episode audio carries a spoken AI-generation disclosure
  - citations are written per episode and surfaced in show notes
  - the corrections contact is published in the feed and show notes

Adapted from the Cariboo Signals daily pipeline; audio assembly, TTS handling,
and RSS emission are ports of that show's proven code.
"""

import argparse
import glob
import json
import os
import re
import sys
import tempfile
import time
import xml.sax.saxutils as saxutils
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from string import Template

import requests

from config_loader import (
    load_podcast_profile,
    list_podcast_profiles,
    load_hosts_config,
    load_prompts_config,
    get_voice_for_host,
    get_speed_for_host,
    message_text,
)
import trace as trace_mod
import sources
import search
import ranking
from episode_plan import build_episode_plan
from psa_selector import select_psa
from azure_tts import (
    generate_azure_tts_for_section,
    PRONUNCIATION_DICT as AZURE_PRONUNCIATION_DICT,
    get_azure_speech_config,
)

try:
    from anthropic import Anthropic
    from openai import OpenAI
    from pydub import AudioSegment
except ImportError as e:
    print(f"⚠️  Missing required library: {e}")
    print("Please install with: pip install anthropic openai pydub")
    print("Also ensure ffmpeg is installed for audio processing")
    sys.exit(1)

# ── Configuration ───────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).parent
SHOWS_DIR = SCRIPT_DIR / "shows"

# Claude model selection (override via environment variables).
# Cost hierarchy (cheapest to most expensive): Haiku → Sonnet.
# Script + polish use Sonnet (dialogue quality demonstrably needs it — carried
# over from the reference show); everything else defaults to Haiku.
SCRIPT_MODEL = os.getenv("CLAUDE_SCRIPT_MODEL", "claude-sonnet-5")
POLISH_MODEL = os.getenv("CLAUDE_POLISH_MODEL", "claude-sonnet-5")

# Music beds (shared default host pack assets)
INTRO_MUSIC = SCRIPT_DIR / "cariboo-signals-intro.mp3"
INTERVAL_MUSIC = SCRIPT_DIR / "cariboo-signals-interval.mp3"
OUTRO_MUSIC = SCRIPT_DIR / "cariboo-signals-outro.mp3"
INTERVAL_MUSIC_DURATION_MS = 6000
INTERVAL_FADE_OUT_MS = 1500

# Audio normalization targets (dBFS)
TARGET_SPEECH_DBFS = -20.0  # Speech louder and clear
TARGET_MUSIC_DBFS = -28.0   # Music ducked beneath speech

# Short fade applied to the end of each speech section before the transition gap.
SECTION_BOUNDARY_FADE_MS = 40

# Azure TTS feature flag (full switch; OpenAI is the default provider)
USE_AZURE_TTS = bool(os.getenv("USE_AZURE_TTS"))

# Maximum characters per OpenAI TTS call. Segments above this are pre-split at
# sentence boundaries so no single call carries enough text to risk a hang.
TTS_SEGMENT_MAX_CHARS = 500

# Body enrichment limits (prompt compression: cap what we feed the model)
BODY_MAX_CHARS = 6000
NEWS_BODY_MIN_CHARS = 400

_openai_quota_exceeded = False


# ── API helpers ─────────────────────────────────────────────────────────────

def api_retry(func, max_retries=3, base_delay=2):
    """Call func() with exponential backoff on transient errors."""
    for attempt in range(max_retries + 1):
        try:
            return func()
        except Exception as e:
            err_str = str(e)
            is_quota = 'insufficient_quota' in err_str
            is_transient = not is_quota and any(
                s in err_str for s in ['429', '503', '502', 'timeout', 'Connection'])
            if attempt < max_retries and is_transient:
                delay = base_delay * (2 ** attempt)
                print(f"  ⚠️  Retrying in {delay}s (attempt {attempt+1}/{max_retries}): {e}")
                time.sleep(delay)
            else:
                raise


# Per-episode spend accumulator: service -> {"calls": n, <unit>: total, ...}.
# Cleared at the start of each generate_episode run.
_API_TOTALS: dict[str, dict[str, int]] = {}


def _log_api_call(service: str, unit: str, count) -> None:
    """Log an API call for cost metering. Every call that spends money logs."""
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"  [api] {ts} service={service} {unit}={count}")
    totals = _API_TOTALS.setdefault(service, {"calls": 0})
    totals["calls"] += 1
    if isinstance(count, (int, float)):
        totals[unit] = totals.get(unit, 0) + int(count)


def _print_api_totals() -> None:
    """Print the per-episode API spend summary accumulated by _log_api_call."""
    if not _API_TOTALS:
        return
    print("  [api totals]")
    for service in sorted(_API_TOTALS):
        parts = " ".join(f"{u}={v}" for u, v in _API_TOTALS[service].items())
        print(f"    {service} {parts}")


def get_anthropic_client():
    api_key = os.getenv("ANTHROPIC_API_KEY")
    return Anthropic(api_key=api_key) if api_key else None


def get_openai_client():
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("⚠️  OPENAI_API_KEY not found")
        return None
    return OpenAI(api_key=api_key)


# ── Show paths and idempotency ─────────────────────────────────────────────

def show_dir(profile) -> Path:
    """Per-show output root: shows/<name>/ (site) and shows/<name>/podcasts/."""
    return SHOWS_DIR / profile["name"]


def show_podcasts_dir(profile) -> Path:
    return show_dir(profile) / "podcasts"


def _week_key(d: datetime) -> str:
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def episode_exists_for_period(profile, today: datetime) -> bool:
    """Idempotency guard: has this schedule period's episode been generated?

    Daily shows check for today's citations file; weekly shows check for any
    citations file inside the current ISO week. Citations are written after a
    script is finalized, so their presence marks a completed generation.
    """
    pdir = show_podcasts_dir(profile)
    if profile["schedule"] == "daily":
        return (pdir / f"citations_{today.strftime('%Y-%m-%d')}.json").exists()

    this_week = _week_key(today)
    for f in glob.glob(str(pdir / "citations_*.json")):
        m = re.search(r'citations_(\d{4}-\d{2}-\d{2})\.json', os.path.basename(f))
        if not m:
            continue
        try:
            file_date = datetime.strptime(m.group(1), "%Y-%m-%d")
        except ValueError:
            continue
        if _week_key(file_date) == this_week:
            return True
    return False


# ── Article body enrichment (deep dives) ────────────────────────────────────

_TAG_RE = re.compile(r'<(script|style)[^>]*>.*?</\1>', re.DOTALL | re.IGNORECASE)
_HTML_RE = re.compile(r'<[^>]+>')


def _fetch_article_body(url):
    """Fetch an article URL and reduce it to readable text. '' on any failure."""
    try:
        resp = requests.get(url, timeout=15, headers={
            "User-Agent": "PersonalPodcastCreator/1.0 (personal use)"})
        resp.raise_for_status()
        text = _TAG_RE.sub(' ', resp.text)
        text = _HTML_RE.sub(' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:BODY_MAX_CHARS]
    except Exception as e:
        print(f"  ⚠️  body fetch failed for {url[:60]}: {e}")
        return ""


def enrich_articles_with_bodies(articles, profile):
    """Fetch full text for each selected article; Brave-enrich thin ones.

    Mutates the article dicts (_body key). Only called when the plan says
    fetch_bodies — titles-depth shows never pay this cost.
    """
    for a in articles:
        if a.get("body"):
            a["_body"] = a["body"][:BODY_MAX_CHARS]
            continue
        a["_body"] = _fetch_article_body(a.get("url", "")) if a.get("url") else ""

    # Brave enrichment for articles whose body came back thin
    for a in articles:
        if len(a.get("_body", "")) >= NEWS_BODY_MIN_CHARS:
            continue
        results = search.search_all(a.get("title", ""), profile, count=3)
        if results:
            extra = " | ".join(
                f"{r['title']}: {r['description']}" for r in results if r.get("description"))
            a["_brave_context"] = extra[:1500]
            print(f"  🔎 Brave-enriched thin article: {a.get('title', '')[:60]}")
    return articles


# ── Script generation ───────────────────────────────────────────────────────

def _host_lines_and_tags():
    """Build host bios and speaker tags from the shared host pack."""
    hosts = load_hosts_config()
    host_lines = []
    for key, host in hosts.items():
        name = host.get("name", key.title())
        pronouns = host.get("pronouns", "")
        bio = host.get("full_bio", host.get("short_bio", ""))
        host_lines.append(f"- {name} ({pronouns}): {bio}")
    speaker_tags = ", ".join(
        f"**{h.get('name', k).upper()}:**" for k, h in hosts.items())
    host_names = " and ".join(
        h.get("name", k.title()) for k, h in hosts.items())
    return "\n".join(host_lines), speaker_tags, host_names


def _editorial_lens(profile):
    prompts = load_prompts_config()
    if profile.get("scope") == "regional":
        return Template(prompts["editorial_lens_regional"]).safe_substitute(
            region_hint=profile.get("region_hint", "the region"))
    return prompts["editorial_lens_global"]


def _psa_structure(psa_info):
    if not psa_info or not psa_info.get("org_name"):
        return ""
    prompts = load_prompts_config()
    details = (
        f"{psa_info['org_name']} — {psa_info.get('org_description', '')}"
    )
    if psa_info.get("psa_angle"):
        details += f" Angle: {psa_info['psa_angle']}"
    if psa_info.get("org_website"):
        details += f" Website: {psa_info['org_website']}"
    return Template(prompts["psa_structure"]).safe_substitute(psa_details=details)


def build_system_prompt(profile, plan, psa_info=None):
    """Assemble the script-generation system prompt from config templates.

    The source-integrity rules (cite specific sources, hedge honestly when
    sourcing is thin, attribute original reporting) are baked into every
    episode — they are what earns the TRACE sourceIntegrity /
    consentAttribution / displacement scores.
    """
    prompts = load_prompts_config()
    host_lines, speaker_tags, _ = _host_lines_and_tags()

    guardrail = prompts["hallucination_guardrail"]
    integrity = Template(prompts["source_integrity_rules"]).safe_substitute(
        hallucination_guardrail=guardrail,
        example_source="the source outlet's name",
    )

    structure_key = "structure_news" if plan["format"] == "news" else "structure_dialog"
    structure = Template(prompts[structure_key]).safe_substitute(
        psa_structure=_psa_structure(psa_info),
        email=profile["email"],
    )

    system = Template(prompts["system_template"]).safe_substitute(
        title=profile["title"],
        tagline=profile.get("tagline", ""),
        host_lines=host_lines,
        editorial_lens=_editorial_lens(profile),
        speaker_tags=speaker_tags,
        target_words=plan["target_words"],
        source_integrity_rules=integrity,
    )
    return f"{system}\n\n{structure}"


def _sources_block(articles):
    """Compact source-article block for the user prompt."""
    lines = ["SOURCE ARTICLES:\n"]
    for a in articles:
        lines.append(f"Title: {a.get('title', '')}")
        lines.append(f"Source: {a.get('source', '')}")
        lines.append(f"URL: {a.get('url', '')}")
        if a.get("published"):
            lines.append(f"Published: {a['published']}")
        if a.get("summary"):
            lines.append(f"Summary: {a['summary'][:400]}")
        if a.get("_body"):
            lines.append(f"Content: {a['_body'][:2000]}")
        if a.get("_brave_context"):
            lines.append(f"Related coverage: {a['_brave_context']}")
        if a.get("_evolving"):
            lines.append("Note: this is an update to a story this show covered recently.")
        lines.append("")
    return "\n".join(lines)


def generate_episode_script(profile, plan, articles, evolving_context="",
                            psa_info=None, client=None):
    """Generate the raw episode script for a profile. None on failure."""
    if client is None:
        client = get_anthropic_client()
    if client is None:
        print("❌ ANTHROPIC_API_KEY not set — cannot generate script")
        return None

    prompts = load_prompts_config()
    _, _, host_names = _host_lines_and_tags()
    system_prompt = build_system_prompt(profile, plan, psa_info=psa_info)
    user_prompt = Template(prompts["user_template"]).safe_substitute(
        sources_block=_sources_block(articles),
        evolving_context=evolving_context,
        host_names=host_names,
    )

    # Script length scales max_tokens: ~1.5 tokens/word plus headroom.
    max_tokens = min(16000, max(4000, int(plan["target_words"] * 2)))
    print(f"  Generating {plan['format']} script (~{plan['target_words']} words) "
          f"with {SCRIPT_MODEL}...")
    response = api_retry(lambda: client.messages.create(
        model=SCRIPT_MODEL,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    ))
    usage = getattr(response, "usage", None)
    _log_api_call("claude-script", "input_tokens", getattr(usage, "input_tokens", "?"))
    script = message_text(response)
    return script or None


def polish_script(script, profile, articles, client=None):
    """One combined polish + fact-check pass (batched into a single call).

    Keeps the evidence honest: statistics or findings that are not in the
    verified sources get rewritten with hedges rather than removed. Falls back
    to the raw script if the polish breaks the speaker-tag format.
    """
    if client is None:
        client = get_anthropic_client()
    if client is None:
        return script

    prompts = load_prompts_config()
    hosts = load_hosts_config()
    _, speaker_tags, _ = _host_lines_and_tags()
    verified_sources = "\n".join(
        f"- {a.get('title', '')} ({a.get('url', '')})\n  {a.get('summary', '')[:300]}"
        for a in articles
    )
    prompt = Template(prompts["polish_template"]).safe_substitute(
        title=profile["title"],
        hallucination_guardrail=prompts["hallucination_guardrail"],
        verified_sources=verified_sources,
        speaker_tags=speaker_tags,
        script=script,
    )
    print("  Polishing + fact-checking script...")
    try:
        response = api_retry(lambda: client.messages.create(
            model=POLISH_MODEL,
            max_tokens=16000,
            messages=[{"role": "user", "content": prompt}],
        ))
        usage = getattr(response, "usage", None)
        _log_api_call("claude-polish", "input_tokens", getattr(usage, "input_tokens", "?"))
        polished = message_text(response)
    except Exception as e:
        print(f"  ⚠️  Polish pass failed ({e}) — using raw script")
        return script

    host_tags = [f"**{h.get('name', k).upper()}:**" for k, h in hosts.items()]
    if polished and all(tag in polished for tag in host_tags):
        return polished
    print("  ⚠️  Polish may have broken format — using raw script")
    return script


def append_spoken_disclosure(script, profile):
    """Append the TRACE spoken disclosure as the final host line of the script.

    Living in the script (rather than only the assembler) means every render
    path — music assembly, TTS-only fallback, VTT/HTML transcripts — carries
    the disclosure. This is TRACE enforcement obligation 4: the AI-generated
    nature is disclosed in audio, not just metadata.
    """
    disclosure = trace_mod.spoken_disclosure(profile["trace"])
    hosts = load_hosts_config()
    first_host = next(iter(hosts.values()))
    tag = first_host.get("name", "host").upper()
    contact = f" Corrections and feedback: {profile['email']}."
    return f"{script.rstrip()}\n\n**{tag}:** {disclosure}{contact}\n"


# ── Citations + show notes ─────────────────────────────────────────────────

def _article_cited_in_script(article, script):
    """Loose check that an article actually made it into the episode."""
    title_words = [w for w in re.findall(r"\w{5,}", article.get("title", ""))][:3]
    return any(w.lower() in script.lower() for w in title_words) if title_words else True


def build_show_notes_html(profile, articles, date_str):
    """HTML show notes: description, cited sources, TRACE + corrections contact."""
    parts = [f"<p>{saxutils.escape(profile.get('tagline', ''))}</p>"]
    if articles:
        parts.append("<p><b>Sources cited in this episode:</b></p><ol>")
        for a in articles:
            title = saxutils.escape(a.get("title", "")[:100])
            src = saxutils.escape(a.get("source", ""))
            url = a.get("url", "")
            if url:
                parts.append(f'<li>{src}: <a href="{saxutils.escape(url)}">{title}</a></li>')
            else:
                parts.append(f"<li>{src}: {title}</li>")
        parts.append("</ol>")
    t = profile["trace"]
    parts.append(
        f"<p><b>TRACE transparency:</b> AI-generated episode "
        f"({', '.join(t.get('ai_tools', []))}). Self-assessed score "
        f"{t['total_score']}/{t['total_max']} ({t['verdict']}), track {t['track']}. "
        f"See {saxutils.escape(t['producer_url'])}</p>"
    )
    parts.append(
        f"<p><b>Corrections:</b> {saxutils.escape(profile['email'])}</p>"
    )
    return "".join(parts)


def write_citations_file(profile, articles, script, date_str):
    """Write citations_<date>.json — the machine-readable episode record.

    This file is what future runs deduplicate against, what the RSS feed builds
    episode entries from, and where the TRACE sourceIntegrity trail lives.
    """
    pdir = show_podcasts_dir(profile)
    pdir.mkdir(parents=True, exist_ok=True)

    cited = [a for a in articles if _article_cited_in_script(a, script)]
    if not cited:  # loose matcher can miss; never write an empty trail
        cited = articles

    segment_name = "deep_dive" if profile["cadence"] == "dialog" else "news"
    data = {
        "episode": {
            "date": date_str,
            "title": profile["title"],
            "description": build_show_notes_html(profile, cited, date_str),
            "corrections_contact": profile["email"],
        },
        "segments": {
            segment_name: {
                "articles": [
                    {
                        "title": a.get("title", ""),
                        "url": a.get("url", ""),
                        "source": a.get("source", ""),
                        "published": a.get("published", ""),
                    }
                    for a in cited
                ]
            }
        },
        "trace": {
            "total_score": profile["trace"]["total_score"],
            "total_max": profile["trace"]["total_max"],
            "verdict": profile["trace"]["verdict"],
        },
    }
    path = pdir / f"citations_{date_str}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"📋 Saved citations: {path}")
    return str(path)


# ── Script parsing (speaker turns + sections) ──────────────────────────────

def _host_tag_map():
    """Map UPPERCASE display names to host keys, e.g. RILEY -> riley."""
    hosts = load_hosts_config()
    return {h.get("name", k).upper(): k for k, h in hosts.items()}


def _extract_pacing_tag(text):
    """Extract an optional [overlap:N] or [pause:N] tag from the start of text.

    Returns (gap_ms, cleaned_text). gap_ms is None when no tag is present,
    meaning the heuristic default should be used at assembly time.
    """
    m = re.match(r'\[(?:overlap|pause):(-?\d+)\]\s*', text)
    if m:
        return int(m.group(1)), text[m.end():]
    return None, text


SECTION_MARKERS = [
    ("NEWS ROUNDUP", "news"),
    ("COMMUNITY SPOTLIGHT", "spotlight"),
    ("DEEP DIVE", "deep_dive"),
]


def parse_script_into_segments(script):
    """Parse a script into welcome/news/spotlight/deep_dive speaker segments."""
    segments = {"welcome": [], "news": [], "spotlight": [], "deep_dive": []}
    tag_map = _host_tag_map()
    speaker_re = re.compile(
        r'\*\*(' + "|".join(re.escape(n) for n in tag_map) + r'):\*\*\s*(.*)')

    current_section = "welcome"
    current_speaker = None
    current_text = []
    current_gap_ms = None

    def _flush():
        nonlocal current_text, current_gap_ms
        if current_speaker and current_text:
            segments[current_section].append({
                "speaker": current_speaker,
                "text": " ".join(current_text).strip(),
                "gap_ms": current_gap_ms,
            })
        current_text = []
        current_gap_ms = None

    for line in script.split("\n"):
        line = line.strip()
        if not line:
            continue

        # Section transitions
        matched_section = None
        for marker, section in SECTION_MARKERS:
            if marker in line.upper():
                matched_section = section
                break
        if matched_section:
            # Skip premature markers before any welcome content
            if not (current_section == "welcome" and not segments["welcome"]
                    and current_speaker is None):
                _flush()
                current_speaker = None
            current_section = matched_section
            continue

        m = speaker_re.match(line)
        if m:
            _flush()
            current_speaker = tag_map[m.group(1)]
            text_after = m.group(2) or ""
            current_gap_ms, text_after = _extract_pacing_tag(text_after)
            current_text = [text_after] if text_after else []
        elif current_speaker:
            # Standalone pacing tag between turns
            gap_ms_tag, remaining = _extract_pacing_tag(line)
            if gap_ms_tag is not None:
                _flush()
                current_gap_ms = gap_ms_tag
                if remaining.strip():
                    current_text = [remaining.strip()]
                continue
            # Skip metadata/markers; keep dialogue continuation lines
            if (not line.startswith("#") and not line.startswith("---")
                    and "SEGMENT" not in line and not line.startswith("[")):
                current_text.append(line)

    _flush()

    for section in segments:
        segments[section] = [s for s in segments[section] if len(s["text"]) > 10]

    print("🎭 Parsed script into segments: " + ", ".join(
        f"{k}={len(v)}" for k, v in segments.items()))
    return segments


# ── Dynamic pacing helpers (silence trim + heuristic gap) ──────────────────

def normalize_segment(audio_segment, target_dbfs):
    """Normalize a pydub segment to the target loudness."""
    change = target_dbfs - audio_segment.dBFS
    return audio_segment.apply_gain(change)


def trim_tts_silence(segment, silence_thresh=-45, min_silence_len=80):
    """Trim leading/trailing silence from a pydub AudioSegment."""
    from pydub.silence import detect_leading_silence
    lead = detect_leading_silence(segment, silence_threshold=silence_thresh,
                                  chunk_size=min_silence_len)
    trail = detect_leading_silence(segment.reverse(), silence_threshold=silence_thresh,
                                   chunk_size=min_silence_len)
    end = len(segment) - trail
    if end <= lead:
        return segment  # degenerate case: clip is entirely "silent"
    return segment[lead:end]


def _is_story_transition(text):
    """Detect phrases that signal a new story or topic shift."""
    lower = text.strip().lower()
    transition_starters = (
        "moving on", "next up", "in other news", "turning to",
        "switching gears", "also today", "meanwhile", "on the",
        "now,", "now ", "over in", "closer to home",
        "also worth noting", "before we move on", "a couple of quick",
        "and finally", "lastly", "wrapping up",
    )
    return any(lower.startswith(phrase) for phrase in transition_starters)


def _jitter_gap_ms(gap_ms, text):
    """Apply deterministic ±15% jitter so gaps don't fall on a metronomic grid.

    Seeded from a CRC of the text (not hash(), which is salted per process)
    so reruns produce identical audio. Short gaps are left untouched.
    """
    if gap_ms < 300:
        return gap_ms
    frac = (zlib.crc32(text.encode("utf-8")) % 1000) / 1000.0
    return int(gap_ms * (0.85 + 0.30 * frac))


def heuristic_gap_ms(text, prev_speaker, cur_speaker, section="deep_dive", prev_text=None):
    """Return a sensible inter-segment gap based on the upcoming text."""
    base = _heuristic_gap_base(text, prev_speaker, cur_speaker, section)
    if (
        base >= 600
        and section not in ("news", "welcome")
        and prev_text
        and prev_speaker and cur_speaker and prev_speaker != cur_speaker
        and prev_text.rstrip().rstrip('"”\'').endswith("?")
    ):
        base = 300
    return _jitter_gap_ms(base, text)


def _heuristic_gap_base(text, prev_speaker, cur_speaker, section):
    stripped = text.strip()
    char_count = len(stripped)

    if section == "news" and _is_story_transition(stripped):
        return 1800  # very clear topic break

    if cur_speaker and prev_speaker == cur_speaker:
        if section == "news":
            if char_count > 80:
                return 1500  # likely a new story — deliberate pause
            return 600
        return 100

    if section == "news":
        if char_count <= 25:
            return 300
        if char_count <= 80:
            return 600
        return 1300

    if section == "welcome":
        if char_count <= 25:
            return 200
        if char_count <= 80:
            return 400
        return 700

    # Default (deep dive / other): conversational pacing
    if char_count <= 25:
        return 180
    if char_count <= 80:
        return 320
    return 600


def _append_with_gap(combined, speech, gap_ms):
    """Append *speech* to *combined* using the given gap (negative = overlap)."""
    if gap_ms > 0:
        combined += AudioSegment.silent(duration=gap_ms) + speech
    elif gap_ms == 0:
        combined += speech
    else:
        overlap = min(-gap_ms, len(combined))
        position = len(combined) - overlap
        needed_len = position + len(speech)
        if needed_len > len(combined):
            combined += AudioSegment.silent(duration=needed_len - len(combined))
        combined = combined.overlay(speech, position=position)
    return combined


# ── TTS ─────────────────────────────────────────────────────────────────────

def _split_at_sentences(text, max_chars=TTS_SEGMENT_MAX_CHARS):
    """Split text into chunks at sentence boundaries, each under max_chars."""
    if len(text) <= max_chars:
        return [text]

    sentences = re.split(r'(?<=[.!?])\s+', text)
    raw_chunks = []
    current = ""
    for sentence in sentences:
        if not current:
            current = sentence
        elif len(current) + 1 + len(sentence) <= max_chars:
            current += " " + sentence
        else:
            raw_chunks.append(current)
            current = sentence
    if current:
        raw_chunks.append(current)

    # Guard: a single sentence longer than max_chars gets word-split
    result = []
    for chunk in raw_chunks:
        if len(chunk) <= max_chars:
            result.append(chunk)
        else:
            words = chunk.split()
            sub = ""
            for word in words:
                if not sub:
                    sub = word
                elif len(sub) + 1 + len(word) <= max_chars:
                    sub += " " + word
                else:
                    result.append(sub)
                    sub = word
            if sub:
                result.append(sub)
    return result


def generate_tts_for_segment(text, speaker, output_file):
    """Generate TTS audio for a text segment via OpenAI."""
    client = get_openai_client()
    if not client:
        raise ValueError("OPENAI_API_KEY not found")

    voice = get_voice_for_host(speaker)
    speed = get_speed_for_host(speaker)

    # Apply shared pronunciation substitutions
    clean = text
    for word, alias in AZURE_PRONUNCIATION_DICT.items():
        clean = clean.replace(word, alias)

    response = api_retry(lambda: client.audio.speech.create(
        model="tts-1",
        voice=voice,
        input=clean,
        speed=speed
    ), max_retries=2, base_delay=1)
    _log_api_call("openai-tts", "chars", len(clean))

    with open(output_file, "wb") as f:
        f.write(response.content)

    # Duration-ratio checksum: warn if audio is significantly shorter than expected.
    expected_words = len(re.findall(r"\b\w+\b", clean))
    if expected_words >= 10:
        actual_ms = len(AudioSegment.from_mp3(output_file))
        expected_ms = expected_words * 400  # 150 wpm ≈ 400 ms/word
        ratio = actual_ms / expected_ms
        if ratio < 0.80:
            print(
                f"  ⚠️  TTS duration check: expected ~{expected_ms // 1000}s "
                f"for {expected_words} words, got {actual_ms // 1000}s "
                f"({ratio:.0%}) — possible word omission"
            )


# ── Audio assembly ──────────────────────────────────────────────────────────

SECTION_ORDER = [
    ("welcome", "🎤 Generating welcome section..."),
    ("news", "📰 Generating news section..."),
    ("spotlight", "🏘️  Generating community spotlight..."),
    ("deep_dive", "🔍 Generating deep dive section..."),
]
SECTION_TITLES = {
    "welcome": "Introduction",
    "news": "News Roundup",
    "spotlight": "Community Spotlight",
    "deep_dive": "Deep Dive",
}


def build_credits_text(profile):
    """Short spoken production credits (accountability: corrections contact)."""
    tools = ", ".join(profile["trace"].get("ai_tools", [])) or "AI tools"
    return (
        f"{profile['title']} is produced by {profile.get('author', 'its producer')} "
        f"using {tools}. Automated with GitHub Actions. "
        f"Corrections: {profile['email']}."
    )


def generate_audio_from_script(script, output_filename, profile):
    """Convert script to audio with music beds and per-section transitions."""
    print("📊 Generating audio with music interludes...")

    if USE_AZURE_TTS:
        if not get_azure_speech_config():
            print("❌ Azure TTS enabled but AZURE_SPEECH_KEY/AZURE_SPEECH_REGION not set")
            return None
    elif not get_openai_client():
        return None

    music_files_exist = all([
        INTRO_MUSIC.exists(), INTERVAL_MUSIC.exists(), OUTRO_MUSIC.exists()])
    if not music_files_exist:
        print("⚠️  Music files not found — falling back to TTS-only mode")
        return generate_audio_tts_only(script, output_filename)

    try:
        segments = parse_script_into_segments(script)
        content_sections = [s for s, _ in SECTION_ORDER[1:] if segments[s]]
        if not segments["welcome"] or not content_sections:
            print("⚠️  Segment parsing failed - falling back to TTS-only mode")
            return generate_audio_tts_only(script, output_filename)

        intro_music = normalize_segment(AudioSegment.from_mp3(str(INTRO_MUSIC)), TARGET_MUSIC_DBFS)
        interval_music = normalize_segment(AudioSegment.from_mp3(str(INTERVAL_MUSIC)), TARGET_MUSIC_DBFS)
        interval_music = interval_music[:INTERVAL_MUSIC_DURATION_MS].fade_out(INTERVAL_FADE_OUT_MS)
        outro_music = normalize_segment(AudioSegment.from_mp3(str(OUTRO_MUSIC)), TARGET_MUSIC_DBFS)

        section_gap = AudioSegment.silent(duration=400)

        with tempfile.TemporaryDirectory() as tmpdir:
            combined = intro_music + section_gap
            chapters = [{"startTime": 0, "title": "Introduction"}]

            def _render_section(seg_list, label, prefix):
                nonlocal combined
                print(f"  {label}")

                if USE_AZURE_TTS:
                    section_wav = os.path.join(tmpdir, f"{prefix}_azure.wav")
                    total_chars = sum(len(s['text']) for s in seg_list)
                    print(f"    Azure Multi-Talker: {len(seg_list)} turns, {total_chars} chars")
                    generate_azure_tts_for_section(seg_list, section_wav)
                    section_audio = normalize_segment(
                        trim_tts_silence(AudioSegment.from_file(section_wav, format="wav")),
                        TARGET_SPEECH_DBFS,
                    )
                    combined += section_audio
                    return

                prev_speaker = None
                prev_text = None
                for i, segment in enumerate(seg_list):
                    chunks = _split_at_sentences(segment['text'])
                    chunk_label = f" ({len(chunks)} chunks)" if len(chunks) > 1 else ""
                    print(f"    {segment['speaker']}: {len(segment['text'])} chars{chunk_label}")

                    chunk_audios = []
                    for j, chunk_text in enumerate(chunks):
                        temp_file = os.path.join(tmpdir, f"{prefix}_{i}_{j}.mp3")
                        generate_tts_for_segment(chunk_text, segment['speaker'], temp_file)
                        chunk_audio = normalize_segment(AudioSegment.from_mp3(temp_file), TARGET_SPEECH_DBFS)
                        chunk_audios.append(trim_tts_silence(chunk_audio))
                    speech = sum(chunk_audios[1:], chunk_audios[0])

                    gap = segment.get('gap_ms')
                    if gap is None:
                        gap = heuristic_gap_ms(segment['text'], prev_speaker,
                                               segment['speaker'], section=prefix,
                                               prev_text=prev_text)
                    combined = _append_with_gap(combined, speech, gap)
                    prev_speaker = segment['speaker']
                    prev_text = segment['text']

            # Welcome, then each present content section, separated by interval music
            _render_section(segments["welcome"], SECTION_ORDER[0][1], "welcome")
            for section in content_sections:
                combined = combined[:-SECTION_BOUNDARY_FADE_MS] + \
                    combined[-SECTION_BOUNDARY_FADE_MS:].fade_out(SECTION_BOUNDARY_FADE_MS)
                combined += section_gap + interval_music + section_gap
                chapters.append({"startTime": round(len(combined) / 1000, 1),
                                 "title": SECTION_TITLES[section]})
                label = dict(SECTION_ORDER)[section]
                _render_section(segments[section], label, section)

            # Spoken credits (accountability) before the outro music
            chapters.append({"startTime": round(len(combined) / 1000, 1), "title": "Credits"})
            credits_text = build_credits_text(profile)
            try:
                if USE_AZURE_TTS:
                    credits_wav = os.path.join(tmpdir, "credits.wav")
                    first_host = next(iter(load_hosts_config()))
                    generate_azure_tts_for_section(
                        [{"speaker": first_host, "text": credits_text, "gap_ms": None}],
                        credits_wav,
                    )
                    credits_audio = normalize_segment(
                        trim_tts_silence(AudioSegment.from_file(credits_wav, format="wav")),
                        TARGET_SPEECH_DBFS,
                    )
                else:
                    credits_file = os.path.join(tmpdir, "credits.mp3")
                    first_host = next(iter(load_hosts_config()))
                    generate_tts_for_segment(credits_text, first_host, credits_file)
                    credits_audio = normalize_segment(
                        trim_tts_silence(AudioSegment.from_mp3(credits_file)), TARGET_SPEECH_DBFS
                    )
                combined += AudioSegment.silent(duration=600) + credits_audio
                print("  ✅ Added spoken credits")
            except Exception as ce:
                print(f"  ⚠️  Credits segment skipped: {ce}")

        combined += section_gap + outro_music
        combined.export(output_filename, format="mp3")

        # Save chapters JSON
        chapters_data = {"version": "1.2.0", "chapters": chapters}
        chapters_filename = str(Path(output_filename).with_name(
            Path(output_filename).name.replace('podcast_audio_', 'podcast_chapters_').replace('.mp3', '.json')
        ))
        with open(chapters_filename, 'w', encoding='utf-8') as f:
            json.dump(chapters_data, f, indent=2)
        print(f"📑 Saved chapters: {chapters_filename}")

        duration_minutes = len(combined) / 1000 / 60
        file_size_mb = os.path.getsize(output_filename) / 1024 / 1024
        print(f"✅ Generated podcast audio! {duration_minutes:.1f} min, {file_size_mb:.1f} MB")
        return output_filename

    except Exception as e:
        print(f"❌ Error generating audio with music: {e}")
        if 'insufficient_quota' in str(e):
            global _openai_quota_exceeded
            _openai_quota_exceeded = True
            print("💳 OpenAI billing quota exceeded — skipping audio generation")
            return None
        print("⚠️  Falling back to TTS-only mode")
        return generate_audio_tts_only(script, output_filename)


def generate_audio_tts_only(script, output_filename):
    """Fallback: generate audio without music (TTS only)."""
    print("📊 Generating TTS-only audio...")
    if not get_openai_client():
        return None
    try:
        segments = parse_script_into_segments(script)
        all_segments = [s for name, _ in SECTION_ORDER for s in segments[name]]
        if not all_segments:
            return None
        combined = AudioSegment.silent(duration=500)
        with tempfile.TemporaryDirectory() as tmpdir:
            for i, segment in enumerate(all_segments):
                chunks = _split_at_sentences(segment["text"])
                for j, chunk_text in enumerate(chunks):
                    temp_file = os.path.join(tmpdir, f"seg_{i}_{j}.mp3")
                    generate_tts_for_segment(chunk_text, segment["speaker"], temp_file)
                    audio = normalize_segment(AudioSegment.from_mp3(temp_file), TARGET_SPEECH_DBFS)
                    combined += trim_tts_silence(audio) + AudioSegment.silent(duration=400)
        combined.export(output_filename, format="mp3")
        return output_filename
    except Exception as e:
        print(f"❌ TTS-only generation failed: {e}")
        if 'insufficient_quota' in str(e):
            global _openai_quota_exceeded
            _openai_quota_exceeded = True
        return None


# ── Transcripts ─────────────────────────────────────────────────────────────

def _ms_to_vtt_ts(ms):
    """Convert milliseconds to WebVTT timestamp HH:MM:SS.mmm."""
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def script_to_vtt_transcript(script_content, intro_offset_ms=25000):
    """Convert a raw podcast script to WebVTT format with estimated timestamps."""
    WORDS_PER_MS = 140 / 60000
    tag_map = _host_tag_map()
    speaker_re = re.compile(
        r'\*\*(' + "|".join(re.escape(n) for n in tag_map) + r'):\*\*\s*(.*)')
    cues = []
    current_ms = intro_offset_ms

    for line in script_content.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue

        extra_pause = sum(int(m.group(1)) for m in re.finditer(r'\[pause:(\d+)\]', stripped))
        stripped = re.sub(r'\[(?:overlap|pause):-?\d+\]\s*', '', stripped).strip()

        m = speaker_re.match(stripped)
        if not m:
            continue
        speaker = m.group(1).title()
        text = m.group(2).strip()
        if not text:
            continue

        current_ms += extra_pause
        duration_ms = max(1000, int(len(text.split()) / WORDS_PER_MS))
        end_ms = current_ms + duration_ms
        cues.append(f"{_ms_to_vtt_ts(current_ms)} --> {_ms_to_vtt_ts(end_ms)}\n<v {speaker}>{text}")
        current_ms = end_ms + 300

    return "WEBVTT\n\n" + "\n\n".join(cues) if cues else None


def script_to_friendly_transcript(script_content, title="Transcript"):
    """Convert a raw podcast script to a clean HTML transcript."""
    tag_map = _host_tag_map()
    speaker_re = re.compile(
        r'\*\*(' + "|".join(re.escape(n) for n in tag_map) + r'):\*\*\s*(.*)')
    section_headers = {m for m, _ in SECTION_MARKERS}

    html_parts = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        f"<head><meta charset=\"UTF-8\"><title>{saxutils.escape(title)}</title></head>",
        "<body>",
    ]

    for line in script_content.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        stripped = re.sub(r'\[(?:overlap|pause):-?\d+\]\s*', '', stripped)

        m = speaker_re.match(stripped)
        if m:
            text = saxutils.escape(m.group(2).strip())
            html_parts.append(f"<p><strong>{m.group(1).title()}:</strong> {text}</p>")
            continue

        header_m = re.match(r'\*\*([^*]+)\*\*', stripped)
        if header_m:
            header_text = header_m.group(1).strip().rstrip(':')
            if any(kw in header_text.upper() for kw in section_headers):
                html_parts.append(f"<h2>{saxutils.escape(header_text)}</h2>")
                continue

        if not stripped:
            html_parts.append("")
            continue
        html_parts.append(f"<p>{saxutils.escape(stripped)}</p>")

    html_parts.append("</body>")
    html_parts.append("</html>")
    return "\n".join(html_parts)


def generate_episode_transcript(profile, script_filename, date_str):
    """Generate HTML and WebVTT transcripts from a podcast script file."""
    if not script_filename or not os.path.exists(script_filename):
        return None

    pdir = show_podcasts_dir(profile)
    html_filename = str(pdir / f"podcast_transcript_{date_str}.html")
    vtt_filename = str(pdir / f"podcast_transcript_{date_str}.vtt")

    try:
        with open(script_filename, 'r', encoding='utf-8') as f:
            script_content = f.read()

        html = script_to_friendly_transcript(
            script_content, title=f"{profile['title']} — {date_str}")
        with open(html_filename, 'w', encoding='utf-8') as f:
            f.write(html)
        print(f"📄 Saved HTML transcript to: {html_filename}")

        vtt = script_to_vtt_transcript(script_content)
        if vtt:
            with open(vtt_filename, 'w', encoding='utf-8') as f:
                f.write(vtt)
            print(f"📄 Saved VTT transcript to: {vtt_filename}")

        return html_filename
    except Exception as e:
        print(f"⚠️  Could not generate transcript: {e}")
        return None


# ── RSS + site emission ─────────────────────────────────────────────────────

def _rfc2822(date_obj):
    """Format a date as an RFC 2822 pubDate (episodes publish at 13:00 UTC)."""
    return date_obj.strftime("%a, %d %b %Y 13:00:00 GMT")


def generate_show_rss_feed(profile):
    """Generate a show's RSS feed (feed.xml) from its citations archive.

    The channel carries the machine-readable TRACE assessment
    (<trace:assessment>, obligation 3) and the corrections contact
    (obligation 6).
    """
    print(f"📡 Generating RSS feed for '{profile['name']}'...")
    pdir = show_podcasts_dir(profile)
    base_url = profile.get("base_url", "")
    if base_url and not base_url.endswith("/"):
        base_url += "/"

    episodes = []
    for citations_file in sorted(glob.glob(str(pdir / "citations_*.json")), reverse=True):
        m = re.match(r'citations_(\d{4}-\d{2}-\d{2})\.json', os.path.basename(citations_file))
        if not m:
            continue
        date_str = m.group(1)
        try:
            date_obj = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            continue

        try:
            with open(citations_file, 'r', encoding='utf-8') as f:
                citations_data = json.load(f)
        except Exception as e:
            print(f"   ⚠️ Could not load citations file {citations_file}: {e}")
            continue

        episode_meta = citations_data.get("episode", {})
        description = episode_meta.get("description", profile.get("tagline", ""))

        audio_basename = f"podcast_audio_{date_str}.mp3"
        audio_file = pdir / audio_basename
        if audio_file.exists():
            file_size = os.path.getsize(audio_file)
            try:
                audio = AudioSegment.from_mp3(str(audio_file))
                total_secs = len(audio) // 1000
                duration = f"{total_secs // 60}:{total_secs % 60:02d}"
            except Exception:
                duration = f"{profile['length_minutes']}:00"
        elif episode_meta.get("audio_file_size"):
            file_size = episode_meta["audio_file_size"]
            duration = episode_meta.get("audio_duration", f"{profile['length_minutes']}:00")
        else:
            print(f"   ⚠️ No audio found for {audio_basename} — skipping")
            continue

        episode = {
            "title": f"{profile['title']} — {date_str}",
            "audio_url": f"{base_url}podcasts/{audio_basename}",
            "pub_date": _rfc2822(date_obj),
            "file_size": file_size,
            "duration": duration,
            "description": description,
            "guid": f"{profile['name']}-{date_str}",
        }
        for kind, ext, mime in (("vtt", "vtt", "text/vtt"), ("html", "html", "text/html")):
            t_file = pdir / f"podcast_transcript_{date_str}.{ext}"
            episode[f"transcript_{kind}"] = (
                f"{base_url}podcasts/podcast_transcript_{date_str}.{ext}"
                if t_file.exists() else None)
        chapters_file = pdir / f"podcast_chapters_{date_str}.json"
        episode["chapters_url"] = (
            f"{base_url}podcasts/podcast_chapters_{date_str}.json"
            if chapters_file.exists() else None)
        episodes.append(episode)

    language = profile.get("language", "en-us")
    author = profile.get("author", profile["title"])
    cover = profile.get("cover_image", "")

    rss_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"'
        ' xmlns:podcast="https://podcastindex.org/namespace/1.0"'
        f' xmlns:trace="{trace_mod.NAMESPACE}">',
        '<channel>',
        f'<title>{saxutils.escape(profile["title"])}</title>',
        f'<link>{base_url}index.html</link>',
        f'<language>{language}</language>',
        f'<itunes:subtitle>{saxutils.escape(profile.get("tagline", ""))}</itunes:subtitle>',
        f'<itunes:author>{saxutils.escape(author)}</itunes:author>',
        f'<itunes:summary>{saxutils.escape(profile.get("tagline", ""))}</itunes:summary>',
        f'<description>{saxutils.escape(profile.get("tagline", ""))} '
        f'Corrections: {saxutils.escape(profile["email"])}</description>',
        '<itunes:owner>',
        f'<itunes:name>{saxutils.escape(author)}</itunes:name>',
        f'<itunes:email>{saxutils.escape(profile["email"])}</itunes:email>',
        '</itunes:owner>',
    ]
    if cover:
        rss_lines.append(f'<itunes:image href="{base_url}{cover}"/>')
    for category in profile.get("categories", []):
        rss_lines.append(f'<itunes:category text="{saxutils.escape(category)}"/>')
    rss_lines.extend([
        '<itunes:type>episodic</itunes:type>',
        f'<itunes:explicit>{"true" if profile.get("explicit") else "false"}</itunes:explicit>',
        f'<lastBuildDate>{datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")}</lastBuildDate>',
    ])

    # TRACE machine-readable transparency record (channel level)
    rss_lines += trace_mod.build_trace_xml(profile["trace"], author)

    for episode in episodes:
        item_lines = [
            '<item>',
            f'<title>{saxutils.escape(episode["title"])}</title>',
            f'<link>{base_url}index.html</link>',
            f'<pubDate>{episode["pub_date"]}</pubDate>',
            f'<description><![CDATA[{episode["description"]}]]></description>',
            f'<itunes:summary><![CDATA[{episode["description"]}]]></itunes:summary>',
            f'<enclosure url="{saxutils.escape(episode["audio_url"], {chr(34): "&quot;"})}" '
            f'length="{episode["file_size"]}" type="audio/mpeg"/>',
            f'<guid isPermaLink="false">{saxutils.escape(episode["guid"])}</guid>',
            f'<itunes:duration>{episode["duration"]}</itunes:duration>',
            f'<itunes:explicit>{"true" if profile.get("explicit") else "false"}</itunes:explicit>',
            '<itunes:episodeType>full</itunes:episodeType>',
        ]
        if episode.get("transcript_vtt"):
            u = saxutils.escape(episode["transcript_vtt"], {chr(34): "&quot;"})
            item_lines.append(f'<podcast:transcript url="{u}" type="text/vtt" language="{language}"/>')
        if episode.get("transcript_html"):
            u = saxutils.escape(episode["transcript_html"], {chr(34): "&quot;"})
            item_lines.append(f'<podcast:transcript url="{u}" type="text/html" language="{language}"/>')
        if episode.get("chapters_url"):
            u = saxutils.escape(episode["chapters_url"], {chr(34): "&quot;"})
            item_lines.append(f'<podcast:chapters url="{u}" type="application/json+chapters"/>')
        item_lines.append('</item>')
        rss_lines.extend(item_lines)

    rss_lines.extend(['</channel>', '</rss>'])

    feed_path = show_dir(profile) / "feed.xml"
    feed_path.parent.mkdir(parents=True, exist_ok=True)
    with open(feed_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(rss_lines))
    print(f"✅ Generated RSS feed with {len(episodes)} episodes: {feed_path}")
    return str(feed_path)


def generate_show_index_html(profile):
    """Minimal per-show landing page: episodes, TRACE summary, corrections."""
    pdir = show_podcasts_dir(profile)
    t = profile["trace"]
    rows = []
    for citations_file in sorted(glob.glob(str(pdir / "citations_*.json")), reverse=True):
        m = re.match(r'citations_(\d{4}-\d{2}-\d{2})\.json', os.path.basename(citations_file))
        if not m:
            continue
        date_str = m.group(1)
        rows.append(
            f'<li>{date_str} — <a href="podcasts/podcast_audio_{date_str}.mp3">audio</a>'
            f' · <a href="podcasts/podcast_transcript_{date_str}.html">transcript</a></li>')

    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>{saxutils.escape(profile['title'])}</title></head>
<body>
<h1>{saxutils.escape(profile['title'])}</h1>
<p>{saxutils.escape(profile.get('tagline', ''))}</p>
<p><a href="feed.xml">Podcast RSS feed</a></p>
<h2>Episodes</h2>
<ul>
{chr(10).join(rows) if rows else '<li>No episodes yet.</li>'}
</ul>
<h2>Transparency (TRACE)</h2>
<p>This show is AI-generated using {saxutils.escape(', '.join(t.get('ai_tools', [])))}.
Self-assessed TRACE score: {t['total_score']}/{t['total_max']} ({saxutils.escape(t['verdict'])}),
track {saxutils.escape(t['track'])}, assessed {saxutils.escape(t['assessment_date'])} by
{saxutils.escape(t['assessed_by'])}. Standard: <a href="{trace_mod.NAMESPACE}">TRACE 1.0</a>.</p>
<p>Corrections and feedback: {saxutils.escape(profile['email'])}</p>
</body>
</html>
"""
    index_path = show_dir(profile) / "index.html"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with open(index_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"✅ Generated site index: {index_path}")
    return str(index_path)


# ── Main pipeline ───────────────────────────────────────────────────────────

def generate_episode(profile_name, force=False, dry_run=False):
    """Run the full pipeline for one profile. Returns True when an episode
    exists at the end of the run (fresh or already present)."""
    _API_TOTALS.clear()
    try:
        return _generate_episode(profile_name, force=force, dry_run=dry_run)
    finally:
        # finally so the spend summary survives every exit path, including
        # the sys.exit on OpenAI quota exhaustion
        _print_api_totals()


def _generate_episode(profile_name, force=False, dry_run=False):
    profile = load_podcast_profile(profile_name)
    print(f"🎙️ {profile['title']} — {profile.get('tagline', '')}")
    print("=" * 60)

    # TRACE publish gate — before anything is generated or written (obligation 2)
    trace_cfg = trace_mod.assert_publishable(profile)
    print(f"✅ TRACE gate passed: {trace_cfg['total_score']}/{trace_cfg['total_max']} "
          f"({trace_cfg['verdict']}), track {trace_cfg['track']}")

    plan = build_episode_plan(profile)
    print(f"📐 Plan: {plan['format']}, {plan['article_count']} articles, "
          f"~{plan['target_words']} words, fetch_bodies={plan['fetch_bodies']}")

    today = datetime.now()
    date_str = today.strftime("%Y-%m-%d")
    pdir = show_podcasts_dir(profile)

    # Idempotency: don't regenerate this period's episode
    if not force and episode_exists_for_period(profile, today):
        print(f"✅ This {profile['schedule']} period's episode already exists — "
              "refreshing feed only")
        generate_show_rss_feed(profile)
        generate_show_index_html(profile)
        return True

    # Ingest: profile feeds + keyword search, deduped by URL
    articles = sources.gather_feed_articles(profile)
    articles += search.gather_keyword_articles(profile)
    seen, merged = set(), []
    for a in articles:
        url = a.get("url", "")
        if url and url not in seen:
            seen.add(url)
            merged.append(a)
    print(f"📥 Ingested {len(merged)} unique articles")
    if not merged:
        print("❌ No articles from any source — cannot generate an episode")
        return False

    # Rank + select (dedup vs history, Haiku scoring, evolving-story boost)
    client = get_anthropic_client()
    selected, evolving_context = ranking.rank_and_select(
        merged, profile, plan, client=client, podcasts_dir=pdir)
    if not selected:
        print("❌ No fresh articles survived dedup — skipping to protect novelty")
        return False
    print(f"🏆 Selected {len(selected)} stories:")
    for a in selected:
        print(f"   [{a.get('_boosted_score', '?')}] {a.get('title', '')[:75]}")

    # Depth: fetch bodies + Brave enrichment for deep dives only
    if plan["fetch_bodies"]:
        enrich_articles_with_bodies(selected, profile)

    # Optional PSA / community spotlight
    psa_info = select_psa(profile, today=today.date())
    if psa_info and psa_info.get("org_name"):
        print(f"🏘️  Community Spotlight: {psa_info['org_name']} ({psa_info['source']})")

    if dry_run:
        print("🔎 Dry run — stopping before script generation (no API spend)")
        return False

    # Generate + polish script
    script = generate_episode_script(
        profile, plan, selected, evolving_context=evolving_context,
        psa_info=psa_info, client=client)
    if not script:
        print("❌ Failed to generate script. Exiting.")
        return False
    script = polish_script(script, profile, selected, client=client)

    # Spoken TRACE disclosure — in the script so every render path carries it
    script = append_spoken_disclosure(script, profile)

    # Persist script
    pdir.mkdir(parents=True, exist_ok=True)
    script_filename = str(pdir / f"podcast_script_{date_str}.txt")
    with open(script_filename, "w", encoding="utf-8") as f:
        f.write(f"# {profile['title']} — {date_str}\n\n{script}")
    print(f"💾 Saved script: {script_filename}")

    # Citations (the sourceIntegrity trail; also the dedup history)
    write_citations_file(profile, selected, script, date_str)

    # Audio
    audio_filename = str(pdir / f"podcast_audio_{date_str}.mp3")
    audio_file = generate_audio_from_script(script, audio_filename, profile)
    if audio_file:
        print(f"🎉 Episode audio complete: {audio_file}")
    else:
        print("📊 Audio generation failed — script and citations are saved; "
              "a re-run will pick up from the feed refresh")

    # Transcripts, feed, index
    generate_episode_transcript(profile, script_filename, date_str)
    generate_show_rss_feed(profile)
    generate_show_index_html(profile)

    if _openai_quota_exceeded:
        print("❌ OpenAI billing quota exceeded — audio was not generated.")
        sys.exit(1)

    return audio_file is not None


def main():
    parser = argparse.ArgumentParser(description="Personal Podcast Creator")
    parser.add_argument("--profile", default=None,
                        help="Profile name (config/profiles/<name>.json). "
                             "Omit to list available profiles.")
    parser.add_argument("--force", action="store_true",
                        help="Regenerate even if this period's episode exists")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run ingest + ranking only; no script/audio API spend")
    args = parser.parse_args()

    if not args.profile:
        names = list_podcast_profiles()
        print("Available profiles:")
        for n in names:
            print(f"  - {n}")
        print("\nRun with: python podcast_generator.py --profile <name>")
        return

    generate_episode(args.profile, force=args.force, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
