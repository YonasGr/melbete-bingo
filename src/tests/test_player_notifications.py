"""Player event notifications: templates, inbox storage, dedupe and Telegram delivery status."""
import asyncio

import pytest

from src.models.schemas import Player
from src.services import player_notifications as pn
from src.services.player_notifications import TEMPLATES, PlayerNotifier, render


class FakeRepo:
    def __init__(self, fail=False):
        self.rows = {}
        self.keys = set()
        self.fail = fail

    async def create(self, nid, player_id, type_, title, body, data, dedupe_key, created_at):
        if self.fail:
            raise RuntimeError("db down")
        if dedupe_key and (player_id, dedupe_key) in self.keys:
            return None
        if dedupe_key:
            self.keys.add((player_id, dedupe_key))
        row = {"id": nid, "player_id": player_id, "type": type_, "title": title, "body": body,
               "data": data, "telegram_status": "pending"}
        self.rows[nid] = row
        return row

    async def set_telegram_status(self, nid, status):
        self.rows[nid]["telegram_status"] = status


class FakePlayers:
    def __init__(self, *players):
        self.players = {p.id: p for p in players}

    async def get_by_id(self, pid):
        return self.players.get(pid)


def make(sender_result="sent", fail_repo=False, players=None):
    sent = []

    async def sender(chat_id, text, open_app=False):
        sent.append((chat_id, text))
        if isinstance(sender_result, Exception):
            raise sender_result
        return sender_result

    players = players or [Player(id="tg_111", telegram_id="111", display_name="Abebe")]
    n = PlayerNotifier(repo=FakeRepo(fail=fail_repo), players=FakePlayers(*players), sender=sender)
    return n, sent


def test_every_template_renders_a_title_and_body():
    sample = {"amount": 1500, "note": "blurry screenshot", "payout": 900, "cartela_number": 7, "shared_with": 3,
              "bonus_note": "First-deposit reward: +100 bonus ETB (10%).", "reason": "Welcome"}
    for type_ in TEMPLATES:
        emoji, title, body = render(type_, sample)
        assert emoji and title and body, type_
    assert render("unknown_type", {})[1] == "Notification"


def test_message_content_for_the_important_events():
    assert "1,500 ETB" in render("deposit_approved", {"amount": 1500})[2]
    assert "refunded" in render("withdrawal_rejected", {"amount": 300})[2]
    _, title, body = render("win", {"payout": 900, "cartela_number": 7, "shared_with": 3})
    assert title == "You won - shared prize!" and "900 ETB" in body and "2 other winner" in body
    assert render("win", {"payout": 900, "shared_with": 1})[1] == "You won!"
    assert "Reason: blurry" in render("deposit_rejected", {"amount": 10, "note": "blurry"})[2]


@pytest.mark.asyncio
async def test_notify_stores_inbox_row_and_sends_to_telegram():
    n, sent = make()
    nid = await n.notify("tg_111", "deposit_approved", {"amount": 500}, dedupe_key="deposit:1")
    assert nid
    row = n.repo.rows[nid]
    assert row["telegram_status"] == "sent" and row["title"] == "Deposit approved"
    assert sent[0][0] == 111 and "500 ETB" in sent[0][1]


@pytest.mark.asyncio
async def test_same_event_is_notified_only_once():
    n, sent = make()
    assert await n.notify("tg_111", "deposit_approved", {"amount": 500}, dedupe_key="deposit:1")
    assert await n.notify("tg_111", "deposit_approved", {"amount": 500}, dedupe_key="deposit:1") is None
    assert len(sent) == 1 and len(n.repo.rows) == 1


@pytest.mark.asyncio
async def test_blocked_and_failed_deliveries_are_recorded_but_inbox_row_stays():
    n, _ = make(sender_result="blocked")
    nid = await n.notify("tg_111", "win", {"payout": 100})
    assert n.repo.rows[nid]["telegram_status"] == "blocked"

    n, _ = make(sender_result=RuntimeError("telegram down"))
    nid = await n.notify("tg_111", "win", {"payout": 100})
    assert n.repo.rows[nid]["telegram_status"] == "failed"


@pytest.mark.asyncio
async def test_bots_and_players_without_a_telegram_id_get_inbox_only():
    n, sent = make(players=[Player(id="bot_1", telegram_id="1", display_name="Bot", is_bot=True),
                            Player(id="web_1", telegram_id="", display_name="Web")])
    for pid in ("bot_1", "web_1", "missing"):
        nid = await n.notify(pid, "win", {"payout": 1})
        assert n.repo.rows[nid]["telegram_status"] == "skipped"
    assert sent == []


@pytest.mark.asyncio
async def test_send_telegram_false_only_writes_the_inbox():
    n, sent = make()
    nid = await n.notify("tg_111", "bonus_granted", {"amount": 50}, send_telegram=False)
    assert n.repo.rows[nid]["telegram_status"] == "skipped" and sent == []


@pytest.mark.asyncio
async def test_storage_failure_never_raises():
    n, sent = make(fail_repo=True)
    assert await n.notify("tg_111", "win", {"payout": 1}) is None
    assert sent == []


@pytest.mark.asyncio
async def test_schedule_notify_runs_in_background_and_never_raises(monkeypatch):
    n, sent = make()
    monkeypatch.setattr(pn, "get_notifier", lambda: n)
    pn.schedule_notify("tg_111", "win", {"payout": 10}, dedupe_key="win:r1:tg_111")
    await asyncio.sleep(0.05)
    assert len(sent) == 1


def test_schedule_notify_without_an_event_loop_is_a_silent_noop():
    pn.schedule_notify("tg_111", "win", {"payout": 10})  # must not raise
