from types import SimpleNamespace

from src.services.lobby_sync import desired_room_values, plan_room_update


def _settings(**kw):
    base = dict(draw_interval=4, lobby_countdown_seconds=30, min_cartelas_to_start=2, cards_per_player=4, cartela_min=1, cartela_max=250)
    base.update(kw)
    return SimpleNamespace(**base)


def _room(taken, **kw):
    base = dict(draw_interval=4, countdown_seconds=30, min_cartelas_to_start=2, stake_amount=10, cards_per_player=4,
                cartela_min=1, cartela_max=250, taken_cartelas=taken)
    base.update(kw)
    return SimpleNamespace(**base)


def test_empty_lobby_takes_every_change():
    want = desired_room_values(_settings(draw_interval=6, lobby_countdown_seconds=20, cards_per_player=2), {"stake_amount": 25})
    fields, deferred = plan_room_update(_room([]), want)
    assert fields == {"draw_interval": 6, "countdown_seconds": 20, "stake_amount": 25, "cards_per_player": 2}
    assert deferred == []


def test_stake_and_cards_wait_once_players_hold_cartelas():
    want = desired_room_values(_settings(draw_interval=6, cards_per_player=2), {"stake_amount": 25})
    fields, deferred = plan_room_update(_room([7, 9]), want)
    assert fields == {"draw_interval": 6}
    assert sorted(deferred) == ["cards_per_player", "stake_amount"]


def test_nothing_to_do_when_in_sync():
    want = desired_room_values(_settings(), {"stake_amount": 10})
    assert plan_room_update(_room([3]), want) == ({}, [])
