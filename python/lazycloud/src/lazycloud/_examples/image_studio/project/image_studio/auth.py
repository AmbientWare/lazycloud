"""The studio key: a bearer check on API calls, and signed URLs for what a
browser loads without headers, its images and its progress socket."""

import hashlib
import hmac
from urllib.parse import urlencode

MIN_KEY_LENGTH = 16


class WeakKeyError(ValueError):
    pass


def check_key_strength(key: str) -> str:
    if len(key) < MIN_KEY_LENGTH:
        raise WeakKeyError(f"the studio key needs at least {MIN_KEY_LENGTH} characters")
    return key


def bearer_matches(authorization: str | None, key: str) -> bool:
    scheme, _, token = (authorization or "").partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(token.encode(), key.encode())


def _signature(key: str, path: str, expires: int) -> str:
    message = f"{path}\n{expires}".encode()
    return hmac.new(key.encode(), message, hashlib.sha256).hexdigest()


def sign_path(key: str, path: str, *, now: float, ttl_seconds: int) -> str:
    expires = int(now) + ttl_seconds
    query = urlencode({"expires": expires, "signature": _signature(key, path, expires)})
    return f"{path}?{query}"


def signature_valid(key: str, path: str, *, expires: int, signature: str, now: float) -> bool:
    return now < expires and hmac.compare_digest(signature, _signature(key, path, expires))
