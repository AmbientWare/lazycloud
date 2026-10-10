import pytest
from pagination import page_count


def test_a_partial_last_page_counts() -> None:
    assert page_count(41, 20) == 3


def test_no_items_need_no_pages() -> None:
    assert page_count(0, 20) == 0


def test_page_size_below_one_is_rejected() -> None:
    with pytest.raises(ValueError):
        page_count(10, 0)
