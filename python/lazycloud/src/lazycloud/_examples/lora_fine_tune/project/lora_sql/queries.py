"""Run SQL in a throwaway SQLite database and compare what queries return.

Model output is untrusted, so every query gets its own in-memory database,
cannot attach files and stops at a time limit.
"""

import re
import sqlite3
import time
from collections import Counter

from pydantic import BaseModel

QUERY_SECONDS = 2.0
MAX_ROWS = 100

type Cell = str | int | float | None

_PARENTHESIZED = re.compile(r"\([^()]*\)")
_ORDER_BY = re.compile(r"\border\s+by\b", re.IGNORECASE)


class SchemaError(Exception):
    """The database statements failed, so no query against them can be judged."""


class QueryError(Exception):
    """The query failed: bad syntax, an unknown column, or the time limit."""


class QueryRows(BaseModel):
    columns: list[str]
    rows: list[list[Cell]]
    truncated: bool


def run_query(context: str, sql: str, *, seconds: float = QUERY_SECONDS) -> QueryRows:
    """Create the database from `context`, run one query on it, and return up to MAX_ROWS rows."""
    deadline = time.monotonic() + seconds
    connection = sqlite3.connect(":memory:")
    try:
        connection.set_authorizer(_deny_attach)
        connection.set_progress_handler(lambda: time.monotonic() > deadline, 10_000)
        try:
            connection.executescript(context)
        except sqlite3.Error as exc:
            raise SchemaError(str(exc)) from exc
        try:
            cursor = connection.execute(sql)
            rows = cursor.fetchmany(MAX_ROWS + 1)
        except sqlite3.Error as exc:
            raise QueryError(str(exc)) from exc
        columns = [column[0] for column in cursor.description or []]
    finally:
        connection.close()
    return QueryRows(
        columns=columns,
        rows=[[_cell(value) for value in row] for row in rows[:MAX_ROWS]],
        truncated=len(rows) > MAX_ROWS,
    )


def execution_match(context: str, reference_sql: str, predicted_sql: str) -> bool:
    """Whether the predicted query returns the same rows as the reference query.

    Column names may differ. Row order counts only when the reference query
    orders its result.
    """
    expected = run_query(context, reference_sql)
    try:
        actual = run_query(context, predicted_sql)
    except QueryError:
        return False
    want = [_comparable(row) for row in expected.rows]
    got = [_comparable(row) for row in actual.rows]
    if _orders_result(reference_sql):
        return want == got
    return Counter(want) == Counter(got)


def _deny_attach(action: int, *_: str | None) -> int:
    # ATTACH would let a query create or read files outside its database.
    return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_ATTACH else sqlite3.SQLITE_OK


def _cell(value: object) -> Cell:
    if isinstance(value, bytes):
        return value.hex()
    if value is None or isinstance(value, str | int | float):
        return value
    return str(value)


def _comparable(row: list[Cell]) -> tuple[Cell, ...]:
    # SQLite returns 2 for COUNT and 2.0 for SUM; both answer the question.
    return tuple(
        round(float(value), 4) if isinstance(value, int | float) else value for value in row
    )


def _orders_result(sql: str) -> bool:
    """Whether ORDER BY applies to the whole result, not a subquery or window."""
    outer = sql
    while (inner := _PARENTHESIZED.sub("", outer)) != outer:
        outer = inner
    return _ORDER_BY.search(outer) is not None
