import asyncio
from datetime import datetime, timezone
from typing import Any, Generic, TypeVar

from pydantic import BaseModel
from sqlalchemy import Select, delete, func, update
from sqlalchemy import exists as sql_exists
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from backend.database.models import BaseDbModel
from backend.database.tables import BaseTable

TableT = TypeVar("TableT", bound=BaseTable)
ModelT = TypeVar("ModelT", bound=BaseDbModel)


class DatabaseService(Generic[TableT, ModelT]):
    """Base service class for SQLAlchemy operations."""

    def __init__(
        self,
        db_model_class: type[TableT],
        pydantic_model_class: type[ModelT],
        session: AsyncSession,
    ):
        self.db_model_class = db_model_class
        self.pydantic_model_class = pydantic_model_class
        self._session = session

    def _to_pydantic(self, db_model: TableT) -> ModelT:
        """Convert SQLAlchemy model to Pydantic model."""
        return db_model.to_pydantic(self.pydantic_model_class)

    def _apply_default_filters(
        self, query: Select[tuple[TableT]]
    ) -> Select[tuple[TableT]]:
        """Apply default filters (e.g., soft delete) if the model supports them."""
        if hasattr(self.db_model_class, "deleted_at"):
            query = query.where(self.db_model_class.deleted_at.is_(None))
        return query

    def _build_filtered_query(
        self, filters: dict[str, Any], include_deleted: bool = False
    ) -> Select[tuple[TableT]]:
        """Build a base query with filters applied."""
        query = select(self.db_model_class)

        if not include_deleted:
            query = self._apply_default_filters(query)

        for key, value in filters.items():
            if key.endswith("__gte"):
                field = key[:-5]
                if hasattr(self.db_model_class, field):
                    query = query.where(getattr(self.db_model_class, field) >= value)
            elif key.endswith("__lte"):
                field = key[:-5]
                if hasattr(self.db_model_class, field):
                    query = query.where(getattr(self.db_model_class, field) <= value)
            elif hasattr(self.db_model_class, key):
                query = query.where(getattr(self.db_model_class, key) == value)

        return query

    async def get_all(self) -> list[ModelT]:
        """Get all model instances."""
        query = select(self.db_model_class)
        query = self._apply_default_filters(query)
        result = await self._session.execute(query)
        return [self._to_pydantic(db_model) for db_model in result.scalars().all()]

    async def get_by_id(
        self,
        id: str,
        include_deleted: bool = False,
        with_lock: bool = False,
    ) -> ModelT | None:
        """Get a model instance by id."""
        query = select(self.db_model_class).where(self.db_model_class.id == id)
        if not include_deleted:
            query = self._apply_default_filters(query)
        if with_lock:
            query = query.with_for_update()
        result = await self._session.execute(query)
        db_model = result.scalar_one_or_none()
        return self._to_pydantic(db_model) if db_model else None

    async def get_by_user_id(self, user_id: str) -> list[ModelT]:
        """Get all model instances by user id."""
        query = select(self.db_model_class).where(
            self.db_model_class.user_id == user_id
        )
        query = self._apply_default_filters(query)
        result = await self._session.execute(query)
        return [self._to_pydantic(model) for model in result.scalars().all()]

    async def create(self, model: BaseModel) -> ModelT:
        """Create a new model instance."""
        model_data = model.model_dump(exclude={"id", "created_at", "updated_at"})
        db_model = self.db_model_class(**model_data)
        self._session.add(db_model)
        await self._session.flush()
        await self._session.refresh(db_model)
        return self._to_pydantic(db_model)

    async def create_bulk(self, models: list[BaseModel]) -> list[ModelT]:
        """Create multiple model instances."""
        db_models = [
            self.db_model_class(
                **model.model_dump(exclude={"id", "created_at", "updated_at"})
            )
            for model in models
        ]
        self._session.add_all(db_models)
        await self._session.flush()

        for db_model in db_models:
            await self._session.refresh(db_model)

        return [self._to_pydantic(model) for model in db_models]

    async def update(self, model: BaseDbModel) -> ModelT | None:
        """Update an existing model instance."""
        update_data = model.model_dump(exclude={"id", "created_at", "updated_at"})
        update_data["updated_at"] = datetime.now(timezone.utc)

        stmt = (
            update(self.db_model_class)
            .where(self.db_model_class.id == model.id)
            .values(**update_data)
            .returning(self.db_model_class)
        )
        result = await self._session.execute(
            stmt, execution_options={"populate_existing": True}
        )
        updated_model = result.scalar_one_or_none()
        return self._to_pydantic(updated_model) if updated_model else None

    async def delete(self, id: str) -> bool:
        """Delete a model instance. Returns True if deleted, False if not found."""
        stmt = (
            delete(self.db_model_class)
            .where(self.db_model_class.id == id)
            .returning(self.db_model_class.id)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def delete_bulk(self, ids: list[str]) -> int:
        """Delete multiple model instances. Returns count of deleted rows."""
        stmt = (
            delete(self.db_model_class)
            .where(self.db_model_class.id.in_(ids))
            .returning(self.db_model_class.id)
        )
        result = await self._session.execute(stmt)
        return len(result.scalars().all())

    async def exists(self, id: str) -> bool:
        """Check if a model instance exists."""
        query = select(sql_exists().where(self.db_model_class.id == id))
        if hasattr(self.db_model_class, "deleted_at"):
            query = select(
                sql_exists()
                .where(self.db_model_class.id == id)
                .where(self.db_model_class.deleted_at.is_(None))
            )
        result = await self._session.execute(query)
        return result.scalar() or False

    async def find(self, filters: dict[str, Any]) -> list[ModelT]:
        """Find models matching the filters."""
        query = self._build_filtered_query(filters, include_deleted=False)
        result = await self._session.execute(query)
        return [self._to_pydantic(model) for model in result.scalars().all()]

    async def find_one(self, filters: dict[str, Any]) -> ModelT | None:
        """Find a single model matching the filters."""
        query = self._build_filtered_query(filters, include_deleted=False)
        result = await self._session.execute(query)
        db_model = result.scalar_one_or_none()
        return self._to_pydantic(db_model) if db_model else None

    async def find_paginated(
        self,
        filters: dict[str, Any],
        offset: int = 0,
        limit: int = 100,
        include_deleted: bool = False,
        include_total: bool = True,
    ) -> tuple[int | None, list[ModelT]]:
        """Find models matching filters with offset-based pagination."""
        base_query = self._build_filtered_query(filters, include_deleted)

        if include_total:
            count_query = select(func.count()).select_from(base_query.subquery())
            paginated_query = base_query.offset(offset).limit(limit)

            total_result, data_result = await asyncio.gather(
                self._session.execute(count_query),
                self._session.execute(paginated_query),
            )
            total = total_result.scalar() or 0
        else:
            paginated_query = base_query.offset(offset).limit(limit)
            data_result = await self._session.execute(paginated_query)
            total = None

        return total, [
            self._to_pydantic(model) for model in data_result.scalars().all()
        ]
