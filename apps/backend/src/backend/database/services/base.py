import asyncio
from datetime import datetime, timezone
from typing import Generic, Type, TypeVar

from sqlalchemy import delete, func, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from backend.database.models import BaseDbModel
from backend.database.tables import BaseTable

baseDbType = TypeVar("baseDbType", bound=BaseTable)
basePydanticType = TypeVar("basePydanticType", bound=BaseDbModel)


class DatabaseService(Generic[baseDbType, basePydanticType]):
    """Base service class for SQLAlchemy operations."""

    def __init__(
        self,
        db_model_class: Type[baseDbType],
        pydantic_model_class: Type[basePydanticType],
        session: AsyncSession,
    ):
        self.db_model_class = db_model_class
        self.pydantic_model_class = pydantic_model_class
        self._session = session

    def _to_pydantic(self, db_model: baseDbType) -> basePydanticType:
        """Convert SQLAlchemy model to Pydantic model"""
        return db_model.to_pydantic(self.pydantic_model_class)

    def _apply_default_filters(self, query):
        """Apply default filters (e.g., soft delete) if the model supports them"""
        if hasattr(self.db_model_class, "deleted_at"):
            query = query.where(self.db_model_class.deleted_at.is_(None))
        return query

    def _build_filtered_query(self, filters: dict, include_deleted: bool = False):
        """Build a base query with filters applied"""
        query = select(self.db_model_class)

        if not include_deleted:
            query = self._apply_default_filters(query)

        for key, value in filters.items():
            if hasattr(self.db_model_class, key):
                if key.endswith("__gte"):
                    field = key[:-5]
                    query = query.where(getattr(self.db_model_class, field) >= value)
                elif key.endswith("__lte"):
                    field = key[:-5]
                    query = query.where(getattr(self.db_model_class, field) <= value)
                else:
                    query = query.where(getattr(self.db_model_class, key) == value)

        return query

    async def get_all(self) -> list[basePydanticType]:
        """Get all model instances"""
        query = select(self.db_model_class)
        query = self._apply_default_filters(query)
        result = await self._session.execute(query)
        db_models = list[baseDbType](result.scalars().all())
        return [self._to_pydantic(db_model) for db_model in db_models]

    async def get_by_id(
        self,
        id: str,
        include_deleted: bool = False,
        with_lock: bool = False,
    ) -> basePydanticType | None:
        """Get a model instance by id"""
        query = select(self.db_model_class).where(self.db_model_class.id == id)
        if not include_deleted:
            query = self._apply_default_filters(query)
        if with_lock:
            query = query.with_for_update()
        result = await self._session.execute(query)
        db_model = result.scalar_one_or_none()

        return self._to_pydantic(db_model) if db_model else None

    async def get_by_user_id(self, user_id: str) -> list[basePydanticType]:
        """Get all model instances by user id"""
        query = select(self.db_model_class).where(
            self.db_model_class.user_id == user_id
        )
        query = self._apply_default_filters(query)
        result = await self._session.execute(query)
        db_models = list[baseDbType](result.scalars().all())
        return [self._to_pydantic(model) for model in db_models]

    async def create(self, model: basePydanticType) -> basePydanticType:
        """Create a new model instance"""
        model_data = model.model_dump(exclude={"id", "created_at", "updated_at"})
        db_model = self.db_model_class(**model_data)
        self._session.add(db_model)
        await self._session.flush()
        await self._session.refresh(db_model)
        return self._to_pydantic(db_model)

    async def create_bulk(
        self, models: list[basePydanticType]
    ) -> list[basePydanticType]:
        """Create multiple model instances"""
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

    async def update(self, model: basePydanticType) -> basePydanticType | None:
        """Update an existing model instance."""
        update_data = model.model_dump(exclude={"id", "created_at", "updated_at"})
        update_data["updated_at"] = datetime.now(timezone.utc)

        stmt = (
            update(self.db_model_class)
            .where(self.db_model_class.id == model.id)
            .values(**update_data)
            .returning(self.db_model_class)
        )
        # populate_existing=True overwrites cached objects with fresh DB values
        result = await self._session.execute(
            stmt, execution_options={"populate_existing": True}
        )
        updated_model = result.scalar_one_or_none()
        return self._to_pydantic(updated_model) if updated_model else None

    async def delete(self, id: str) -> None:
        """Delete a model instance"""
        stmt = delete(self.db_model_class).where(self.db_model_class.id == id)
        await self._session.execute(stmt)

    async def delete_bulk(self, ids: list[str]) -> None:
        """Delete multiple model instances"""
        stmt = delete(self.db_model_class).where(self.db_model_class.id.in_(ids))
        await self._session.execute(stmt)

    async def exists(self, id: str) -> bool:
        """Check if a model instance exists"""
        return await self.get_by_id(id) is not None

    async def find(self, filters: dict) -> list[basePydanticType]:
        """Find models matching the filters"""
        query = self._build_filtered_query(filters, include_deleted=False)
        result = await self._session.execute(query)
        db_models = list(result.scalars().all())
        return [self._to_pydantic(model) for model in db_models]

    async def find_one(self, filters: dict) -> basePydanticType | None:
        """Find a single model matching the filters"""
        query = self._build_filtered_query(filters, include_deleted=False)
        result = await self._session.execute(query)
        db_model = result.scalar_one_or_none()
        return self._to_pydantic(db_model) if db_model else None

    async def find_paginated(
        self,
        filters: dict,
        offset: int = 0,
        limit: int = 100,
        include_deleted: bool = False,
        include_total: bool = True,
    ) -> tuple[int | None, list[basePydanticType]]:
        """Find models matching filters with offset-based pagination"""
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

        db_models = list(data_result.scalars().all())
        return total, [self._to_pydantic(model) for model in db_models]
