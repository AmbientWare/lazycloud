from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar
from uuid import UUID

from database.tables.base import IdPayloadTable, NamedWorkspacePayloadTable, utc_now
from database.tables.identity import UserTable, WorkspaceTable
from pydantic import BaseModel, JsonValue, TypeAdapter
from shared.errors import NotFoundError
from shared.identity import UserRecord, UserStatus, WorkspaceRecord, WorkspaceStatus
from sqlalchemy import DateTime, Select, Uuid, select
from sqlalchemy.orm import Session, class_mapper
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.sql.base import ReadOnlyColumnCollection
from sqlalchemy.sql.elements import ColumnElement, KeyedColumnElement

TableRow = IdPayloadTable | NamedWorkspacePayloadTable
TableRowType = type[IdPayloadTable] | type[NamedWorkspacePayloadTable]
type JsonObject = dict[str, JsonValue]
type ColumnValue = JsonValue | datetime
type PayloadValue = (
    str
    | int
    | float
    | bool
    | None
    | datetime
    | BaseModel
    | Mapping[str, PayloadValue]
    | Sequence[PayloadValue]
)


def _lock_active_workspace(session: Session, workspace_id: str) -> None:
    row = session.scalars(
        select(WorkspaceTable)
        .where(WorkspaceTable.id == workspace_id)
        .with_for_update(read=True, key_share=True)
        .execution_options(populate_existing=True)
    ).first()
    if row is None:
        raise NotFoundError(f"workspace not found: {workspace_id}")
    workspace = WorkspaceRecord.model_validate(row.payload)
    if workspace.status is not WorkspaceStatus.Active:
        raise NotFoundError(f"workspace not found: {workspace_id}")


def _lock_active_user(session: Session, user_id: str) -> None:
    row = session.scalars(
        select(UserTable)
        .where(UserTable.id == user_id)
        .with_for_update(read=True, key_share=True)
        .execution_options(populate_existing=True)
    ).first()
    if row is None:
        raise NotFoundError(f"user not found: {user_id}")
    user = UserRecord.model_validate(row.payload)
    if user.status is not UserStatus.Active:
        raise NotFoundError(f"user not found: {user_id}")


_JSON_OBJECT_ADAPTER: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])
_JSON_VALUE_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
_COLUMN_VALUE_ADAPTER: TypeAdapter[ColumnValue] = TypeAdapter(ColumnValue)


@dataclass(frozen=True, slots=True)
class TableRepositoryConfig[TModel: BaseModel]:
    table: TableRowType
    model_type: type[TModel]
    key_field: str = "id"


