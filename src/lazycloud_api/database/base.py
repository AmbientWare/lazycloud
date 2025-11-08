import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated, AsyncGenerator, Generic, Type, TypeVar

from pydantic import BaseModel as PydanticBaseModel
from pydantic.functional_validators import BeforeValidator
from sqlalchemy import DateTime, delete, func, orm, update
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import Mapped, mapped_column

from lazycloud_api.database.session import session_manager

Base = orm.declarative_base()


# Custom type that automatically converts UUID to string
def _uuid_to_str(v):
    if isinstance(v, uuid.UUID):
        return str(v)
    return v


UUIDStr = Annotated[str, BeforeValidator(_uuid_to_str)]


class BaseTable(Base):
    __abstract__ = True
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def to_pydantic(
        self, pydantic_class: Type["basePydanticType"]
    ) -> "basePydanticType":
        """Convert the model to a Pydantic model"""
        return pydantic_class.model_validate(self, from_attributes=True)


class BaseDbPydanticModel(PydanticBaseModel):
    """Base class for all Pydantic models with an id"""

    id: UUIDStr | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


basePydanticType = TypeVar("basePydanticType", bound=BaseDbPydanticModel)
baseDbType = TypeVar("baseDbType", bound=BaseTable)


class DatabaseService(Generic[baseDbType, basePydanticType]):
    """Base service class for SQLAlchemy operations"""

    def __init__(
        self,
        db_model_class: Type[baseDbType],
        pydantic_model_class: Type[basePydanticType],
    ):
        self.db_model_class = db_model_class
        self.pydantic_model_class = pydantic_model_class
        self._session_manager = session_manager

    # TODO: expand other methods to use this function as needed
    async def _execute_in_session(self, func, session: AsyncSession | None = None):
        """Execute a function in a session, using provided session or creating new one."""
        if session:
            # Use existing session (part of a transaction)
            return await func(session)
        else:
            # Create new session and commit
            async with self._session_manager.get_session() as new_session:
                result = await func(new_session)
                await new_session.commit()
                return result

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[AsyncSession, None]:
        """Create a database transaction context."""
        async with self._session_manager.get_session() as session:
            async with session.begin():
                yield session

    def _to_pydantic(self, db_model: baseDbType | None) -> basePydanticType | None:
        """Convert SQLAlchemy model to Pydantic model"""
        if db_model is None:
            return None

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

    async def aget_all(self) -> list[basePydanticType]:
        """Get all model instances"""
        async with self._session_manager.get_session() as session:
            query = select(self.db_model_class)
            query = self._apply_default_filters(query)
            result = await session.execute(query)
            db_models = list[baseDbType](result.scalars().all())
            return [self._to_pydantic(db_model) for db_model in db_models]

    async def aget_by_id(
        self,
        id: str,
        include_deleted: bool = False,
        session: AsyncSession | None = None,
    ) -> basePydanticType | None:
        """Get a model instance by id"""

        async def _get(sess: AsyncSession):
            query = select(self.db_model_class).where(self.db_model_class.id == id)
            if not include_deleted:
                query = self._apply_default_filters(query)
            result = await sess.execute(query)
            db_model = result.scalar_one_or_none()
            return self._to_pydantic(db_model)

        if session:
            return await _get(session)
        else:
            return await self._execute_in_session(_get, None)

    async def aget_by_user_id(self, user_id: str) -> list[basePydanticType]:
        """Get all model instances by user id"""
        async with self._session_manager.get_session() as session:
            query = select(self.db_model_class).where(
                self.db_model_class.user_id == user_id
            )
            query = self._apply_default_filters(query)
            result = await session.execute(query)
            db_models = list(result.scalars().all())
            return [model.to_pydantic(self.pydantic_model_class) for model in db_models]

    async def acreate(
        self, model: basePydanticType, session: AsyncSession | None = None
    ) -> basePydanticType:
        """Create a new model instance"""

        async def _create(sess: AsyncSession):
            model_data = model.model_dump(exclude={"id", "created_at", "updated_at"})
            db_model = self.db_model_class(**model_data)
            sess.add(db_model)
            await sess.flush()
            await sess.refresh(db_model)
            return db_model.to_pydantic(self.pydantic_model_class)

        return await self._execute_in_session(_create, session)

    async def acreate_bulk(
        self, models: list[basePydanticType]
    ) -> list[basePydanticType]:
        """Create multiple model instances"""
        async with self._session_manager.get_session() as session:
            db_models = [
                self.db_model_class(
                    **model.model_dump(exclude={"id", "created_at", "updated_at"})
                )
                for model in models
            ]
            session.add_all(db_models)
            await session.flush()
            await session.commit()

            for db_model in db_models:
                await session.refresh(db_model)

            return [self._to_pydantic(model) for model in db_models]

    async def aupdate(
        self, model: basePydanticType, session: AsyncSession | None = None
    ) -> basePydanticType | None:
        """Update an existing model instance"""

        async def _update(sess: AsyncSession):
            update_data = model.model_dump(exclude={"id", "created_at", "updated_at"})
            update_data["updated_at"] = datetime.now(timezone.utc)

            stmt = (
                update(self.db_model_class)
                .where(self.db_model_class.id == model.id)
                .values(**update_data)
                .returning(self.db_model_class)
            )
            result = await sess.execute(stmt)
            updated_model = result.scalar_one_or_none()
            return self._to_pydantic(updated_model)

        result = await self._execute_in_session(_update, session)
        return result

    async def adelete(self, id: str) -> None:
        """Delete a model instance"""
        async with self._session_manager.get_session() as session:
            stmt = delete(self.db_model_class).where(self.db_model_class.id == id)
            await session.execute(stmt)
            await session.commit()

    async def adelete_bulk(self, ids: list[str]) -> None:
        """Delete multiple model instances"""
        async with self._session_manager.get_session() as session:
            stmt = delete(self.db_model_class).where(self.db_model_class.id.in_(ids))
            await session.execute(stmt)
            await session.commit()

    async def aexists(self, id: str) -> bool:
        """Check if a model instance exists"""
        return await self.aget_by_id(id) is not None

    async def afind(self, filters: dict) -> list[basePydanticType]:
        """Find models matching the filters"""
        async with self._session_manager.get_session() as session:
            query = self._build_filtered_query(filters, include_deleted=False)
            result = await session.execute(query)
            db_models = list(result.scalars().all())
            return [model.to_pydantic(self.pydantic_model_class) for model in db_models]

    async def afind_one(self, filters: dict) -> basePydanticType | None:
        """Find a single model matching the filters"""
        results = await self.afind(filters)
        return results[0] if results else None

    async def afind_paginated(
        self,
        filters: dict,
        offset: int = 0,
        limit: int = 100,
        include_deleted: bool = False,
        include_total: bool = True,
    ) -> tuple[int | None, list[basePydanticType]]:
        """Find models matching filters with offset-based pagination"""

        async with self._session_manager.get_session() as session:
            base_query = self._build_filtered_query(filters, include_deleted)

            # Execute count and data queries in parallel for better performance
            if include_total:
                count_query = select(func.count()).select_from(base_query.subquery())
                paginated_query = base_query.offset(offset).limit(limit)

                total_result, data_result = await asyncio.gather(
                    session.execute(count_query),
                    session.execute(paginated_query),
                )
                total = total_result.scalar() or 0
            else:
                paginated_query = base_query.offset(offset).limit(limit)
                data_result = await session.execute(paginated_query)
                total = None

            db_models = list(data_result.scalars().all())
            return total, [
                model.to_pydantic(self.pydantic_model_class) for model in db_models
            ]
