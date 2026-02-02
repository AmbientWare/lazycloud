import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import UUID, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database.tables.base import (
    BaseTable,
)

if TYPE_CHECKING:
    from backend.database.tables.users import UserTable


class ApiKeyTable(BaseTable):
    """SQLAlchemy model for a api key"""

    __tablename__ = "api_keys"

    name: Mapped[str] = mapped_column(String, index=True)
    value: Mapped[str] = mapped_column(String, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    # Relationships
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    user: Mapped["UserTable"] = relationship(
        "UserTable",
        back_populates="api_keys",
        lazy="joined",
    )

    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_user_api_key_name"),
        Index("ix_api_keys_user_id_name", "user_id", "name"),
    )
