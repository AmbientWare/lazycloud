import pytest
from shared.container_requests import RequestMountPointConfig
from shared.mounts import normalize_mount_prefix


def test_mount_prefix_normalization_is_deterministic() -> None:
    assert normalize_mount_prefix("") == ""
    assert normalize_mount_prefix("/models/releases") == "models/releases/"
    assert normalize_mount_prefix("models/releases/") == "models/releases/"
    assert (
        RequestMountPointConfig(
            bucket_name="customer-data",
            prefix="/models/releases",
        ).prefix
        == "models/releases/"
    )


def test_mount_prefix_rejects_parent_segments() -> None:
    with pytest.raises(ValueError, match=r"cannot contain '\.\.'"):
        RequestMountPointConfig(
            bucket_name="customer-data",
            prefix="models/../private",
        )
