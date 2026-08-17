from __future__ import annotations

import pytest
import shared.realtime.streams
from foundation.stubs import (
    StubScopedContainerPrefix,
    extract_stub_id_from_container_id,
    extract_stub_id_from_stub_scoped_container_id,
    parse_stub_scoped_container_id,
)


def test_stub_scoped_container_id_parser_accepts_current_prefixes() -> None:
    stub_id = "5e3e31ff-aef4-40b6-a98d-439268a9832e"

    expected_prefixes = {
        "sandbox": StubScopedContainerPrefix.Sandbox,
        "pod": StubScopedContainerPrefix.Pod,
        "endpoint": StubScopedContainerPrefix.Endpoint,
    }
    for prefix, expected in expected_prefixes.items():
        container_id = f"{prefix}-{stub_id}-1717f4fc"
        parsed = parse_stub_scoped_container_id(container_id)

        assert parsed is not None
        assert parsed.prefix is expected
        assert parsed.stub_id == stub_id
        assert parsed.suffix == "1717f4fc"
        assert extract_stub_id_from_stub_scoped_container_id(container_id) == stub_id


def test_stub_scoped_container_id_parser_rejects_function_and_invalid_ids() -> None:
    stub_id = "5e3e31ff-aef4-40b6-a98d-439268a9832e"

    assert extract_stub_id_from_container_id(f"function-{stub_id}-1717f4fc") == stub_id
    assert extract_stub_id_from_stub_scoped_container_id(f"function-{stub_id}-1717f4fc") == ""
    assert parse_stub_scoped_container_id("sandbox-not-enough-parts") is None
    assert parse_stub_scoped_container_id("sandbox") is None
    with pytest.raises(ValueError, match="invalid container id"):
        extract_stub_id_from_container_id("sandbox-not-enough-parts")


def test_stub_scoped_container_id_parser_preserves_hyphenated_suffixes_and_event_api() -> None:
    stub_id = "5e3e31ff-aef4-40b6-a98d-439268a9832e"
    container_id = f"endpoint-{stub_id}-suffix-with-hyphens"

    parsed = parse_stub_scoped_container_id(container_id)

    assert parsed is not None
    assert parsed.stub_id == stub_id
    assert parsed.suffix == "suffix-with-hyphens"
    assert (
        shared.realtime.streams.extract_stub_id_from_stub_scoped_container_id(container_id)
        == stub_id
    )
