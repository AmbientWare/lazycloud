from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from database.tables.base import DatabaseBase, uuid_type


class ProviderNodeLaunchTable(DatabaseBase):
    __tablename__ = "provider_node_launches"
    __table_args__ = (
        Index(
            "uq_provider_node_launches_active_slot",
            "unit_id",
            "server_name",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
            sqlite_where=text("revoked_at IS NULL"),
        ),
        Index("uq_provider_node_launches_token_hash", "bootstrap_token_hash", unique=True),
        Index("uq_provider_node_launches_node_hash", "node_token_hash", unique=True),
        Index(
            "uq_provider_node_launches_active_instance",
            "provider_ref",
            "provider_instance_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL AND provider_instance_id IS NOT NULL"),
            sqlite_where=text("revoked_at IS NULL AND provider_instance_id IS NOT NULL"),
        ),
        CheckConstraint("expires_at > created_at", name="ck_provider_node_launches_expiry"),
        CheckConstraint("generation > 0", name="ck_provider_node_launches_generation"),
        CheckConstraint(
            "(redeemed_at IS NULL AND node_token_hash IS NULL) OR "
            "(redeemed_at IS NOT NULL AND node_token_hash IS NOT NULL "
            "AND bootstrap_token_ciphertext IS NULL)",
            name="ck_provider_node_launches_redemption",
        ),
    )

    id: Mapped[str] = mapped_column(uuid_type, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        uuid_type, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    unit_id: Mapped[str] = mapped_column(
        uuid_type, ForeignKey("compute_units.id", ondelete="CASCADE"), nullable=False
    )
    provider_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    region: Mapped[str] = mapped_column(String(64), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    server_name: Mapped[str] = mapped_column(String(255), nullable=False)
    provider_instance_id: Mapped[str | None] = mapped_column(String(128))
    bootstrap_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    bootstrap_token_ciphertext: Mapped[str | None] = mapped_column(Text)
    node_token_hash: Mapped[str | None] = mapped_column(String(64))
    fingerprint_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    redeemed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    enrolled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
