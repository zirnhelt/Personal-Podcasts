#!/usr/bin/env python3
"""
PSA selector — show-agnostic community organization rotation.

Selects a community organization to feature based on:
1. Event-driven: upcoming awareness dates or local events
2. Round-robin fallback: cycles through the day's roster

All data is profile-driven: a profile that wants a PSA segment sets
`"psa": true` and points `psa_config` at its own data files:

    "psa": true,
    "psa_config": {
        "organizations": "config/psa/myshow_organizations.json",
        "events": "config/psa/myshow_events.json",          // optional
        "notable_dates": "config/psa/myshow_dates.json"     // optional
    }

Rotation state is stored in the show's own output directory so shows never
share PSA history.
"""

import json
from datetime import date
from pathlib import Path

REPO_DIR = Path(__file__).parent

EVENT_LOOKAHEAD_DAYS = 7
MIN_DAYS_BETWEEN_REPEATS = 28


def _load_json(path, key=None, default=None):
    """Load a JSON file relative to the repo root; return default if absent."""
    if not path:
        return default if default is not None else []
    full = REPO_DIR / path
    if not full.exists():
        return default if default is not None else []
    with open(full, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data[key] if key else data


def load_psa_data(profile):
    """Load (organizations, events, notable_dates) from a profile's psa_config."""
    cfg = profile.get("psa_config", {})
    organizations = _load_json(cfg.get("organizations"), key="organizations", default={})
    events = _load_json(cfg.get("events"), key="events", default=[])
    notable_dates = _load_json(cfg.get("notable_dates"), key="dates", default=[])
    return organizations, events, notable_dates


def load_rotation_state(state_file):
    """Load round-robin rotation state from disk.

    State format:
        {
            "rotation": {"0": 2, "4": 1, ...},   # weekday -> last used index
            "last_aired": {"org_id": "YYYY-MM-DD", ...}  # org -> date last featured
        }
    """
    state_file = Path(state_file)
    if state_file.exists():
        with open(state_file, "r") as f:
            data = json.load(f)
        # Migrate legacy flat format: {"0": 3, "1": 1} -> new nested format
        if data and all(k.isdigit() for k in data):
            return {"rotation": data, "last_aired": {}}
        return data
    return {"rotation": {}, "last_aired": {}}


def save_rotation_state(state, state_file):
    """Persist rotation state to disk."""
    state_file = Path(state_file)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2)


def record_aired(org_id, today, state):
    """Record that an org aired on the given date."""
    state.setdefault("last_aired", {})[org_id] = today.isoformat()
    return state


def get_orgs_for_weekday(weekday, organizations):
    """Return (org_id, org_data) tuples eligible on a weekday.

    Orgs without a "weekdays" list are eligible every day — the common case for
    a weekly show with a small roster.
    """
    return [
        (org_id, org)
        for org_id, org in organizations.items()
        if "weekdays" not in org or weekday in org["weekdays"]
    ]


def find_active_events(today, events, lookahead_days=EVENT_LOOKAHEAD_DAYS):
    """Find events that are active today or start within the lookahead window.

    Returns events sorted by specificity: single-day events first, then shorter
    ranges, then longer ranges. This ensures a specific awareness day (like
    Earth Day) takes priority over a broad seasonal event.
    """
    year = today.year

    active = []
    for event in events:
        start_mmdd = event["start_date"]
        end_mmdd = event["end_date"]

        try:
            start = date(year, int(start_mmdd[:2]), int(start_mmdd[3:]))
            end = date(year, int(end_mmdd[:2]), int(end_mmdd[3:]))
        except ValueError:
            continue

        # Event is active if today falls within its date range
        if start <= today <= end:
            duration = (end - start).days
            active.append((duration, event))
            continue

        # Or if the event starts within the lookahead window.
        # Only tease short events (≤3 days) in advance — multi-day awareness weeks
        # (e.g. National Volunteer Week) should not run before they actually start,
        # because the psa_angle says "It's [event]" as if it's already happening.
        days_until = (start - today).days
        event_duration = (end - start).days
        if 0 < days_until <= lookahead_days and event_duration <= 3:
            duration = event_duration
            active.append((duration, event))

    # Sort by duration (shorter/more specific events first)
    active.sort(key=lambda x: x[0])
    return [event for _, event in active]


def match_event_to_roster(active_events, roster_org_ids, organizations):
    """Find the best event match for today's roster.

    Returns (org_id, org_data, psa_angle) or None.
    """
    for event in active_events:
        # Event targets a specific org
        target_id = event.get("organization_id")
        if target_id and target_id in roster_org_ids:
            org = organizations[target_id]
            angle = _format_psa_angle(event["psa_angle"], org)
            return target_id, org, angle

        # Event targets multiple specific orgs (rotate through them)
        target_ids = event.get("organization_ids", [])
        for tid in target_ids:
            if tid in roster_org_ids:
                org = organizations[tid]
                angle = _format_psa_angle(event["psa_angle"], org)
                return tid, org, angle

        # Event applies to all orgs (Volunteer Week, Giving Tuesday, etc.)
        if event.get("all_orgs") and roster_org_ids:
            # Pick first org on the roster for this "all orgs" event
            first_id = roster_org_ids[0]
            org = organizations[first_id]
            angle = _format_psa_angle(event["psa_angle"], org)
            return first_id, org, angle

    return None


def _format_psa_angle(angle_template, org):
    """Fill in {org_name} and {org_website} placeholders in PSA angle text."""
    return angle_template.format(
        org_name=org["name"],
        org_website=org.get("website", "their website"),
    )


