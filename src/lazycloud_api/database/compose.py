from datetime import datetime
from typing import TYPE_CHECKING, List

from sqlalchemy import (
    JSON,
    DateTime,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    Enum as SQLAEnum,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lazycloud_api.database.base import BaseModel, BaseTable, DatabaseService
from shared.models.deployments import DeploymentStates
from shared.models.helm import HelmValues

if TYPE_CHECKING:
    from lazycloud_api.database.secrets import SecretTable


class ComposeDeploymentTable(BaseTable):
    """SQLAlchemy model for a compose deployment."""

    __tablename__ = "compose_deployments"

    name: Mapped[str | None] = mapped_column(String, index=True)
    namespace: Mapped[str] = mapped_column(String)
    compose_yaml: Mapped[str] = mapped_column(Text)
    helm_values: Mapped[dict | None] = mapped_column(JSON)
    state: Mapped[DeploymentStates] = mapped_column(
        SQLAEnum(DeploymentStates), default=DeploymentStates.PENDING, index=True
    )
    status_message: Mapped[str | None] = mapped_column(Text)
    deployed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Unique constraint to ensure one deployment per name per user
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_user_deployment_name"),
        Index("ix_compose_deployments_user_id_name", "user_id", "name"),
    )

    # Relationships
    secrets: Mapped[List["SecretTable"]] = relationship(
        "SecretTable",
        back_populates="deployment",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )


class ComposeDeploymentPydantic(BaseModel):
    """Pydantic model for a compose deployment."""

    name: str | None = None
    namespace: str
    compose_yaml: str
    helm_values: HelmValues | None = None
    state: DeploymentStates = DeploymentStates.PENDING
    status_message: str | None = None
    deployed_at: datetime | None = None


class ComposeDeploymentService(
    DatabaseService[ComposeDeploymentTable, ComposeDeploymentPydantic]
):
    """Service layer for compose deployment operations."""

    def __init__(self):
        super().__init__(ComposeDeploymentTable, ComposeDeploymentPydantic)

    async def aget_by_name(
        self, user_id: str, name: str
    ) -> ComposeDeploymentPydantic | None:
        """Get deployment by name."""
        return await self.afind_one({"user_id": user_id, "name": name})

    async def find_by_namespace(
        self, user_id: str, namespace: str
    ) -> ComposeDeploymentPydantic | None:
        """Find deployment by namespace and user."""
        return await self.afind_one({"user_id": user_id, "namespace": namespace})

    async def find_by_status(
        self, user_id: str, state: DeploymentStates
    ) -> list[ComposeDeploymentPydantic]:
        """Find deployments by status."""
        return await self.afind({"user_id": user_id, "state": state})

    async def update_status(
        self,
        deployment_id: str,
        state: DeploymentStates,
        message: str | None = None,
    ) -> ComposeDeploymentPydantic | None:
        """Update deployment status."""
        deployment = await self.aget_by_id(deployment_id)
        if not deployment:
            return None

        deployment.state = state
        if message:
            deployment.status_message = message

        return await self.aupdate(deployment)
