from src.services.admin_search import clean_query, like_pattern, phone_pattern


def test_like_pattern_matches_special_characters_literally():
    assert like_pattern("50%_off") == "%50\\%\\_off%"
    assert like_pattern("  abc ") == "%abc%"


def test_phone_pattern_uses_the_last_nine_digits():
    assert phone_pattern("+251 91 234 5678") == "%912345678%"
    assert phone_pattern("0912345678") == "%912345678%"
    assert phone_pattern("1234") == "%1234%"
    assert phone_pattern("abc12") is None


def test_query_is_trimmed_and_capped():
    assert clean_query("  hi  ") == "hi"
    assert len(clean_query("x" * 500)) == 100
