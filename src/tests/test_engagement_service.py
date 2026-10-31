"""Engagement: streak rewards, round-starting nudges, win-back free rounds, reminders."""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.services.engagement_service import EngagementService, eat_day, parse_streak_rewards

UTC = timezone.utc


def cfg(**over):
    base = dict(
        engagement_streak_rewards="3:10,7:30", engagement_winback_days=7, engagement_winback_bonus_etb=10,
        engagement_winback_cooldown_days=30, engagement_winback_daily_budget_etb=500,
        engagement_nudge_cooldown_hours=6, engagement_nudge_max_recipients=100,
        engagement_nudge_batch_gap_minutes=10, engagement_loop_minutes=30,
        engagement_quiet_start_hour=22, engagement_quiet_end_hour=7,
    )
    base.update(over)
    return SimpleNamespace(**base)


class FakeWallets:
    def __init__(self, fail_for=()):
        self.credits = []  # (player_id, amount, tx_type)
        self.fail_for = set(fail_for)

    async def credit_engagement_bonus(self, player_id, amount, tx_type, note):
        if player_id in self.fail_for:
            raise RuntimeError("wallet down")
        self.credits.append((player_id, amount, tx_type))


class FakeRepo:
    def __init__(self, wallets):
        self.wallets = wallets
        self.state = {}
        self.lapsed, self.nudgeable, self.reminders = [], [], []

    def _s(self, pid):
        return self.state.setdefault(pid, {"streak_days": 0, "best_streak": 0, "last_play_day": None,
                                           "last_rewarded_streak": 0, "last_nudge_at": None,
                                           "last_winback_at": None, "last_streak_reminder_day": None})

    async def record_play(self, pid, day, now):
        s = self._s(pid)
        last = s["last_play_day"]
        if last == day:
            pass
        elif last is not None and (day - last).days == 1:
            s["streak_days"] += 1
        else:
            s["streak_days"], s["last_rewarded_streak"] = 1, 0
        s["best_streak"] = max(s["best_streak"], s["streak_days"])
        s["last_play_day"] = day
        return dict(s)

    async def claim_streak_reward(self, pid, milestone):
        s = self._s(pid)
        if s["last_rewarded_streak"] < milestone:
            s["last_rewarded_streak"] = milestone
            return True
        return False

    async def lapsed_candidates(self, lapsed_before, not_older_than, cooldown_cutoff, limit):
        return self.lapsed[:limit]

    async def claim_winback(self, pid, cutoff, now):
        s = self._s(pid)
        if s["last_winback_at"] is None or s["last_winback_at"] < cutoff:
            s["last_winback_at"] = now
            return True
        return False

    async def release_winback(self, pid, previous):
        self._s(pid)["last_winback_at"] = previous

    async def winback_paid_since(self, since):
        return sum(a for _p, a, t in self.wallets.credits if t == "winback_bonus")

    async def nudge_candidates(self, active_since, cutoff, exclude, limit):
        return [r for r in self.nudgeable if r["player_id"] not in exclude][:limit]

    async def claim_nudge(self, pid, cutoff, now):
        s = self._s(pid)
        if s["last_nudge_at"] is None or s["last_nudge_at"] < cutoff:
            s["last_nudge_at"] = now
            return True
        return False

    async def streak_reminder_candidates(self, today, limit):
        return self.reminders[:limit]

    async def claim_streak_reminder(self, pid, today):
        s = self._s(pid)
        if s["last_streak_reminder_day"] is None or s["last_streak_reminder_day"] < today:
            s["last_streak_reminder_day"] = today
            return True
        return False


class Clock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now


def make(start=datetime(2026, 10, 19, 9, 0, tzinfo=UTC), fail_wallet_for=(), **cfg_over):  # 12:00 EAT
    wallets = FakeWallets(fail_wallet_for)
    repo = FakeRepo(wallets)
    sent = []
    clock = Clock(start)
    svc = EngagementService(repo=repo, wallets=wallets, cfg=cfg(**cfg_over),
                            notify=lambda pid, t, d, k=None: sent.append((pid, t, d, k)), now_fn=clock)
    return svc, repo, wallets, sent, clock


