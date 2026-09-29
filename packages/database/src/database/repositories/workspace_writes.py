from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from datetime import datetime
from uuid import UUID

from database.tables.base import DatabaseBase
from database.tables.identity import WorkspaceTable
from pydantic import JsonValue
from shared.identity import WorkspaceStatus
from sqlalchemy import cast, column, func, literal, select, values
from sqlalchemy.dialects.postgresql import Insert, insert
from sqlalchemy.orm import class_mapper


def workspace_fenced_insert(
    model: type[DatabaseBase],
    records: Sequence[Mapping[str, JsonValue | datetime]],
    *,
    workspace_ids: Collection[str],
) -> Insert:
    records = sorted(records, key=lambda record: UUID(str(record["id"])))
    workspace_ids = {str(UUID(workspace_id)) for workspace_id in workspace_ids}
    if not workspace_ids:
        return insert(model).values(records)
    mapper = class_mapper(model)
    columns = [mapper.column_attrs[name].columns[0] for name in records[0]]
    incoming = (
        values(*(column(item.name, item.type) for item in columns))
        .data(
            [
                tuple(
                    cast(literal(record[name], type_=item.type), item.type)
                    for name, item in zip(records[0], columns, strict=True)
                )
                for record in records
            ]
        )
        .cte("incoming_owned_rows")
    )
    owners = (
        select(WorkspaceTable.id)
        .where(
            WorkspaceTable.id.in_(workspace_ids),
            WorkspaceTable.status == WorkspaceStatus.Active.value,
        )
        .order_by(WorkspaceTable.id)
        .with_for_update(read=True, key_share=True)
        .cte("active_workspace_owners")
        .prefix_with("MATERIALIZED")
    )
    # Gate the whole batch: an unavailable owner must not leave other rows
    # written when the caller handles the resulting domain error.
    source = (
        select(*incoming.c)
        .where(select(func.count()).select_from(owners).scalar_subquery() == len(workspace_ids))
        .order_by(incoming.c.id)
    )
    return insert(model).from_select(columns, source)
