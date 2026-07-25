from __future__ import annotations

from typing import Any

from lazycloud.values import decode_value, encode_value


def test_json_safe_values_are_stored_as_inspectable_json() -> None:
    values: list[Any] = [
        {"a": 1},
        [1, 2, 3],
        "text",
        42,
        3.5,
        None,
        True,
        {"nested": {"x": ["y"]}},
    ]
    for value in values:
        encoded = encode_value(value)
        assert not encoded or encoded[0] != 0x80
        assert decode_value(encoded) == value


def test_non_json_values_fall_back_to_pickle_and_round_trip() -> None:
    values: list[Any] = [(1, 2), {1: 2}, {"blob": b"x"}, {"mixed": (1, "a")}]
    for value in values:
        encoded = encode_value(value)
        assert encoded[0] == 0x80
        assert decode_value(encoded) == value


def test_empty_payload_decodes_to_none() -> None:
    assert decode_value(b"") is None