def test_parse_streak_rewards_ignores_garbage():
    assert parse_streak_rewards("3:10, 7:30") == {3: 10, 7: 30}
    assert parse_streak_rewards("") == {}
    assert parse_streak_rewards("x:1,3:0,0:5,4:") == {}
    assert parse_streak_rewards("3:10,bad,5:20") == {3: 10, 5: 20}


def test_days_and_quiet_hours_follow_ethiopia_time():
    assert eat_day(datetime(2026, 10, 19, 21, 30, tzinfo=UTC)) == date(2026, 10, 20)  # 00:30 EAT next day
    svc, *_ = make()
    # UTC hour -> is it quiet (22:00-07:00 EAT = 19:00-04:00 UTC)?
    for utc_hour, quiet in ((18, False), (19, True), (23, True), (3, True), (4, False), (9, False)):
        assert svc.in_quiet_hours(datetime(2026, 10, 19, utc_hour, 0, tzinfo=UTC)) is quiet, utc_hour


@pytest.mark.asyncio
async def test_streak_builds_one_day_at_a_time_and_pays_each_milestone_once():
    svc, repo, wallets, sent, clock = make()
    for day in range(1, 8):
        await svc.record_round_played({"p1"})
        await svc.record_round_played({"p1"})  # a second round the same day changes nothing
        assert repo.state["p1"]["streak_days"] == day
        clock.now += timedelta(days=1)
    assert wallets.credits == [("p1", 10, "streak_reward"), ("p1", 30, "streak_reward")]
    assert [t for _p, t, _d, _k in sent] == ["streak_reward", "streak_reward"]
    assert sent[0][2] == {"days": 3, "amount": 10}


@pytest.mark.asyncio
async def test_a_missed_day_resets_the_streak_and_the_rewards_start_over():
    svc, repo, wallets, sent, clock = make()
    for _ in range(3):
        await svc.record_round_played({"p1"})
        clock.now += timedelta(days=1)
    assert len(wallets.credits) == 1
    clock.now += timedelta(days=2)  # skipped days
    await svc.record_round_played({"p1"})
    assert repo.state["p1"]["streak_days"] == 1 and repo.state["p1"]["last_rewarded_streak"] == 0
    for _ in range(2):
        clock.now += timedelta(days=1)
        await svc.record_round_played({"p1"})
    assert len(wallets.credits) == 2  # the 3-day reward is earnable again in the new run


@pytest.mark.asyncio
async def test_one_players_failure_does_not_stop_the_others():
    svc, repo, wallets, sent, clock = make(fail_wallet_for={"p1"}, engagement_streak_rewards="1:5")
    await svc.record_round_played({"p1", "p2"})
    assert wallets.credits == [("p2", 5, "streak_reward")]


@pytest.mark.asyncio
async def test_no_streak_rewards_when_disabled():
    svc, repo, wallets, sent, clock = make(engagement_streak_rewards="")
    for _ in range(8):
        await svc.record_round_played({"p1"})
        clock.now += timedelta(days=1)
    assert wallets.credits == []


@pytest.mark.asyncio
async def test_nudge_goes_to_recent_players_outside_the_room_once_per_cooldown():
    svc, repo, wallets, sent, clock = make()
    repo.nudgeable = [{"player_id": "a"}, {"player_id": "b"}, {"player_id": "in_room"}]
    assert await svc.nudge_round_starting("real_10", 10, ["in_room"], 30) == 2
    assert sorted(p for p, t, *_ in sent if t == "room_filling") == ["a", "b"]
    # a second lobby right away: batch gap blocks it ...
    assert await svc.nudge_round_starting("real_10", 10, [], 30) == 0
    # ... and after the gap the same players are still inside their 6h cooldown
    clock.now += timedelta(minutes=11)
    assert await svc.nudge_round_starting("real_10", 10, [], 30) == 0
    clock.now += timedelta(hours=7)
    assert await svc.nudge_round_starting("real_10", 10, [], 30) == 2


