import pytest
from durations import parse_duration


def test_units_can_be_skipped() -> None:
    assert parse_duration("2h5s") == 7205
    assert parse_duration("45s") == 45


@pytest.mark.parametrize("text", ["", "5", "5x", "1m1h", "1h1h", "-5s", "h"])
def test_malformed_durations_are_rejected(text: str) -> None:
    with pytest.raises(ValueError):
        parse_duration(text)