@dataclass(slots=True)
class _TableRecordStore[TModel: BaseModel]:
    """Shared row mapping for payload tables; only scoped subclasses are public.

    ``_scope_column`` names the column that carries ownership for this repository's
    tables — one per owner kind, so the scoping guarantee reads the same whether the
    owner is a workspace or a user, and a table cannot silently lose it by moving
    between them.
    """

    session: Session
    config: TableRepositoryConfig[TModel]

    _scope_column: ClassVar[str | None] = None

    def _create(
        self,
        payload: Mapping[str, PayloadValue],
        *,
        scope_id: str | None,
        name: str | None,
        status: str | None,
    ) -> TModel:
        data = _json_object(payload)
        now = utc_now()
        row = self._new_row(_payload_key(data, self.config.key_field), data, now)
        self.session.add(row)
        apply_common_columns(
            row,
            data,
            scope_column=self._scope_column,
            scope_id=scope_id,
            name=name,
            status=status,
        )
        self.session.flush()
        _copy_generated_columns(row, data, self.config.model_type.model_fields.keys())
        model = self.config.model_type.model_validate(data)
        row.payload = _json_object(model)
        flag_modified(row, "payload")
        self.session.flush()
        return model

    def _upsert(
        self,
        model: TModel,
        *,
        key: str | None,
        scope_id: str | None,
        scoped: bool,
        name: str | None,
        status: str | None,
    ) -> TModel:
        # Creates validate; upserts did not, and `model_copy(update=...)` is how
        # this codebase mutates a record — it is documented to skip validation,
        # so a caller passing a serialized value for a typed field persisted a
        # record whose in-memory type contradicted its own annotation.
        #
        # `dict(model)` reads raw field values without invoking the serializer.
        # `model_dump(mode="python")` would serialize, and on a drifted record
        # that emits the very warning this guard exists to prevent.
        model = self.config.model_type.model_validate(dict(model))
        payload = _json_object(model)
        lookup_key = key or _payload_key(payload, self.config.key_field)
        row = (
            self._get_row(lookup_key, scope_id=scope_id, scoped=scoped)
            if lookup_key is not None
            else None
        )
        now = utc_now()
        if row is None:
            row = self._new_row(lookup_key, payload, now)
            self.session.add(row)

        row.payload = payload
        row.updated_at = now
        apply_common_columns(
            row,
            payload,
            scope_column=self._scope_column,
            scope_id=scope_id,
            name=name,
            status=status,
        )
        self.session.flush()
        return model

    def _get(
        self,
        key: str,
        *,
        scope_id: str | None,
        scoped: bool,
    ) -> TModel | None:
        row = self._get_row(key, scope_id=scope_id, scoped=scoped)
        if row is None:
            return None
        return self.config.model_type.model_validate(row.payload)

    def _list(
        self,
        *,
        scope_id: str | None,
        scoped: bool,
        app_id: str | None,
        status: str | None,
        name: str | None,
    ) -> list[TModel]:
        statement: Select[tuple[TableRow]] = select(self.config.table)
        app_column = _table_column(self.config.table, "app_id")
        status_column = _table_column(self.config.table, "status")
        name_column = _table_column(self.config.table, "name")
        created_at_column = _table_column(self.config.table, "created_at")
        id_column = _table_column(self.config.table, "id")
        if scoped:
            statement = statement.where(self._scope_column_element() == scope_id)
        if app_id is not None and app_column is not None:
            statement = statement.where(app_column == app_id)
        if status is not None and status_column is not None:
            statement = statement.where(status_column == status)
        if name is not None and name_column is not None:
            statement = statement.where(name_column == name)
        if created_at_column is not None and id_column is not None:
            statement = statement.order_by(
                created_at_column.desc(),
                id_column.asc(),
            )
        return [
            self.config.model_type.model_validate(row.payload)
            for row in self.session.scalars(statement)
        ]

    def _delete(self, key: str, *, scope_id: str | None, scoped: bool) -> bool:
        row = self._get_row(key, scope_id=scope_id, scoped=scoped)
        if row is None:
            return False
        self.session.delete(row)
        self.session.flush()
        return True

    def _get_row(
        self,
        key: str | None,
        *,
        scope_id: str | None,
        scoped: bool,
    ) -> TableRow | None:
        if key is None:
            return None
        if self.config.key_field == "id":
            lookup_key = _uuid_lookup_key(key)
            if lookup_key is None:
                return None
            if not scoped:
                return self.session.get(self.config.table, lookup_key)
            id_column = _required_table_column(self.config.table, "id")
            statement = select(self.config.table).where(
                id_column == lookup_key,
                self._scope_column_element() == scope_id,
            )
            return self.session.scalars(statement).first()
        column = _required_table_column(self.config.table, self.config.key_field)
        statement: Select[tuple[TableRow]] = select(self.config.table).where(column == key)
        if scoped:
            statement = statement.where(self._scope_column_element() == scope_id)
        return self.session.scalars(statement).first()

    def _scope_column_element(self) -> ColumnElement[ColumnValue]:
        if self._scope_column is None:
            msg = f"{type(self).__name__} has no ownership scope to filter on"
            raise ValueError(msg)
        return _required_table_column(self.config.table, self._scope_column)

    def _existing_scope_id(self, key: str | None) -> str | None:
        """The owner already recorded for a row, so a cross-scope write still locks it."""
        row = self._get_row(key, scope_id=None, scoped=False)
        if row is None:
            return None
        id_column = _required_table_column(self.config.table, "id")
        value = self.session.scalar(select(self._scope_column_element()).where(id_column == row.id))
        return value if isinstance(value, str) and value else None

    def _new_row(
        self,
        key: str | None,
        payload: Mapping[str, JsonValue],
        created_at: datetime,
    ) -> TableRow:
        row = self.config.table()
        if self.config.key_field == "id" and key is not None:
            row.id = _uuid_key(key)
        row.payload = dict(payload)
        row.created_at = created_at
        return row


