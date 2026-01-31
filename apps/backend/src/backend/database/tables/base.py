import uuid
from datetime import datetime, timezone
from typing import Type, TypeVar

from sqlalchemy import DateTime, orm
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.database.models import BaseDbPydanticModel

Base = orm.declarative_base()

_PydanticT = TypeVar("_PydanticT", bound=BaseDbPydanticModel)


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

    def to_pydantic(self, pydantic_class: Type[_PydanticT]) -> _PydanticT:
        """Convert the model to a Pydantic model"""
        return pydantic_class.model_validate(self, from_attributes=True)
