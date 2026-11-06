from datetime import datetime, timezone

from src.services.engagement_service import parse_streak_rewards
from src.services.missions import day_start_utc


def test_mission_day_starts_at_midnight_ethiopia_time():
    # 22:30 UTC on 1 Nov is 01:30 on 2 Nov in Addis, so the mission day began at 21:00 UTC on 1 Nov
    assert day_start_utc(datetime(2026, 11, 1, 22, 30, tzinfo=timezone.utc)) == datetime(2026, 11, 1, 21, 0, tzinfo=timezone.utc)
    assert day_start_utc(datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)) == datetime(2026, 10, 31, 21, 0, tzinfo=timezone.utc)


def test_mission_config_format():
    assert parse_streak_rewards("3:20,5:40,10:80") == {3: 20, 5: 40, 10: 80}
    assert parse_streak_rewards("") == {}
