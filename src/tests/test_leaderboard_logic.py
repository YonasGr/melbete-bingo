from datetime import datetime, timedelta, timezone

import pytest

from src.repositories.leaderboard_repository import _RANK_ORDER
from src.services.leaderboard_service import (
    DEFAULT_REWARD_TIERS, LeaderboardError, campaign_phase, mask_name, normalize_channel, parse_local, split_ladder,
    split_rewards, tier_for_rank, tiers_total, validate_campaign, validate_tiers, wagering_summary,
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


# ----------------------------------------------------------------------------- monthly ladder (250 winners / 100,000 ETB)
LADDER = {**GOOD, "reward_tiers": DEFAULT_REWARD_TIERS}


def test_default_ladder_matches_the_spec():
    assert tiers_total(DEFAULT_REWARD_TIERS) == 100_000
    assert DEFAULT_REWARD_TIERS[-1]["to"] == 250
    assert [tier_for_rank(DEFAULT_REWARD_TIERS, r)["amount"] for r in (1, 2, 3, 4, 10, 11, 50, 51, 100, 101, 200, 201, 250)] == \
        [20000, 10000, 5000, 1000, 1000, 600, 600, 400, 400, 100, 100, 80, 80]
    assert tier_for_rank(DEFAULT_REWARD_TIERS, 251) is None


def test_ladder_decides_the_number_of_winners():
    v = validate_campaign({**LADDER, "winners_count": 5})
    assert v["winners_count"] == 250 and v["reward_tiers"][0] == {"from": 1, "to": 1, "amount": 20000}
    assert v["points_per_cartela"] == 10


def test_legacy_campaign_without_a_ladder_still_validates():
    assert validate_campaign(GOOD)["reward_tiers"] is None


@pytest.mark.parametrize("tiers", [
    [{"from": 2, "to": 5, "amount": 10}],                                          # must start at rank 1
    [{"from": 1, "to": 3, "amount": 10}, {"from": 5, "to": 6, "amount": 10}],      # gap
    [{"from": 1, "to": 3, "amount": 10}, {"from": 3, "to": 6, "amount": 10}],      # overlap
    [{"from": 1, "to": 3, "amount": 10}, {"from": 4, "to": 2, "amount": 10}],      # backwards
    [{"from": 1, "to": 1, "amount": 100_001}],                                     # more than the budget
    [], "nope",
])
def test_bad_ladders_are_rejected(tiers):
    with pytest.raises(LeaderboardError):
        validate_tiers(tiers, 100_000)


def test_ladder_pays_each_band_and_never_more_than_the_budget():
    standings = [{"player_id": f"p{i}", "points": 1000 - i} for i in range(1, 301)]
    out = split_ladder(DEFAULT_REWARD_TIERS, standings)
    assert len(out) == 250 and out[0]["amount"] == 20000 and out[3]["amount"] == 1000 and out[-1]["amount"] == 80
    assert sum(r["amount"] for r in out) == 100_000


def test_ladder_with_fewer_players_than_places_pays_only_those_players():
    out = split_ladder(DEFAULT_REWARD_TIERS, [{"player_id": "a", "points": 50}, {"player_id": "b", "points": 40}])
    assert [(r["rank"], r["amount"]) for r in out] == [(1, 20000), (2, 10000)]


def test_ties_are_broken_by_reserved_etb_before_who_got_there_first():
    assert _RANK_ORDER.index("points") < _RANK_ORDER.index("reserved_etb") < _RANK_ORDER.index("last_at")


def test_wagering_tracker_matches_the_spec_example():
    rows = [{"status": "locked", "wager_required": 37_000, "wagered": 12_500, "bonus_won": 0}]
    w = wagering_summary(rows, 37)
    assert (w["required"], w["completed"], w["remaining"], w["percent"], w["status"]) == (37_000, 12_500, 24_500, 33.78, "in_progress")


def test_wagering_tracker_projects_before_a_reward_exists_and_unlocks_when_done():
    assert wagering_summary([], 37, estimated=1000)["required"] == 37_000
    done = wagering_summary([{"status": "released", "wager_required": 37_000, "wagered": 40_000, "bonus_won": 120}], 37)
    assert done["status"] == "eligible" and done["percent"] == 100.0 and done["remaining"] == 0
