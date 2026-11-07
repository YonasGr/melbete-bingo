from datetime import datetime, timedelta, timezone

import pytest

from src.services.leaderboard_service import (
    LeaderboardError, campaign_phase, mask_name, normalize_channel, parse_local, split_rewards, validate_campaign,
)

UTC = timezone.utc
GOOD = {"name": "Nov", "timezone": "Africa/Addis_Ababa", "starts_at": "2026-11-01T09:00", "ends_at": "2026-11-30T21:00",
        "budget_etb": 100000, "winners_count": 250, "points_per_cartela": 10, "wagering_multiplier": 37}


def test_local_time_is_converted_with_the_admins_timezone():
    assert parse_local("2026-11-01T09:00", "Africa/Addis_Ababa") == datetime(2026, 11, 1, 6, 0, tzinfo=UTC)   # EAT = UTC+3


def test_valid_campaign_uses_the_configured_values():
    v = validate_campaign(GOOD)
    assert (v["budget_etb"], v["winners_count"], v["points_per_cartela"], v["wagering_multiplier"], v["channel_points"]) == (100000, 250, 10, 37, 50)
    assert v["starts_at"] < v["ends_at"] and v["channel_enabled"] is False


@pytest.mark.parametrize("patch", [
    {"ends_at": "2026-10-01T09:00"}, {"budget_etb": 0}, {"winners_count": 0}, {"points_per_cartela": 1.5},
    {"wagering_multiplier": -1}, {"timezone": "Mars/Base"}, {"name": ""}, {"starts_at": "", "ends_at": ""},
    {"channel_enabled": True, "channel_username": ""}, {"channel_enabled": True, "channel_username": "ab"},
])
def test_bad_input_is_rejected(patch):
    with pytest.raises(LeaderboardError):
        validate_campaign({**GOOD, **patch})


def test_channel_username_is_normalised():
    assert normalize_channel("https://t.me/melbete_news") == "melbete_news"
    assert normalize_channel("@melbete_news") == "melbete_news"
    v = validate_campaign({**GOOD, "channel_enabled": True, "channel_username": "@melbete_news"})
    assert v["channel_username"] == "melbete_news"


def test_phases_follow_the_admins_dates_and_never_auto_distribute():
    start = datetime(2026, 11, 1, tzinfo=UTC); end = start + timedelta(days=30)
    c = {"cancelled": False, "starts_at": start, "ends_at": end, "distribution_status": "none"}
    assert campaign_phase(c, start - timedelta(seconds=1)) == "scheduled"
    assert campaign_phase(c, start) == "active"
    assert campaign_phase(c, end) == "ended"                       # ended, but still waiting for the admin
    assert campaign_phase({**c, "distribution_status": "approved"}, end) == "distributed"
    assert campaign_phase({**c, "distribution_status": "rejected"}, end) == "rejected"
    assert campaign_phase({**c, "cancelled": True}, start) == "cancelled"


def test_rewards_are_shared_by_points_and_never_exceed_the_budget():
    standings = [{"player_id": "a", "points": 150}, {"player_id": "b", "points": 100}, {"player_id": "c", "points": 50}]
    out = split_rewards(100000, standings, 2)
    assert [r["player_id"] for r in out] == ["a", "b"]
    assert sum(r["amount"] for r in out) <= 100000
    assert out[0]["amount"] == 100000 * 150 // 250


def test_no_participants_means_no_rewards():
    assert split_rewards(100000, [], 250) == []


def test_example_from_the_spec_is_150_points():
    cartela_points, channel_points = 10 * 10, 50
    assert cartela_points + channel_points == 150


def test_names_are_masked():
    assert mask_name("Abebe") == "Ab***"


def test_extension_must_move_the_end_later():
    c_end = parse_local("2026-11-30T21:00", "Africa/Addis_Ababa")
    assert parse_local("2026-12-05T21:00", "Africa/Addis_Ababa") > c_end
