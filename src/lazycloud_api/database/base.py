import asyncio
from typing import Optional, List, Generic, Type, TypeVar
from datetime import datetime, timezone
import nest_asyncio
from sqlalchemy import Column, String, DateTime, Integer
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.future import select
from pydantic import BaseModel as PydanticBaseModel

from lazycloud_api.database.session import session_manager

nest_asyncio.apply()

Base = declarative_base()


class BaseTable(Base):
    """Base class for all SQLAlchemy models"""

    __abstract__ = True

    id = Column(Integer, primary_key=True, index=True, unique=True, autoincrement=True)
    user_id = Column(String, nullable=False, index=True)
    created_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def to_pydantic(
        self, pydantic_class: Type["basePydanticType"]
    ) -> "basePydanticType":
        """Convert the model to a Pydantic model"""
        return pydantic_class(**self.__dict__)


class BaseModel(PydanticBaseModel):
    """Base class for all Pydantic models"""

    id: Optional[int] = None
    user_id: str
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


basePydanticType = TypeVar("basePydanticType", bound=BaseModel)
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

    def _to_pydantic(
        self, db_model: Optional[baseDbType]
    ) -> Optional[basePydanticType]:
        """Convert SQLAlchemy model to Pydantic model"""
        if db_model is None:
            return None

        return db_model.to_pydantic(self.pydantic_model_class)

    async def aget_by_id(self, id: int) -> Optional[basePydanticType]:
        """Get a model instance by id"""
        async with self._session_manager.get_session() as session:
            query = select(self.db_model_class).where(self.db_model_class.id == id)
            result = await session.execute(query)
            db_model = result.scalar_one_or_none()
            return self._to_pydantic(db_model)

    def get_by_id(self, id: int) -> Optional[basePydanticType]:
        """Get a model instance by id"""
        return asyncio.run(self.aget_by_id(id))

    async def aget_by_user_id(self, user_id: str) -> List[basePydanticType]:
        """Get all model instances by user id"""
        async with self._session_manager.get_session() as session:
            query = select(self.db_model_class).where(
                self.db_model_class.user_id == user_id
            )
            result = await session.execute(query)
            db_models = list(result.scalars().all())
            return [model.to_pydantic(self.pydantic_model_class) for model in db_models]

    def get_by_user_id(self, user_id: str) -> List[basePydanticType]:
        """Get all model instances by user id"""
        return asyncio.run(self.aget_by_user_id(user_id))

    async def acreate(self, model: basePydanticType) -> basePydanticType:
        """Create a new model instance"""
        async with self._session_manager.get_session() as session:
            # Exclude id, created_at, and updated_at since they're handled by the database
            model_data = model.model_dump(exclude={"id", "created_at", "updated_at"})
            db_model = self.db_model_class(**model_data)
            session.add(db_model)
            await session.flush()  # Flush to get the generated ID
            await session.commit()
            await session.refresh(db_model)
            return db_model.to_pydantic(self.pydantic_model_class)

    def create(self, model: basePydanticType) -> basePydanticType:
        """Create a new model instance"""
        return asyncio.run(self.acreate(model))

    async def aupdate(self, model: basePydanticType) -> Optional[basePydanticType]:
        """Update an existing model instance"""
        async with self._session_manager.get_session() as session:
            query = select(self.db_model_class).where(
                self.db_model_class.id == model.id
            )
            result = await session.execute(query)
            db_model = result.scalar_one_or_none()
            if db_model:
                for key, value in model.model_dump().items():
                    setattr(db_model, key, value)
                await session.commit()
                await session.refresh(db_model)
                return db_model.to_pydantic(self.pydantic_model_class)
            return None

    def update(self, model: basePydanticType) -> Optional[basePydanticType]:
        """Update an existing model instance"""
        return asyncio.run(self.aupdate(model))

    async def adelete(self, id: int) -> None:
        """Delete a model instance"""
        async with self._session_manager.get_session() as session:
            query = select(self.db_model_class).where(self.db_model_class.id == id)
            result = await session.execute(query)
            db_model = result.scalar_one_or_none()
            if db_model:
                await session.delete(db_model)
                await session.commit()

    def delete(self, id: int) -> None:
        """Delete a model instance"""
        return asyncio.run(self.adelete(id))

    async def aexists(self, id: int) -> bool:
        """Check if a model instance exists"""
        return await self.aget_by_id(id) is not None

    def exists(self, id: int) -> bool:
        """Check if a model instance exists"""
        return asyncio.run(self.aexists(id))

    async def afind(self, filters: dict) -> List[basePydanticType]:
        """Find models matching the filters"""
        async with self._session_manager.get_session() as session:
            query = select(self.db_model_class)
            for key, value in filters.items():
                if hasattr(self.db_model_class, key):
                    if key.endswith("__gte"):
                        field = key[:-5]
                        query = query.where(
                            getattr(self.db_model_class, field) >= value
                        )
                    elif key.endswith("__lte"):
                        field = key[:-5]
                        query = query.where(
                            getattr(self.db_model_class, field) <= value
                        )
                    else:
                        query = query.where(getattr(self.db_model_class, key) == value)

            result = await session.execute(query)
            db_models = list(result.scalars().all())
            return [model.to_pydantic(self.pydantic_model_class) for model in db_models]

    def find(self, filters: dict) -> List[basePydanticType]:
        """Find models matching the filters"""
        return asyncio.run(self.afind(filters))

    async def afind_one(self, filters: dict) -> Optional[basePydanticType]:
        """Find a single model matching the filters"""
        results = await self.afind(filters)
        return results[0] if results else None

    def find_one(self, filters: dict) -> Optional[basePydanticType]:
        """Find a single model matching the filters"""
        return asyncio.run(self.afind_one(filters))
