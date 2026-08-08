from __future__ import annotations

import hashlib
import hmac
import re
import secrets

from database.repositories.identity import normalize_username
from shared.errors import InvalidInputError

_PASSWORD_ITERATIONS = 600_000
"""Separate from the token work factor so the two can move independently.

A password is chosen by a person and is guessable; a token is 256 bits of urandom
and is not. The password therefore carries the higher cost.
"""

_PASSWORD_MIN_LENGTH = 8
"""A floor, not a strength policy. Length is the only property checked here because
composition rules push people toward predictable substitutions without adding
entropy; what actually bounds guessing is the rate limit on the sign-in route."""

_PASSWORD_MAX_LENGTH = 1024
_USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")

ABSENT_USER_HASH = f"pbkdf2_sha256$absent${'0' * 64}"
"""Verified against when no user matched, so a missing username and a wrong password
cost the same and cannot be told apart by timing them."""


def hash_password(password: str, salt: str | None = None) -> str:
    password_salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        password_salt.encode("utf-8"),
        _PASSWORD_ITERATIONS,
    ).hex()
    return f"pbkdf2_sha256${password_salt}${digest}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        _, salt, expected = encoded.split("$", 2)
    except ValueError:
        return False
    actual = hash_password(password, salt).split("$", 2)[2]
    return hmac.compare_digest(actual, expected)


def validate_password(password: str) -> str:
    if len(password) < _PASSWORD_MIN_LENGTH:
        raise InvalidInputError(f"password must be at least {_PASSWORD_MIN_LENGTH} characters")
    if len(password) > _PASSWORD_MAX_LENGTH:
        raise InvalidInputError(f"password must be at most {_PASSWORD_MAX_LENGTH} characters")
    return password


def validate_username(username: str) -> str:
    normalized = normalize_username(username)
    if not _USERNAME_PATTERN.match(normalized):
        raise InvalidInputError(
            "username must be 3-64 characters of lowercase letters, digits, dot, "
            "dash, or underscore, and start with a letter or digit"
        )
    return normalized


__all__ = [
    "ABSENT_USER_HASH",
    "hash_password",
    "validate_password",
    "validate_username",
    "verify_password",
]
