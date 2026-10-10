from pagination import page_count


def test_full_pages() -> None:
    assert page_count(40, 20) == 2
