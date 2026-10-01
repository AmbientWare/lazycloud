from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

import pytest
from lazycloud.abstractions.disk import Disk
from lazycloud.abstractions.map import Map
from lazycloud.abstractions.volume import CloudBucketConfig, Volume, volume_mounts
from shared.deployment_records import VolumeMount

ReturnT = TypeVar("ReturnT")


def _call_runtime(
    function: Callable[..., ReturnT],
    /,
    *args: object,
    **kwargs: object,
) -> ReturnT:
    return function(*args, **kwargs)


@dataclass
class ExportableVolume:
    name: str
    mount_path: str

    def export(self) -> VolumeMount:
        return VolumeMount(name=self.name, mount_path=self.mount_path)


def test_volume_mounts_accept_exportables_and_reject_invalid_items() -> None:
    explicit = VolumeMount(name="models", mount_path="/models")
    exported = ExportableVolume(name="data", mount_path="/data")

    assert volume_mounts([explicit, exported, Volume("cache")]) == [
        explicit,
        VolumeMount(name="data", mount_path="/data"),
        VolumeMount(name="cache", mount_path="/volumes/cache"),
    ]

    with pytest.raises(TypeError, match="unsupported volume type: object"):
        _call_runtime(volume_mounts, [object()])


@pytest.mark.parametrize("path", ["../escape.txt", "/etc/passwd", "a/../../b"])
def test_volume_refuses_paths_outside_the_volume_before_any_request(path: str) -> None:
    volume = Volume("data")

    with pytest.raises(ValueError, match="unsafe"):
        volume.write_text(path, "nope")
    with pytest.raises(ValueError, match="unsafe"):
        volume.move("safe.txt", path)


@pytest.mark.parametrize("ttl", [-1, 7 * 24 * 60 * 60 + 1])
def test_map_refuses_ttls_outside_seven_days_before_any_request(ttl: int) -> None:
    with pytest.raises(ValueError, match="ttl"):
        Map("cache").set("key", "value", ttl=ttl)


@pytest.mark.parametrize(
    ("size", "message"),
    [
        ("512Mi", "at least 1Gi"),
        ("2Ti", "at most 1Ti"),
        (1024**3 + 1, "multiple of 4096"),
    ],
)
def test_disk_sizes_are_bounded_whole_blocks(size: str | int, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        Disk("work", size=size).mount()


def test_cloud_bucket_config_rejects_partial_secret_references() -> None:
    with pytest.raises(ValueError, match="both be set or both be omitted"):
        CloudBucketConfig(access_key="ACCESS_SECRET")


def test_cloud_bucket_config_rejects_parent_prefix_segments() -> None:
    with pytest.raises(ValueError, match=r"cannot contain '\.\.'"):
        CloudBucketConfig(prefix="models/../private")
