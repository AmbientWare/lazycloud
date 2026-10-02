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


__all__ = [
    "APP_SLUG_PATTERN",
    "validate_app_slug",
]
