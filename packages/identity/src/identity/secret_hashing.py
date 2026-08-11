from __future__ import annotations

import hashlib
import hmac

_ENCODING = "pbkdf2_sha256"


def pbkdf2_encode(secret: str, salt: str, *, iterations: int) -> str:
    """The stored form of a secret: one definition of how a hash is written down.

    The work factor belongs to whoever owns the secret, because what it defends
    against differs by secret. The encoding does not: a second copy of it is a
    second place the salt separator, the digest, or the algorithm label could
    drift from what already-stored rows use.
    """
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        secret.encode("utf-8"),
        salt.encode("utf-8"),
        iterations,
    ).hex()
    return f"{_ENCODING}${salt}${digest}"


def pbkdf2_matches(secret: str, encoded: str, *, iterations: int) -> bool:
    """Compare in constant time; an unparseable stored hash matches nothing."""
    try:
        _, salt, expected = encoded.split("$", 2)
    except ValueError:
        return False
    actual = pbkdf2_encode(secret, salt, iterations=iterations).split("$", 2)[2]
    return hmac.compare_digest(actual, expected)


__all__ = ["pbkdf2_encode", "pbkdf2_matches"]
