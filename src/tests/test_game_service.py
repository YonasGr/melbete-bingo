"""
Test Card Generation and Game Logic
"""
import pytest
from src.services.game_service import CardGenerator, DrawEngine, PatternVerifier


class TestCardGenerator:
    """Test card generation"""
    
    def test_generate_75_ball_card(self):
        """Test 75-ball card generation"""
        card = CardGenerator.generate_75_ball_card()
        
        # Check dimensions
        assert len(card) == 5
        assert all(len(row) == 5 for row in card)
        
        # Check center is free
        center = card[2][2]
        assert center["free"] is True
        assert center["marked"] is True
        assert center["value"] is None
        
        # Check number ranges
        for row_idx, row in enumerate(card):
            for col_idx, cell in enumerate(row):
                if not cell["free"] and cell["value"] is not None:
                    # Check column ranges
                    if col_idx == 0:  # B
                        assert 1 <= cell["value"] <= 15
                    elif col_idx == 1:  # I
                        assert 16 <= cell["value"] <= 30
                    elif col_idx == 2:  # N
                        assert 31 <= cell["value"] <= 45
                    elif col_idx == 3:  # G
                        assert 46 <= cell["value"] <= 60
                    elif col_idx == 4:  # O
                        assert 61 <= cell["value"] <= 75
        
        # Check uniqueness
        numbers = []
        for row in card:
            for cell in row:
                if cell["value"] is not None:
                    numbers.append(cell["value"])
        assert len(numbers) == len(set(numbers))
    
    def test_generate_90_ball_card(self):
        """Test 90-ball card generation"""
        card = CardGenerator.generate_90_ball_card()
        
        # Check dimensions
        assert len(card) == 3
        assert all(len(row) == 9 for row in card)
        
        # Check each row has 5 numbers
        for row in card:
            numbers_count = sum(1 for cell in row if cell["value"] is not None)
            assert numbers_count == 5
        
        # Check number ranges and uniqueness
        all_numbers = []
        for row in card:
            for cell in row:
                if cell["value"] is not None:
                    assert 1 <= cell["value"] <= 90
                    all_numbers.append(cell["value"])
        
        assert len(all_numbers) == len(set(all_numbers))


class TestDrawEngine:
    """Test draw engine"""
    
    def test_initialize_draw_pool(self):
        """Test draw pool initialization"""
        pool, seed = DrawEngine.initialize_draw_pool(1, 75)
        
        assert len(pool) == 75
        assert set(pool) == set(range(1, 76))
        assert seed is not None
    
    def test_draw_number(self):
        """Test drawing numbers"""
        pool, _ = DrawEngine.initialize_draw_pool(1, 10)
        
        drawn = []
        for _ in range(10):
            number = DrawEngine.draw_number(pool)
            assert number is not None
            assert 1 <= number <= 10
            drawn.append(number)
        
        # All numbers should be drawn
        assert set(drawn) == set(range(1, 11))
        
        # Pool should be empty
        assert DrawEngine.draw_number(pool) is None
    
    def test_deterministic_shuffle(self):
        """Test that same seed produces same shuffle"""
        pool1, seed = DrawEngine.initialize_draw_pool(1, 20, seed="test123")
        pool2, _ = DrawEngine.initialize_draw_pool(1, 20, seed="test123")
        
        assert pool1 == pool2


class TestPatternVerifier:
    """Test pattern verification"""
    
    def test_verify_horizontal_line(self):
        """Test horizontal line pattern"""
        # Create a card with first row marked
        card = []
        for row in range(5):
            card_row = []
            for col in range(5):
                if row == 2 and col == 2:
                    cell = {"value": None, "marked": True, "free": True}
                else:
                    cell = {"value": row * 5 + col, "marked": row == 0, "free": False}
                card_row.append(cell)
            card.append(card_row)
        
        assert PatternVerifier.verify_pattern(card, "horizontal_line", "75") is True
    
    def test_verify_vertical_line(self):
        """Test vertical line pattern"""
        # Create a card with first column marked
        card = []
        for row in range(5):
            card_row = []
            for col in range(5):
                if row == 2 and col == 2:
                    cell = {"value": None, "marked": True, "free": True}
                else:
                    cell = {"value": row * 5 + col, "marked": col == 0, "free": False}
                card_row.append(cell)
            card.append(card_row)
        
        assert PatternVerifier.verify_pattern(card, "vertical_line", "75") is True
    
    def test_verify_claim_valid(self):
        """Test valid claim verification"""
        card = []
        called_numbers = []
        
        # Create a card with first row marked
        for row in range(5):
            card_row = []
            for col in range(5):
                if row == 2 and col == 2:
                    cell = {"value": None, "marked": True, "free": True}
                else:
                    value = row * 5 + col + 1
                    marked = row == 0
                    cell = {"value": value, "marked": marked, "free": False}
                    if marked:
                        called_numbers.append(value)
                card_row.append(cell)
            card.append(card_row)
        
        is_valid, message = PatternVerifier.verify_claim(
            card, called_numbers, "horizontal_line", "75"
        )
        assert is_valid is True
    
    def test_verify_claim_ignores_client_marked_flag(self):
        """The server derives marks from called_numbers, not from
        whatever 'marked' flags happen to be on the card - so a claim
        with fabricated/stale marks for numbers that were never actually
        called must still be rejected as no valid pattern."""
        card = []
        called_numbers = [1, 2, 3]  # only 3 of row 0's 5 numbers were called

        # Row 0 is flagged "marked" client-side even though 4 and 5 were
        # never called - this must NOT be treated as a valid line.
        for row in range(5):
            card_row = []
            for col in range(5):
                if row == 2 and col == 2:
                    cell = {"value": None, "marked": True, "free": True}
                else:
                    value = row * 5 + col + 1
                    marked = row == 0
                    cell = {"value": value, "marked": marked, "free": False}
                card_row.append(cell)
            card.append(card_row)

        is_valid, message = PatternVerifier.verify_claim(
            card, called_numbers, "horizontal_line", "75"
        )
        assert is_valid is False
        assert "no winning pattern" in message.lower()

    def test_verify_claim_valid_even_without_client_marked_flags(self):
        """The flip side of the fix: a card whose stored 'marked' flags are
        all False must still be accepted as a valid win if every number in
        a line has actually been called - this was the core bug (server
        rejected real wins because it trusted stale/never-updated marked
        flags instead of the authoritative called_numbers list)."""
        card = []
        called_numbers = [1, 2, 3, 4, 5]  # exactly row 0

        for row in range(5):
            card_row = []
            for col in range(5):
                if row == 2 and col == 2:
                    cell = {"value": None, "marked": True, "free": True}
                else:
                    value = row * 5 + col + 1
                    cell = {"value": value, "marked": False, "free": False}  # never marked!
                card_row.append(cell)
            card.append(card_row)

        is_valid, message = PatternVerifier.verify_claim(
            card, called_numbers, "horizontal_line", "75"
        )
        assert is_valid is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