@dataclass(slots=True)
class WorkspaceTableRepository[TModel: BaseModel](_TableRecordStore[TModel]):
    """Accessor for tenant-owned tables.

    Every default read, write, and delete path requires an explicit workspace
    scope. Cross-workspace/system access only exists through the explicitly
    named ``*_across_workspaces`` methods, reserved for admin routes and system
    owners acting under their own authority.
    """

    _scope_column: ClassVar[str | None] = "workspace_id"

    def __post_init__(self) -> None:
        if _table_column(self.config.table, "workspace_id") is None:
            msg = (
                f"{self.config.table.__name__} has no workspace_id column; "
                "use UserTableRepository when a user owns it, or "
                "GlobalTableRepository for tables that carry no ownership"
            )
            raise ValueError(msg)

    def create(
        self,
        payload: Mapping[str, PayloadValue],
        *,
        workspace_id: str,
        name: str | None = None,
        status: str | None = None,
    ) -> TModel:
        _lock_active_workspace(self.session, workspace_id)
        return self._create(payload, scope_id=workspace_id, name=name, status=status)

    def create_across_workspaces(
        self,
        payload: Mapping[str, PayloadValue],
        *,
        workspace_id: str | None = None,
        name: str | None = None,
        status: str | None = None,
    ) -> TModel:
        owner_id = workspace_id or _payload_workspace_id(payload)
        if owner_id is not None:
            _lock_active_workspace(self.session, owner_id)
        return self._create(payload, scope_id=workspace_id, name=name, status=status)

    def upsert(
        self,
        model: TModel,
        *,
        workspace_id: str,
        key: str | None = None,
        name: str | None = None,
        status: str | None = None,
    ) -> TModel:
        _lock_active_workspace(self.session, workspace_id)
        return self._upsert(
            model,
            key=key,
            scope_id=workspace_id,
            scoped=True,
            name=name,
            status=status,
        )

    def upsert_across_workspaces(
        self,
        model: TModel,
        *,
        workspace_id: str | None = None,
        key: str | None = None,
        name: str | None = None,
        status: str | None = None,
    ) -> TModel:
        payload = _json_object(model)
        owner_id = workspace_id or _payload_workspace_id(payload)
        if owner_id is None:
            lookup_key = key or _payload_key(payload, self.config.key_field)
            owner_id = self._existing_scope_id(lookup_key)
        if owner_id is not None:
            _lock_active_workspace(self.session, owner_id)
        return self._upsert(
            model,
            key=key,
            scope_id=workspace_id,
            scoped=False,
            name=name,
            status=status,
        )

    def get(self, key: str, *, workspace_id: str) -> TModel | None:
        return self._get(key, scope_id=workspace_id, scoped=True)

    def get_across_workspaces(self, key: str) -> TModel | None:
        return self._get(key, scope_id=None, scoped=False)

    def list(
        self,
        *,
        workspace_id: str,
        app_id: str | None = None,
        status: str | None = None,
        name: str | None = None,
    ) -> list[TModel]:
        return self._list(
            scope_id=workspace_id,
            scoped=True,
            app_id=app_id,
            status=status,
            name=name,
        )

    def list_across_workspaces(
        self,
        *,
        app_id: str | None = None,
        status: str | None = None,
        name: str | None = None,
    ) -> list[TModel]:
        return self._list(
            scope_id=None,
            scoped=False,
            app_id=app_id,
            status=status,
            name=name,
        )

    def delete(self, key: str, *, workspace_id: str) -> bool:
        _lock_active_workspace(self.session, workspace_id)
        return self._delete(key, scope_id=workspace_id, scoped=True)

    def delete_across_workspaces(self, key: str) -> bool:
        owner_id = self._existing_scope_id(key)
        if owner_id is not None:
            _lock_active_workspace(self.session, owner_id)
        return self._delete(key, scope_id=None, scoped=False)


