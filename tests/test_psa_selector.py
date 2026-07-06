"""Tests for the profile-driven PSA selector."""

import json
from datetime import date

import psa_selector
from psa_selector import (
    find_active_events,
    find_notable_dates,
    get_orgs_for_weekday,
    load_rotation_state,
    round_robin_select,
    select_psa,
)


ORGS = {
    "food_bank": {"name": "Food Bank", "short_name": "FB",
                  "description": "Feeds people", "website": "https://fb.example"},
    "library": {"name": "Library", "short_name": "Lib",
                "description": "Books for all"},
}


def _profile(tmp_path, psa=True, orgs=ORGS, events=None, dates=None):
    """Build a profile whose psa_config points at files under tmp_path."""
    # psa_selector resolves paths against REPO_DIR; absolute paths pass through
    # (Path("/repo") / "/abs/x" == "/abs/x"), which keeps test data in tmp_path.
    cfg = {}
    (tmp_path / "orgs.json").write_text(json.dumps({"organizations": orgs}))
    cfg["organizations"] = str(tmp_path / "orgs.json")
    if events is not None:
        (tmp_path / "events.json").write_text(json.dumps({"events": events}))
        cfg["events"] = str(tmp_path / "events.json")
    if dates is not None:
        (tmp_path / "dates.json").write_text(json.dumps({"dates": dates}))
        cfg["notable_dates"] = str(tmp_path / "dates.json")
    return {"name": "t", "psa": psa, "psa_config": cfg}


def test_psa_disabled_returns_none(tmp_path):
    profile = _profile(tmp_path, psa=False)
    assert select_psa(profile, today=date(2026, 7, 5)) is None


def test_orgs_without_weekdays_are_always_eligible():
    roster = get_orgs_for_weekday(3, ORGS)
    assert len(roster) == 2


def test_orgs_with_weekdays_filter():
    orgs = {"a": {"name": "A", "weekdays": [0]}, "b": {"name": "B", "weekdays": [3]}}
    roster = get_orgs_for_weekday(3, orgs)
    assert [oid for oid, _ in roster] == ["b"]


def test_round_robin_advances_and_persists(tmp_path):
    state_file = tmp_path / "state.json"
    profile = _profile(tmp_path)
    # Same weekday (Sundays) so the rotation pointer for that weekday advances
    first = select_psa(profile, today=date(2026, 7, 5), state_file=state_file)
    second = select_psa(profile, today=date(2026, 8, 2), state_file=state_file)
    assert first["org_id"] != second["org_id"]
    assert first["source"] == "rotation"


def test_min_days_between_repeats(tmp_path):
    state = {"rotation": {}, "last_aired": {"food_bank": "2026-07-01"}}
    roster = list(ORGS.items())
    org_id, _, _ = round_robin_select(6, roster, state, date(2026, 7, 5))
    assert org_id == "library"  # food_bank aired 4 days ago (< 28)


def test_event_driven_selection_beats_rotation(tmp_path):
    events = [{
        "name": "Food Drive Day", "start_date": "07-05", "end_date": "07-05",
        "organization_id": "food_bank",
        "psa_angle": "It's Food Drive Day — support {org_name} at {org_website}",
    }]
    profile = _profile(tmp_path, events=events)
    result = select_psa(profile, today=date(2026, 7, 5),
                        state_file=tmp_path / "state.json")
    assert result["source"] == "event"
    assert result["org_id"] == "food_bank"
    assert "Food Bank" in result["psa_angle"]
    assert result["event_name"] == "Food Drive Day"


def test_long_events_not_teased_in_advance():
    events = [{
        "name": "Awareness Month", "start_date": "07-10", "end_date": "08-10",
        "all_orgs": True, "psa_angle": "It's {org_name} month",
    }]
    active = find_active_events(date(2026, 7, 5), events)
    assert active == []  # 31-day event starting in 5 days: wait for it to start


def test_notable_dates_matched(tmp_path):
    dates = [{"date": "07-05", "name": "Test Day", "note": "A day for tests"}]
    matches = find_notable_dates(date(2026, 7, 5), dates)
    assert matches == [{"name": "Test Day", "note": "A day for tests"}]


def test_no_orgs_and_no_dates_returns_none(tmp_path):
    profile = _profile(tmp_path, orgs={})
    assert select_psa(profile, today=date(2026, 7, 5),
                      state_file=tmp_path / "state.json") is None


def test_legacy_flat_state_migrates(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"0": 1, "3": 0}))
    state = load_rotation_state(state_file)
    assert state["rotation"] == {"0": 1, "3": 0}
    assert state["last_aired"] == {}
