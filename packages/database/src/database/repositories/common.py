from __future__ import annotations

from collections.abc import Collection
from datetime import datetime

from sqlalchemy import BigInteger, cast, extract, func, select
from sqlalchemy.orm import InstrumentedAttribute, Session
from sqlalchemy.sql.elements import ColumnElement


def names_by_id(
    session: Session,
    id_column: InstrumentedAttribute[str],
    name_column: InstrumentedAttribute[str],
    ids: Collection[str],
) -> dict[str, str]:
    """What each of these rows is called, keyed by id.

    Ids absent from the result are rows that are gone entirely, which is a
    different fact from a row that has no name and reads differently at every
    caller: a label is left empty rather than invented.
    """

    if not ids:
        return {}
    rows = session.execute(select(id_column, name_column).where(id_column.in_(set(ids)))).all()
    return {str(row[0]): str(row[1]) for row in rows}


def bucket_index(
    column: InstrumentedAttribute[datetime],
    *,
    start: datetime,
    width_seconds: int,
) -> ColumnElement[int]:
    """Which whole `width_seconds` interval after `start` a row's timestamp falls in.

    Never `date_trunc` or `func.date`: those read the session `TimeZone`, which
    nothing here sets, so the interval a row belonged to would depend on which
    connection answered the request. Counting whole widths elapsed from a start
    the caller supplied makes the interval arithmetic on that start instead, and
    a caller opening its window on a UTC boundary reads UTC days.

    Rows outside `[start, start + n * width_seconds)` still index — negatively
    before the start — so the window predicate remains the caller's to state.
    """

    elapsed = extract("epoch", column) - int(start.timestamp())
    return cast(func.floor(elapsed / width_seconds), BigInteger)
