from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer
from sqlalchemy.orm import Mapped, mapped_column

from database.tables.base import DatabaseBase, uuid_type


class ContainerRolloutDrainTable(DatabaseBase):
    __tablename__ = "container_rollout_drains"
    __table_args__ = (
        CheckConstraint("serving_floor >= 0", name="ck_container_rollout_drains_serving_floor"),
        Index("ix_container_rollout_drains_stub", "stub_id"),
    )

    container_id: Mapped[str] = mapped_column(
        uuid_type, ForeignKey("containers.id", ondelete="CASCADE"), primary_key=True
    )
    stub_id: Mapped[str] = mapped_column(
        uuid_type, ForeignKey("stubs.id", ondelete="CASCADE"), nullable=False
    )
    serving_floor: Mapped[int] = mapped_column(Integer, nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    admission_closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
