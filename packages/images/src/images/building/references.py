from __future__ import annotations

import re
import shlex
from collections.abc import Callable

from shared.image_building.authoring import ImageSpec

from images.building.constants import DEFAULT_IMAGE_BASE, DOCKER_HUB_REGISTRY
from images.building.models import ImageBuildSourcePlan, ImageSourceReference

_DIGEST_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*(?:[-_+.][A-Za-z][A-Za-z0-9]*)*:[0-9A-Fa-f]{32,}$")
_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
_REPOSITORY_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_FROM_LINE_RE = re.compile(
    r"^(?P<prefix>\s*FROM(?:\s+--[^\s]+)*\s+)(?P<image>[^\s]+)(?P<suffix>.*)$",
    re.IGNORECASE,
)


def base_image_source_image(registry: str, name: str, tag: str) -> str:
    if not registry or not name or not tag:
        return ""
    return f"{registry.rstrip('/')}/{name.lstrip('/')}:{tag}"


def parse_image_source_reference(image_ref: str) -> ImageSourceReference:
    value = image_ref.strip()
    if not value:
        msg = "image reference cannot be blank"
        raise ValueError(msg)
    if "://" in value:
        msg = f"image reference must not include a URL scheme: {image_ref}"
        raise ValueError(msg)

    if "@" in value:
        if value.count("@") > 1:
            msg = f"invalid image digest reference: {image_ref}"
            raise ValueError(msg)
        repository_ref, digest = value.rsplit("@", 1)
    else:
        repository_ref, digest = value, ""
    if digest and _DIGEST_RE.fullmatch(digest) is None:
        msg = f"invalid image digest reference: {image_ref}"
        raise ValueError(msg)

    registry = DOCKER_HUB_REGISTRY
    remainder = repository_ref
    if "/" in repository_ref:
        first, rest = repository_ref.split("/", 1)
        if _is_registry_component(first):
            registry = first
            remainder = rest

    tag = ""
    last_component = remainder.rsplit("/", 1)[-1]
    if ":" in last_component:
        repository, tag = remainder.rsplit(":", 1)
    else:
        repository = remainder

    if not repository or repository.startswith(":") or repository.endswith(":"):
        msg = f"invalid image reference: {image_ref}"
        raise ValueError(msg)
    _validate_repository(repository, image_ref)
    if tag and _TAG_RE.fullmatch(tag) is None:
        msg = f"invalid image tag in reference: {image_ref}"
        raise ValueError(msg)
    if not tag and not digest:
        tag = "latest"

    return ImageSourceReference(
        registry=registry,
        repository=repository,
        tag=tag,
        digest=digest,
    )


def image_build_source_plan(image: ImageSpec) -> ImageBuildSourcePlan:
    if image.dockerfile:
        base = dockerfile_base_image(image.dockerfile)
        reference = parse_image_source_reference(base) if base else None
        return ImageBuildSourcePlan(
            custom_dockerfile=True,
            reference=reference,
            reason="custom Dockerfile owns its FROM instruction",
        )
    reference = parse_image_source_reference(image.base or DEFAULT_IMAGE_BASE)
    return ImageBuildSourcePlan(
        source_image=reference.source_image,
        reference=reference,
        custom_dockerfile=False,
        reason="registry base image is used as build source",
    )


def dockerfile_base_image(dockerfile: str) -> str:
    for raw_line in dockerfile.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if not line.upper().startswith("FROM "):
            continue
        try:
            parts = shlex.split(line)
        except ValueError as exc:
            msg = "invalid Dockerfile FROM instruction"
            raise ValueError(msg) from exc
        for token in parts[1:]:
            if token.startswith("--"):
                continue
            return token
    return ""


def pin_dockerfile_base_images(
    dockerfile: str,
    resolve: Callable[[ImageSourceReference], str],
) -> str:
    stage_names: set[str] = set()
    pinned_lines: list[str] = []
    for raw_line in dockerfile.splitlines(keepends=True):
        ending = "\n" if raw_line.endswith("\n") else ""
        line = raw_line.removesuffix("\n")
        match = _FROM_LINE_RE.match(line)
        if match is None:
            pinned_lines.append(raw_line)
            continue
        image = match.group("image")
        if image.lower() in stage_names or image.lower() == "scratch":
            pinned = image
        else:
            if "$" in image:
                raise ValueError(
                    f"Dockerfile FROM variables must be resolved before image build: {image}"
                )
            pinned = resolve(parse_image_source_reference(image))
        suffix = match.group("suffix")
        pinned_lines.append(f"{match.group('prefix')}{pinned}{suffix}{ending}")
        stage_name = _dockerfile_stage_name(suffix)
        if stage_name:
            stage_names.add(stage_name.lower())
    return "".join(pinned_lines)


def _dockerfile_stage_name(suffix: str) -> str:
    try:
        parts = shlex.split(suffix)
    except ValueError as exc:
        raise ValueError("invalid Dockerfile FROM instruction") from exc
    if len(parts) >= 2 and parts[-2].upper() == "AS":
        return parts[-1]
    return ""


def _is_registry_component(value: str) -> bool:
    return value == "localhost" or "." in value or ":" in value


def _validate_repository(repository: str, image_ref: str) -> None:
    for component in repository.split("/"):
        if _REPOSITORY_COMPONENT_RE.fullmatch(component) is None:
            msg = f"invalid image repository in reference: {image_ref}"
            raise ValueError(msg)
