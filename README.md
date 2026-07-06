# Personal Podcasts

A **personal, multi-podcast creator**: one codebase that generates many
independent AI podcasts, each defined by a single profile file. Adapted from
the [Cariboo Signals](https://podcast.cariboosignals.ca/) daily pipeline.

Every show is one JSON file. The generator ingests the show's feeds and
keyword searches, ranks stories for relevance and novelty, writes a two-host
script with Claude, renders audio with OpenAI (or Azure) TTS, and publishes a
podcast RSS feed with full source citations — on a daily or weekly schedule
via GitHub Actions.

## TRACE standard (enforced)

Every show **must** carry a valid [TRACE](https://tracestandard.org/)
assessment. This is a hard gate, not a suggestion:

- A profile without a valid `trace` block refuses to load.
- A disqualified assessment, an undeclared AI role, or a missing corrections
  contact aborts generation before anything is written.
- The RSS feed embeds a machine-readable `<trace:assessment>` block.
- Every episode's audio includes a spoken AI-generation disclosure.
- Per-episode citations are written to JSON and surfaced in show notes.
- The corrections contact is published in the feed, show notes, and spoken credits.

## Creating a show

Add `config/profiles/<name>.json` (see `config/profiles/sample.json`):

```jsonc
{
  "title": "My Podcast",
  "tagline": "One-line subtitle",
  "sources": {
    "opml": "config/feeds.opml",           // optional OPML of RSS feeds
    "rss": ["https://.../feed.xml"],        // optional explicit feeds
    "keywords": ["topic one", "topic two"]  // optional web-search topics
  },
  "scope": "regional",            // "regional" | "global"
  "region_hint": "Cariboo, BC",   // appended to searches when regional
  "cadence": "dialog",            // "news" (roundup) | "dialog" (deep-dive debate)
  "depth": "deep_dive",           // "titles" | "deep_dive"
  "length_minutes": 15,           // 5–30
  "schedule": "weekly",           // "daily" | "weekly"
  "schedule_day": "sunday",
  "search": { "brave": true, "kagi": true },
  "voices": "cariboo_signals",
  "psa": false,                   // optional community-spotlight segment
  "base_url": "https://<user>.github.io/Personal-Podcasts/shows/<name>/",
  "author": "Your Name",
  "email": "corrections@example.com",   // REQUIRED — TRACE accountability contact
  "trace": { /* REQUIRED — see sample.json for the full block */ }
}
```

Then run it:

```bash
pip install -r requirements.txt   # plus ffmpeg for audio assembly
python podcast_generator.py --profile <name>
```

Outputs land in `shows/<name>/`: `feed.xml`, `index.html`, and
`podcasts/` (audio, scripts, citations, transcripts, chapters).

## Scheduling (GitHub Actions)

`.github/workflows/generate.yml` runs a matrix job over every profile in
`config/profiles/`. Each run checks the profile's `schedule`/`schedule_day`
against today and exits early when it isn't the show's day; an idempotency
guard makes re-runs safe. Generated episodes are committed back to the repo so
GitHub Pages can serve them.

**Setup:**
1. Add repository secrets: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, and any of
   `BRAVE_SEARCH_API_KEY`, `KAGI_API_KEY`, `COHERE_API_KEY`,
   `AZURE_SPEECH_KEY`/`AZURE_SPEECH_REGION` you use.
2. Enable GitHub Pages (Settings → Pages → deploy from branch, `/ (root)`).
3. Set each profile's `base_url` to your Pages URL for that show.

## Development

```bash
python -m pytest tests/ -v   # no API keys needed; SDKs are stubbed
```

See `CLAUDE.md` for architecture, the TRACE enforcement map, and the API cost
discipline rules that govern changes.
