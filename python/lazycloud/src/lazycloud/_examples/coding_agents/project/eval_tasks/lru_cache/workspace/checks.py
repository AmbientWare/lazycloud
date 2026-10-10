from cache import LRUCache


def test_stored_values_come_back() -> None:
    cache = LRUCache(2)
    cache.put("a", 1)
    assert cache.get("a") == 1
    assert cache.get("b") is None