@pytest.mark.asyncio
async def test_nudge_respects_quiet_hours_and_recipient_cap():
    svc, repo, wallets, sent, clock = make(start=datetime(2026, 10, 19, 20, 0, tzinfo=UTC))  # 23:00 EAT
    repo.nudgeable = [{"player_id": "a"}]
    assert await svc.nudge_round_starting("real_10", 10, [], 30) == 0 and sent == []
    svc, repo, wallets, sent, clock = make(engagement_nudge_max_recipients=2)
    repo.nudgeable = [{"player_id": str(i)} for i in range(10)]
    assert await svc.nudge_round_starting("real_10", 10, [], 30) == 2


@pytest.mark.asyncio
async def test_winback_pays_bonus_once_per_player_and_notifies():
    svc, repo, wallets, sent, clock = make()
    repo.lapsed = [{"player_id": "a"}, {"player_id": "b"}]
    assert (await svc.run_cycle())["winback"] == 2
    assert wallets.credits == [("a", 10, "winback_bonus"), ("b", 10, "winback_bonus")]
    assert {p for p, t, *_ in sent if t == "winback_bonus"} == {"a", "b"}
    assert (await svc.run_cycle())["winback"] == 0  # still inside the 30-day cooldown


@pytest.mark.asyncio
async def test_winback_never_exceeds_the_daily_budget():
    svc, repo, wallets, sent, clock = make(engagement_winback_daily_budget_etb=25)
    repo.lapsed = [{"player_id": str(i)} for i in range(10)]
    assert (await svc.run_cycle())["winback"] == 2  # 25 // 10
    assert sum(a for _p, a, _t in wallets.credits) <= 25
    clock.now += timedelta(minutes=30)
    assert (await svc.run_cycle())["winback"] == 0  # budget used up for today


@pytest.mark.asyncio
async def test_winback_failure_releases_the_claim_so_the_player_is_not_skipped():
    svc, repo, wallets, sent, clock = make(fail_wallet_for={"a"})
    repo.lapsed = [{"player_id": "a"}]
    assert (await svc.run_cycle())["winback"] == 0
    assert repo.state["a"]["last_winback_at"] is None and sent == []
    wallets.fail_for.clear()
    assert (await svc.run_cycle())["winback"] == 1


@pytest.mark.asyncio
async def test_winback_off_when_amount_or_days_is_zero_and_in_quiet_hours():
    for over in ({"engagement_winback_bonus_etb": 0}, {"engagement_winback_days": 0}):
        svc, repo, wallets, sent, clock = make(**over)
        repo.lapsed = [{"player_id": "a"}]
        assert (await svc.run_cycle())["winback"] == 0
    svc, repo, wallets, sent, clock = make(start=datetime(2026, 10, 19, 20, 0, tzinfo=UTC))
    repo.lapsed = [{"player_id": "a"}]
    assert (await svc.run_cycle())["skipped"] == "quiet_hours" and wallets.credits == []


@pytest.mark.asyncio
async def test_streak_reminder_is_evening_only_once_a_day_and_names_the_next_reward():
    svc, repo, wallets, sent, clock = make(start=datetime(2026, 10, 19, 9, 0, tzinfo=UTC))  # 12:00 EAT
    repo.reminders = [{"player_id": "a", "streak_days": 2}]
    assert (await svc.run_cycle())["reminders"] == 0  # too early in the day
    clock.now = datetime(2026, 10, 19, 15, 0, tzinfo=UTC)  # 18:00 EAT
    assert (await svc.run_cycle())["reminders"] == 1
    assert (await svc.run_cycle())["reminders"] == 0  # once per day
    _pid, type_, data, _k = [x for x in sent if x[1] == "streak_at_risk"][0]
    assert data == {"streak": 2, "next_days": 3, "next_amount": 10}
