from __future__ import annotations

from dataclasses import dataclass, field

import cloudpickle
import pytest
from lazycloud.abstractions.map import Map
from shared.errors import InvalidInputError
from shared.http.collections import (
    MAX_MAP_TTL_SECONDS,
    MapCountResponse,
    MapDeleteResponse,
    MapGetResponse,
    MapKeysResponse,
    MapSetResponse,
    encode_bytes,
)


@dataclass(frozen=True)
class Payload:
    number: int
    labels: tuple[str, ...]


@dataclass
class FakeMapClient:
    values: dict[tuple[str, str], bytes] = field(default_factory=dict)
    expirations: dict[tuple[str, str], int] = field(default_factory=dict)
    fail_set_message: str = ""

    def set(
        self,
        name: str,
        key: str,
        value: bytes,
        *,
        ttl_seconds: int = MAX_MAP_TTL_SECONDS,
    ) -> MapSetResponse:
        if self.fail_set_message:
            raise InvalidInputError(self.fail_set_message)
        storage_key = (name, key)
        self.values[storage_key] = value
        self.expirations[storage_key] = ttl_seconds
        return MapSetResponse()

    def get(self, name: str, key: str) -> MapGetResponse:
        storage_key = (name, key)
        if storage_key not in self.values:
            from shared.errors import NotFoundError

            raise NotFoundError(f"map key not found: {key}")
        return MapGetResponse(value_base64=encode_bytes(self.values[storage_key]))

    def delete(self, name: str, key: str) -> MapDeleteResponse:
        storage_key = (name, key)
        self.values.pop(storage_key, None)
        self.expirations.pop(storage_key, None)
        return MapDeleteResponse()

    def count(self, name: str) -> MapCountResponse:
        return MapCountResponse(count=len(self._keys(name)))

    def keys(self, name: str, *, cursor: str = "", search: str = "") -> MapKeysResponse:
        return MapKeysResponse(data=self._keys(name))

    def delete_map(self, name: str) -> None:
        keys = [storage_key for storage_key in self.values if storage_key[0] == name]
        for storage_key in keys:
            self.values.pop(storage_key, None)
            self.expirations.pop(storage_key, None)

    def _keys(self, name: str) -> list[str]:
        return sorted(key for map_name, key in self.values if map_name == name)


def test_map_serializes_python_values_and_tracks_ttl() -> None:
    client = FakeMapClient()
    mapping = Map("cache")._bind_control(client)
    payload = Payload(number=42, labels=("gpu", "batch"))

    assert mapping.set("answer", payload, ttl=45) is True

    assert client.expirations[("cache", "answer")] == 45
    assert cloudpickle.loads(client.values[("cache", "answer")]) == payload
    assert mapping["answer"] == payload
    assert mapping.get("answer") == payload
    assert len(mapping) == 1
    assert list(mapping) == ["answer"]
    assert list(mapping.items()) == [("answer", payload)]

    mapping.delete()
    assert list(mapping) == []


def test_map_get_and_delete_behave_like_mapping() -> None:
    client = FakeMapClient()
    mapping = Map("cache")._bind_control(client)

    mapping.set("first", {"value": 1}, ttl=30)

    assert client.expirations[("cache", "first")] == 30
    assert mapping.get("missing") is None
    assert mapping.get("missing", "fallback") == "fallback"
    assert mapping["missing"] is None
    assert "missing" not in mapping
    with pytest.raises(KeyError):
        del mapping["missing"]

    del mapping["first"]
    assert len(mapping) == 0

    mapping["none"] = None
    del mapping["none"]
    assert "none" not in mapping


def test_map_rejects_invalid_ttl_and_set_failures() -> None:
    mapping = Map("cache")._bind_control(FakeMapClient())

    with pytest.raises(ValueError, match="non-negative"):
        mapping.set("bad", "value", ttl=-1)
    with pytest.raises(ValueError, match="cannot exceed"):
        mapping.set("bad", "value", ttl=MAX_MAP_TTL_SECONDS + 1)

    failing = Map("cache")._bind_control(FakeMapClient(fail_set_message="ttl too large"))
    with pytest.raises(InvalidInputError, match="ttl too large"):
        failing.set("bad", "value")