def round_robin_select(weekday, roster, state, today, min_days=MIN_DAYS_BETWEEN_REPEATS):
    """Select the next org in rotation for this weekday, skipping recent repeats.

    Advances through the roster in order, skipping any org that aired within
    min_days. If every org aired recently (small roster), falls back to the
    least-recently-aired to avoid an infinite skip loop.

    Returns (org_id, org_data, updated_state).
    """
    rotation = state.setdefault("rotation", {})
    last_aired = state.setdefault("last_aired", {})

    key = str(weekday)
    last_index = rotation.get(key, -1)
    n = len(roster)

    for i in range(n):
        candidate_index = (last_index + 1 + i) % n
        org_id, org_data = roster[candidate_index]

        last_str = last_aired.get(org_id)
        if last_str:
            days_since = (today - date.fromisoformat(last_str)).days
            if days_since < min_days:
                continue  # Aired too recently, try next

        rotation[key] = candidate_index
        return org_id, org_data, state

    # All orgs aired recently — pick least-recently-aired to minimise wait
    def days_ago(item):
        oid, _ = item
        last_str = last_aired.get(oid)
        return date.fromisoformat(last_str) if last_str else date.min

    fallback_index = min(range(n), key=lambda i: days_ago(roster[i]))
    rotation[key] = fallback_index
    org_id, org_data = roster[fallback_index]
    return org_id, org_data, state


def find_notable_dates(today, notable_dates):
    """Find notable dates that match today.

    Returns a list of dicts with 'name' and 'note' for each matching date.
    Matches are dates where today falls on the exact date (or within a range
    if end_date is set).
    """
    today_mmdd = today.strftime("%m-%d")
    matches = []
    for entry in notable_dates:
        entry_mmdd = entry["date"]
        end_mmdd = entry.get("end_date") or entry_mmdd

        if entry_mmdd <= today_mmdd <= end_mmdd:
            matches.append({
                "name": entry["name"],
                "note": entry["note"],
            })

    return matches


def select_psa(profile, today=None, state_file=None):
    """Select today's PSA organization and talking points for one show.

    Returns None when the profile has PSAs disabled or no orgs are configured.
    Otherwise returns a dict with:
        org_id: str
        org_name: str
        org_short_name: str
        org_description: str
        org_website: str | None
        psa_angle: str | None  (event-specific angle, or None for round-robin)
        event_name: str | None
        source: "event" | "rotation"
        notable_dates: list[dict]  (dates of note for today)
    """
    if not profile.get("psa"):
        return None
    if today is None:
        today = date.today()
    if state_file is None:
        state_file = (REPO_DIR / "shows" / profile.get("name", "default")
                      / "podcasts" / "psa_rotation_state.json")

    organizations, events, notable_dates_data = load_psa_data(profile)

    weekday = today.weekday()
    roster = get_orgs_for_weekday(weekday, organizations)

    todays_notable = find_notable_dates(today, notable_dates_data)

    if not roster:
        # Still return notable dates even if no orgs are configured
        if todays_notable:
            return {
                "org_id": None,
                "org_name": None,
                "org_short_name": None,
                "org_description": None,
                "org_website": None,
                "psa_angle": None,
                "event_name": None,
                "source": None,
                "notable_dates": todays_notable,
            }
        return None

    roster_org_ids = [org_id for org_id, _ in roster]

    # Try event-driven selection first
    active_events = find_active_events(today, events)
    match = match_event_to_roster(active_events, roster_org_ids, organizations)

    if match:
        org_id, org, psa_angle = match
        # Find which event matched for metadata
        event_name = None
        for event in active_events:
            target = event.get("organization_id")
            targets = event.get("organization_ids", [])
            if target == org_id or org_id in targets or event.get("all_orgs"):
                event_name = event["name"]
                break

        # Record this org as aired so it counts toward cross-week deduplication
        state = load_rotation_state(state_file)
        state = record_aired(org_id, today, state)
        save_rotation_state(state, state_file)

        return {
            "org_id": org_id,
            "org_name": org["name"],
            "org_short_name": org["short_name"],
            "org_description": org["description"],
            "org_website": org.get("website"),
            "psa_angle": psa_angle,
            "event_name": event_name,
            "source": "event",
            "notable_dates": todays_notable,
        }

    # Fall back to round-robin
    state = load_rotation_state(state_file)
    org_id, org, state = round_robin_select(weekday, roster, state, today)
    state = record_aired(org_id, today, state)
    save_rotation_state(state, state_file)

    return {
        "org_id": org_id,
        "org_name": org["name"],
        "org_short_name": org["short_name"],
        "org_description": org["description"],
        "org_website": org.get("website"),
        "psa_angle": None,
        "event_name": None,
        "source": "rotation",
        "notable_dates": todays_notable,
    }


if __name__ == "__main__":
    import sys
    from config_loader import load_podcast_profile

    name = sys.argv[1] if len(sys.argv) > 1 else "sample"
    result = select_psa(load_podcast_profile(name))
    if result:
        if result.get("org_name"):
            print(f"Today's PSA: {result['org_name']}")
            print(f"  Source: {result['source']}")
            if result["event_name"]:
                print(f"  Event: {result['event_name']}")
            if result["psa_angle"]:
                print(f"  Angle: {result['psa_angle']}")
            else:
                print(f"  Description: {result['org_description']}")
        if result.get("notable_dates"):
            print("  Notable dates today:")
            for nd in result["notable_dates"]:
                print(f"    - {nd['name']}: {nd['note']}")
    else:
        print("PSAs disabled or no organizations configured for this profile.")
