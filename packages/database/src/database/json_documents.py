from __future__ import annotations

import json

from pydantic import JsonValue
from shared.errors import InvalidInputError
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Dialect
from sqlalchemy.exc import DontWrapMixin
from sqlalchemy.sql.operators import OperatorType
from sqlalchemy.types import TypeDecorator, TypeEngine

DEFAULT_DOCUMENT_MAX_BYTES = 1024 * 1024
type JsonOperand = JsonValue | tuple[str | int, ...]


class JsonDocumentError(InvalidInputError, DontWrapMixin):
    """Keep document validation a client error when SQLAlchemy binds a write."""


class JsonDocument(TypeDecorator[JsonValue]):
    impl = JSONB
    cache_ok = True

    def __init__(
        self,
        *,
        max_bytes: int = DEFAULT_DOCUMENT_MAX_BYTES,
        none_as_null: bool = False,
    ) -> None:
        super().__init__(none_as_null=none_as_null)
        self.max_bytes = max_bytes
        self.none_as_null = none_as_null

    def coerce_compared_value(
        self, op: OperatorType | None, value: JsonOperand
    ) -> TypeEngine[JsonOperand]:
        return self.impl_instance.coerce_compared_value(op, value)

    def process_bind_param(self, value: JsonValue, dialect: Dialect) -> JsonValue:
        try:
            encoded = json.dumps(
                value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
        except (ValueError, TypeError) as exc:
            raise JsonDocumentError("document must contain valid JSON values") from exc
        if len(encoded) > self.max_bytes:
            raise JsonDocumentError(f"document exceeds the {self.max_bytes}-byte storage limit")
        return value
