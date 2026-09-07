from __future__ import annotations

from dataclasses import dataclass

from shared.image_building.constants import IMAGE_ARCHIVE_KEY_PREFIX

IMAGE_ARCHIVE_EXTENSION = "rclip"
DEFAULT_IMAGE_ARCHIVE_PRESIGN_SECONDS = 15 * 60


@dataclass(frozen=True, slots=True)
class ImageArchiveSettings:
    bucket: str
    presign_seconds: int = DEFAULT_IMAGE_ARCHIVE_PRESIGN_SECONDS

    def physical_key(self, object_key: str) -> str:
        if not object_key.startswith(f"{IMAGE_ARCHIVE_KEY_PREFIX}/"):
            raise ValueError("image archive object key must use the shared archive prefix")
        return object_key
