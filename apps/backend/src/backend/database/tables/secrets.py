from typing import TYPE_CHECKING

from models.secrets import SecretSource, SecretState
from sqlalchemy import Enum, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database.tables.base import (
    BaseTable,
)

if TYPE_CHECKING:
    from backend.database.tables.compose import ComposeDeploymentTable


class SecretTable(BaseTable):
    """SQLAlchemy model for deployment secrets - each secret is a separate row"""

    __tablename__ = "secrets"

    deployment_id: Mapped[UUID] = mapped_column(
        ForeignKey("compose_deployments.id", ondelete="CASCADE"), index=True
    )

    # Individual secret key-value pair
    key: Mapped[str] = mapped_column(String, index=True)  # Secret name
    value: Mapped[str] = mapped_column(Text)  # Encrypted secret value
    source: Mapped[SecretSource] = mapped_column(Enum(SecretSource))
    state: Mapped[SecretState] = mapped_column(Enum(SecretState))

    # relationships
    deployment: Mapped["ComposeDeploymentTable"] = relationship(
        "ComposeDeploymentTable",
        back_populates="secrets",
        lazy="joined",
    )

    __table_args__ = (
        # Ensure unique combination of deployment_id + key (no duplicate secret names per deployment)
        UniqueConstraint("deployment_id", "key", name="uq_deployment_secret_key"),
    )
