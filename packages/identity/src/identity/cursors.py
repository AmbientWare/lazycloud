from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from pydantic import ValidationError
from shared.contracts import ContractModel
from shared.errors import InvalidInputError


class _CreatedAtCursorPayload(ContractModel):
    created_at: datetime
    id: UUID


def encode_created_at_cursor(created_at: datetime, id: str) -> str:
    payload = {"created_at": created_at.isoformat(), "id": id}
    return base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).decode()


def decode_created_at_cursor[CursorT](
    value: str | None,
    *,
    build: Callable[[datetime, str], CursorT],
    subject: str,
) -> CursorT | None:
    if not value:
        return None
    try:
        payload = _CreatedAtCursorPayload.model_validate_json(
            base64.urlsafe_b64decode(value.encode()),
            strict=True,
        )
    except (binascii.Error, ValidationError) as exc:
        raise InvalidInputError(f"invalid {subject} cursor") from exc
    created_at = payload.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return build(created_at, str(payload.id))
