"""
Tests for `_reshape_winners` / `_room_summary` in src.api.main -
backward-compat handling for any room rows created before the Supabase
migration, back when a winner's grid had to be stored flattened (Firestore
disallows an array directly containing another array). New rows store the
grid as a normal nested 5x5 array directly, so this only matters for old
data. Pure function, no database access needed.
"""
from src.api.main import _reshape_winners


def _flat_grid(start=0):
    return [{"value": start + i, "marked": True, "free": False} for i in range(25)]


def test_reshape_winners_rebuilds_5x5_grid_from_flat():
    winners = [
        {
            "player_id": "player-1",
            "card_id": "card-1",
            "cartela_number": 7,
            "grid_flat": _flat_grid(),
            "pattern": "any_line",
            "payout": 80,
        }
    ]
    reshaped = _reshape_winners(winners)
    assert len(reshaped) == 1
    grid = reshaped[0]["grid"]
    assert len(grid) == 5
    assert all(len(row) == 5 for row in grid)
    # Row-major order preserved.
    assert grid[0][0]["value"] == 0
    assert grid[4][4]["value"] == 24
    # The flattened intermediate field is not leaked to API consumers.
    assert "grid_flat" not in reshaped[0]


def test_reshape_winners_handles_empty_list():
    assert _reshape_winners([]) == []


def test_reshape_winners_leaves_winner_without_grid_untouched():
    """Defensive: a winner record that somehow has no grid data at all
    (e.g. an older/legacy record) must not crash - just pass through."""
    winners = [{"player_id": "player-1", "cartela_number": 3, "payout": 50}]
    reshaped = _reshape_winners(winners)
    assert reshaped == winners


def test_reshape_winners_does_not_mutate_input():
    original = [{"player_id": "player-1", "grid_flat": _flat_grid()}]
    import copy
    snapshot = copy.deepcopy(original)
    _reshape_winners(original)
    assert original == snapshot