@dataclass(slots=True)
class UserTableRepository[TModel: BaseModel](_TableRecordStore[TModel]):
    """Accessor for account-owned tables.

    The same guarantee ``WorkspaceTableRepository`` gives, one level up: every default
    read, write, and delete path requires an explicit user scope, and reaching across
    accounts only happens through the explicitly named ``*_across_users`` methods.
    Resources that belong to a person rather than a workspace—their connected compute
    account, their domains—live here so moving them up did not cost the scoping the
    workspace tables have.
    """

    _scope_column: ClassVar[str | None] = "user_id"

    def __post_init__(self) -> None:
        if _table_column(self.config.table, "user_id") is None:
            msg = (
                f"{self.config.table.__name__} has no user_id column; "
                "use WorkspaceTableRepository when a workspace owns it, or "
                "GlobalTableRepository for tables that carry no ownership"
            )
            raise ValueError(msg)

    def create(
        self,
        payload: Mapping[str, PayloadValue],
        *,
        user_id: str,
        name: str | None = None,
        status: str | None = None,
    ) -> TModel:
        _lock_active_user(self.session, user_id)
        return self._create(payload, scope_id=user_id, name=name, status=status)

    def upsert(
        self,
        model: TModel,
        *,
        user_id: str,
        key: str | None = None,
        name: str | None = None,
        status: str | None = None,
    ) -> TModel:
        _lock_active_user(self.session, user_id)
        return self._upsert(
            model,
            key=key,
            scope_id=user_id,
            scoped=True,
            name=name,
            status=status,
        )

    def get(self, key: str, *, user_id: str) -> TModel | None:
        return self._get(key, scope_id=user_id, scoped=True)

    def get_across_users(self, key: str) -> TModel | None:
        return self._get(key, scope_id=None, scoped=False)

    def list(
        self,
        *,
        user_id: str,
        app_id: str | None = None,
        status: str | None = None,
        name: str | None = None,
    ) -> list[TModel]:
        return self._list(
            scope_id=user_id,
            scoped=True,
            app_id=app_id,
            status=status,
            name=name,
        )

    def list_across_users(
        self,
        *,
        app_id: str | None = None,
        status: str | None = None,
        name: str | None = None,
    ) -> list[TModel]:
        return self._list(
            scope_id=None,
            scoped=False,
            app_id=app_id,
            status=status,
            name=name,
        )

    def delete(self, key: str, *, user_id: str) -> bool:
        _lock_active_user(self.session, user_id)
        return self._delete(key, scope_id=user_id, scoped=True)


@dataclass(slots=True)
class GlobalTableRepository[TModel: BaseModel](_TableRecordStore[TModel]):
    """Accessor for genuinely global tables that carry no tenant ownership."""

    def __post_init__(self) -> None:
        for column_name, owner in (
            ("workspace_id", "WorkspaceTableRepository"),
            ("user_id", "UserTableRepository"),
        ):
            if _table_column(self.config.table, column_name) is not None:
                msg = (
                    f"{self.config.table.__name__} is owned; "
                    f"use {owner} so access requires an owner scope"
                )
                raise ValueError(msg)

    def create(
        self,
        payload: Mapping[str, PayloadValue],
        *,
        name: str | None = None,
        status: str | None = None,
    ) -> TModel:
        return self._create(payload, scope_id=None, name=name, status=status)

    def upsert(
        self,
        model: TModel,
        *,
        key: str | None = None,
        name: str | None = None,
        status: str | None = None,
    ) -> TModel:
        return self._upsert(
            model,
            key=key,
            scope_id=None,
            scoped=False,
            name=name,
            status=status,
        )

    def get(self, key: str) -> TModel | None:
        return self._get(key, scope_id=None, scoped=False)

    def list(
        self,
        *,
        app_id: str | None = None,
        status: str | None = None,
        name: str | None = None,
    ) -> list[TModel]:
        return self._list(
            scope_id=None,
            scoped=False,
            app_id=app_id,
            status=status,
            name=name,
        )

    def delete(self, key: str) -> bool:
        return self._delete(key, scope_id=None, scoped=False)


def _json_object(value: Mapping[str, PayloadValue] | BaseModel) -> JsonObject:
    if isinstance(value, BaseModel):
        return _JSON_OBJECT_ADAPTER.validate_json(value.model_dump_json())
    return _JSON_OBJECT_ADAPTER.validate_python(
        {key: _json_payload_value(item) for key, item in value.items()}
    )


def _json_payload_value(value: PayloadValue) -> JsonValue:
    if isinstance(value, BaseModel):
        return _JSON_VALUE_ADAPTER.validate_json(value.model_dump_json())
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {key: _json_payload_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str):
        return [_json_payload_value(item) for item in value]
    return value


def _payload_key(payload: Mapping[str, JsonValue], field: str) -> str | None:
    value = payload.get(field)
    if isinstance(value, str) and value:
        return value
    return None


def _payload_workspace_id(payload: Mapping[str, PayloadValue] | BaseModel) -> str | None:
    return _payload_key(_json_object(payload), "workspace_id")


def _copy_generated_columns(
    row: TableRow,
    payload: JsonObject,
    model_fields: Collection[str],
) -> None:
    for column_name, _column in _table_columns(type(row)).items():
        if column_name == "payload" or column_name not in model_fields:
            continue
        value = _COLUMN_VALUE_ADAPTER.validate_python(getattr(row, column_name, None))
        if value is not None:
            payload.setdefault(
                column_name,
                value.isoformat() if isinstance(value, datetime) else value,
            )


