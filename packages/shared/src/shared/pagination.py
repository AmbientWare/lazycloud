from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Generic, TypeVar

from pydantic import Field, field_validator

from shared.contracts import ContractModel
from shared.enums import StringEnum

T = TypeVar("T")


class SortDirection(StringEnum):
    Asc = "asc"
    Desc = "desc"


CURSOR_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S.%f %z"


class PageCursor(ContractModel):
    offset: int = 0
    limit: int = 50

    @field_validator("offset")
    @classmethod
    def offset_cannot_be_negative(cls, value: int) -> int:
        if value < 0:
            msg = "offset cannot be negative"
            raise ValueError(msg)
        return value

    @field_validator("limit")
    @classmethod
    def limit_must_be_positive(cls, value: int) -> int:
        if value <= 0:
            msg = "limit must be positive"
            raise ValueError(msg)
        return value


class RepositoryPage(ContractModel, Generic[T]):
    items: list[T] = Field(default_factory=list)
    next: PageCursor | None = None
    total: int = 0


class DatetimeCursor(ContractModel):
    value: str
    id: int

    @classmethod
    def from_datetime(cls, value: datetime, *, id: int) -> DatetimeCursor:
        return cls(value=value.strftime(CURSOR_TIMESTAMP_FORMAT), id=id)

    def parsed_datetime(self) -> datetime:
        return datetime.strptime(self.value, CURSOR_TIMESTAMP_FORMAT)


def paginate_items(items: list[T], cursor: PageCursor | None = None) -> RepositoryPage[T]:
    current = cursor or PageCursor()
    sliced = items[current.offset : current.offset + current.limit]
    next_offset = current.offset + len(sliced)
    next_cursor = (
        PageCursor(offset=next_offset, limit=current.limit) if next_offset < len(items) else None
    )
    return RepositoryPage(items=sliced, next=next_cursor, total=len(items))


def cursor_operator(sort_order: SortDirection | str) -> str:
    normalized = SortDirection(str(sort_order).lower())
    if normalized is SortDirection.Asc:
        return ">"
    return "<"


def encode_datetime_cursor(cursor: DatetimeCursor) -> str:
    payload: dict[str, str | int] = {"id": cursor.id, "value": cursor.value}
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.b64encode(raw).decode()


def decode_datetime_cursor(cursor: str) -> DatetimeCursor | None:
    if cursor == "":
        return None
    raw = base64.b64decode(cursor.encode())
    return DatetimeCursor.model_validate_json(raw)


__all__ = [
    "CURSOR_TIMESTAMP_FORMAT",
    "DatetimeCursor",
    "PageCursor",
    "RepositoryPage",
    "SortDirection",
    "cursor_operator",
    "decode_datetime_cursor",
    "encode_datetime_cursor",
    "paginate_items",
]
