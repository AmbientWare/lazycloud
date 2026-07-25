from __future__ import annotations

from sqlalchemy.exc import IntegrityError


def is_integrity_error(exc: BaseException) -> bool:
    return isinstance(exc, IntegrityError)
