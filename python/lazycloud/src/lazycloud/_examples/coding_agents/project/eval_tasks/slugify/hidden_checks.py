from slug import slugify


def test_accents_are_removed() -> None:
    assert slugify("Crème Brûlée") == "creme-brulee"


def test_separator_runs_collapse_and_ends_are_trimmed() -> None:
    assert slugify("  --Rock & Roll!!  2024-- ") == "rock-roll-2024"


def test_nothing_left_is_empty() -> None:
    assert slugify("!!!") == ""
