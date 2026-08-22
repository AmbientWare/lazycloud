from __future__ import annotations

from enum import StrEnum

from shared.image_building.records import ImageBuildRecord

CURRENT_IMAGE_CLIP_VERSION = 2


class ImageMetadataAliasKind(StrEnum):
    Build = "build"
    CacheKey = "cache-key"
    Tag = "tag"
    PublishedRef = "published-ref"
    Artifact = "artifact"


def image_metadata_alias(kind: ImageMetadataAliasKind, value: str) -> str:
    normalized = value.strip()
    if not normalized:
        return ""
    return f"{kind.value}:{normalized}"


def image_metadata_aliases_for_build(build: ImageBuildRecord) -> list[str]:
    aliases = [
        image_metadata_alias(ImageMetadataAliasKind.Build, build.id),
        image_metadata_alias(ImageMetadataAliasKind.CacheKey, build.cache_key or build.fingerprint),
        image_metadata_alias(ImageMetadataAliasKind.Tag, build.tag or ""),
        image_metadata_alias(ImageMetadataAliasKind.PublishedRef, build.published_ref or ""),
        image_metadata_alias(ImageMetadataAliasKind.Artifact, build.artifact_path or ""),
    ]
    return [alias for alias in aliases if alias]


def merge_image_metadata_aliases(existing: list[str], additions: list[str]) -> list[str]:
    merged: dict[str, None] = {}
    for alias in [*existing, *additions]:
        normalized = alias.strip()
        if normalized:
            merged[normalized] = None
    return sorted(merged)