def _uuid_key(value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError as exc:
        msg = f"database record id must be a UUID, got {value!r}"
        raise ValueError(msg) from exc


def _uuid_lookup_key(value: str) -> str | None:
    try:
        return str(UUID(value))
    except ValueError:
        return None


def apply_common_columns(
    row: TableRow,
    payload: Mapping[str, JsonValue],
    *,
    scope_column: str | None = None,
    scope_id: str | None = None,
    name: str | None = None,
    status: str | None = None,
) -> None:
    table_columns = _table_columns(type(row))
    for column_name, column in table_columns.items():
        if column_name in {"id", "payload", "created_at", "updated_at"}:
            continue
        if column_name in payload:
            setattr(row, column_name, _coerce_column_value(column, payload[column_name]))
    for column_name, value in _column_values(
        payload,
        scope_column=scope_column,
        scope_id=scope_id,
        name=name,
        status=status,
    ):
        if hasattr(row, column_name):
            column = table_columns.get(column_name)
            setattr(
                row,
                column_name,
                _coerce_column_value(column, value) if column is not None else value,
            )


def _column_values(
    payload: Mapping[str, JsonValue],
    *,
    scope_column: str | None,
    scope_id: str | None,
    name: str | None,
    status: str | None,
) -> list[tuple[str, ColumnValue]]:
    values: list[tuple[str, ColumnValue]] = []
    for column_name in (
        "workspace_id",
        "user_id",
        "app_id",
        "stub_id",
        "deployment_id",
        "container_id",
        "parent_task_id",
        "root_task_id",
        "machine_id",
        "worker_id",
        "agent_id",
        "resource_type",
        "resource_id",
        "task_id",
        "queue",
        "stream",
        "message",
        "action",
        "level",
        "kind",
        "type",
        "kind",
        "policy",
        "image_id",
        "cache_key",
        "checkpoint_id",
        "event_type",
        "provider",
        "pool",
        "pool",
        "cron_job",
        "source",
        "target",
        "format",
        "path",
        "visibility",
        "hostname",
        "upstream_task_id",
        "edge_type",
    ):
        raw = payload.get(column_name)
        if raw is not None:
            target_column = "type" if column_name == "kind" else column_name
            values.append((target_column, raw))
    if scope_column is not None and scope_id is not None:
        values.append((scope_column, scope_id))
    if name is not None:
        values.append(("name", name))
    elif isinstance(payload.get("name"), str):
        values.append(("name", payload["name"]))
    if status is not None:
        values.append(("status", status))
    elif payload.get("status") is not None:
        values.append(("status", payload["status"]))
    for column_name in (
        "active",
        "enabled",
        "public",
        "enqueued",
        "feasible",
    ):
        if column_name in payload:
            values.append((column_name, payload[column_name]))
    for column_name in (
        "version",
        "attempts",
        "exit_code",
        "pid",
        "gpu_limit",
        "cpu_millicore_limit",
        "size",
        "hits",
        "clip_version",
        "cache_size_bytes",
        "quantity",
        "priority",
        "min_machines",
        "max_machines",
        "initial_machines",
        "gpu_count",
        "cpu_millicores",
        "memory_mb",
        "hourly_cost_micros",
        "committed_micros",
        "max_spend_micros",
        "ttl_seconds",
        "amount_micros",
        "count",
        "cost_micros",
    ):
        if column_name in payload:
            values.append((column_name, payload[column_name]))
    return values


def _coerce_column_value(
    column: KeyedColumnElement[ColumnValue] | None,
    value: ColumnValue,
) -> ColumnValue:
    normalized = value
    if column is not None and isinstance(column.type, DateTime) and isinstance(normalized, str):
        return datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    if normalized == "" and _is_uuid_column(column):
        return None
    if isinstance(normalized, str) and _is_uuid_column(column):
        return _uuid_lookup_key(normalized)
    return normalized


def _is_uuid_column(column: KeyedColumnElement[ColumnValue] | None) -> bool:
    if column is None:
        return False
    column_type = column.type
    if isinstance(column_type, Uuid):
        return True
    return str(column_type).upper() in {"UUID", "CHAR(32)"}


def _table_columns(
    table: TableRowType,
) -> ReadOnlyColumnCollection[str, KeyedColumnElement[ColumnValue]]:
    return class_mapper(table).local_table.columns


def _table_column(table: TableRowType, name: str) -> KeyedColumnElement[ColumnValue] | None:
    return _table_columns(table).get(name)


def _required_table_column(table: TableRowType, name: str) -> ColumnElement[ColumnValue]:
    column = _table_column(table, name)
    if column is None:
        msg = f"{table.__name__} has no mapped column {name!r}"
        raise ValueError(msg)
    return column
