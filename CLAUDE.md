# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Role and Style

Direct, technical, efficient. No fluff. No apologies. Get straight to the technical solution. Explain the "why" behind significant architectural decisions briefly before writing code.

Apply the **ponytail** decision ladder before writing any code — stop at the first rung that satisfies the task:
1. Does this need to exist? (YAGNI — skip it)
2. Does the standard library handle it?
3. Is there a native platform feature?
4. Is an installed dependency already doing this?
5. Can it be one line?
6. Only then: write the minimum that works.

Mark shortcuts with `# ponytail:` comments naming the simpler path chosen. Safety, security, data-loss handling, and accessibility are never cut.

## What this repository is

A **personal, multi-podcast creator**: one codebase that generates many
independent AI podcasts, each defined by a single profile file at
`config/profiles/<name>.json`. Adapted from the Cariboo Signals daily
pipeline, self-contained (no sibling-repo dependency), driven entirely by
per-show config.

## TRACE standard — enforced, not optional

No episode is generated or published for a profile without a valid,
non-disqualified TRACE assessment (https://tracestandard.org/ns/trace/1.0).
`trace.py` is the single enforcement point:

1. **Load-time validation** — `config_loader.load_podcast_profile` raises on a
   missing/malformed `trace` block.
2. **Publish gate** — `podcast_generator.generate_episode` calls
   `trace.assert_publishable(profile)` before generating or writing anything.
3. **Feed emission** — the RSS channel embeds `trace.build_trace_xml(...)`.
4. **Spoken disclosure** — `append_spoken_disclosure` puts
   `trace.spoken_disclosure(...)` in the script so every audio path renders it.
5. **Source integrity** — the script prompts instruct hosts to cite sources,
   hedge when sourcing is thin, and attribute original reporting; a
   `citations_<date>.json` is written per episode and surfaced in show notes.
6. **Accountability** — the corrections contact (`profile["email"]`) is
   published in the feed, show notes, and spoken credits.

Never let a change weaken any of the six. In diffs, call out any change that
lowers a TRACE score or removes a disclosure/citation path. Treat this with
the same weight as safety/data-loss handling in the ponytail ladder — never cut it.

## Commands

```bash
# Run all tests (no API keys needed — tests/conftest.py stubs the SDKs)
python -m pytest tests/ -v

# Run a single test file
python -m pytest tests/test_podcast_generator.py -v

# List available shows
python podcast_generator.py

# Generate one show's episode (requires .env or exported API keys + ffmpeg)
python podcast_generator.py --profile sample
python podcast_generator.py --profile sample --dry-run   # ingest+rank only, no spend
python podcast_generator.py --profile sample --force     # ignore idempotency guard

# Inspect a profile / its plan / its sources
python config_loader.py
python episode_plan.py sample
python -c "import sources, config_loader; print(len(sources.gather_feed_articles(config_loader.load_podcast_profile('sample'))))"
```

**Note:** `tests/` is in `.gitignore`. Use `git add -f tests/` when committing test changes.

## Architecture

**Pipeline (`podcast_generator.py --profile <name>`):**
1. `load_podcast_profile` (validates pickers + TRACE) → `trace.assert_publishable` (hard gate)
2. `build_episode_plan` — length/depth/cadence → target words, article count, format
3. Ingest: `sources.gather_feed_articles` (OPML+RSS) + `search.gather_keyword_articles` (Brave/Kagi), URL-deduped
4. `ranking.rank_and_select` — history dedup → batched Haiku relevance scores →
   evolving-story boost → intra-batch clustering → optional Cohere rerank → top N
5. Depth: `enrich_articles_with_bodies` (deep dives only)
6. Script: `generate_episode_script` (templates in `config/prompts.json`) →
   `polish_script` (one combined polish+fact-check call) → `append_spoken_disclosure`
7. Render: OpenAI TTS per segment (or Azure via `USE_AZURE_TTS=1`), pydub assembly
   with the shared music beds, chapters JSON
8. Publish: citations JSON, VTT/HTML transcripts, per-show `feed.xml` with the
   TRACE block, per-show `index.html`

**Per-show outputs** live in `shows/<name>/` (`feed.xml`, `index.html`,
`podcasts/*`). Shows never share state — citations, PSA rotation, and feeds are
all per-profile.

**Profiles** (`config/profiles/<name>.json`): sources (opml/rss/keywords),
scope + region_hint, cadence (news|dialog), depth (titles|deep_dive),
length_minutes (5–30), schedule (daily|weekly + schedule_day), search engine
toggles, voices, optional `psa` + `psa_config`, publishing fields (base_url,
author, email), and the required `trace` block.

**Shared config:** `config/hosts.json` (host pack: voices, bios, debate
stances), `config/prompts.json` (all prompt templates — no show strings in code).

## API Cost Discipline

Treat API budget as a first-class constraint on every change.

- **Default to the cheapest model.** Haiku for scoring/clustering; Sonnet only
  for script generation + polish where dialogue quality demands it. Escalate
  only when demonstrably required — justify explicitly.
- **Prompt compression is mandatory.** Strip filler and redundant context
  before sending; ranking truncates summaries to 120 chars.
- **Batch where possible.** Relevance scoring is one call per 40 articles;
  polish and fact-check are a single combined call.
- **Never call an API when local logic suffices.** Dedup, filtering,
  formatting — Python first. The keyword fallback scorer keeps the pipeline
  alive with zero tokens.
- **Log token usage.** Every call that returns usage metadata must log it
  (`_log_api_call`). No silent spending.
- **Review diffs for cost regressions.** Call out any prompt/pipeline change
  that increases per-run token usage.

## Project Constraints

- Python 3.11+, PEP 8, type hints on all functions
- Idempotent scripts where possible (`episode_exists_for_period` guards re-runs)
- Refactor existing files rather than creating new ones
- Keep dependencies minimal — check `requirements.txt` before adding anything
- `tests/` is gitignored; use `git add -f tests/` to stage test files
