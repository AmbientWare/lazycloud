from __future__ import annotations

import keyword
import re

APP_SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


def validate_app_slug(value: str) -> str:
    slug = value.strip()
    if not APP_SLUG_PATTERN.fullmatch(slug) or keyword.iskeyword(slug):
        msg = (
            "app slug must be a lowercase Python identifier segment, "
            "start with a letter, and contain only lowercase letters, digits, or underscores"
        )
        raise ValueError(msg)
    return slug


def app_slug_from_name(value: str) -> str:
    raw = value.strip().lower()
    slug = re.sub(r"[^a-z0-9_]+", "_", raw).strip("_")
    if not slug or not slug[0].isalpha():
        slug = f"app_{slug or 'default'}"
    if len(slug) > 63:
        slug = slug[:63].rstrip("_")
    if keyword.iskeyword(slug):
        slug = f"{slug}_app"
    return validate_app_slug(slug)


def app_slug_or_default(value: str | None, *, default: str) -> str:
    selected = (value or "").strip()
    if selected:
        return validate_app_slug(selected)
    return app_slug_from_name(default)


__all__ = [
    "APP_SLUG_PATTERN",
    "app_slug_from_name",
    "app_slug_or_default",
    "validate_app_slug",
]
