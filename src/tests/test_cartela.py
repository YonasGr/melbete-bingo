"""Tests for the deterministic cartela-number card generator."""
import pytest
from src.services.game_service import CardGenerator


def test_cartela_center_is_free_space():
    """The center cell is a free space. Its value must stay inside the 1-75
    ball range (None, like the standard free cell) so a cartela number
    (which can be up to cartela_max, e.g. 500) can never leak into the
    called-numbers / matched-number display as a bogus "O240" style call."""
    card = CardGenerator.generate_cartela_card(42)
    center = card[2][2]
    assert center["value"] is None
    assert center["free"] is True
    assert center["marked"] is True


def test_cartela_layout_is_deterministic():
    """The same cartela number must always produce the exact same layout,
    every time, for every player - that's the whole point."""
    card_a = CardGenerator.generate_cartela_card(7)
    card_b = CardGenerator.generate_cartela_card(7)
    assert card_a == card_b


def test_different_cartelas_usually_differ():
    card_a = CardGenerator.generate_cartela_card(1)
    card_b = CardGenerator.generate_cartela_card(2)
    assert card_a != card_b


def test_cartela_column_ranges_respected():
    ranges = [(1, 15), (16, 30), (31, 45), (46, 60), (61, 75)]
    card = CardGenerator.generate_cartela_card(99)
    for col in range(5):
        lo, hi = ranges[col]
        for row in range(5):
            cell = card[row][col]
            if cell["free"]:
                continue
            assert lo <= cell["value"] <= hi


def test_cartela_number_out_of_range_rejected():
    with pytest.raises(ValueError):
        CardGenerator.generate_cartela_card(0)
    with pytest.raises(ValueError):
        CardGenerator.generate_cartela_card(501)
