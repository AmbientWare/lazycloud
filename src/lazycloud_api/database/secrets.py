from typing import TYPE_CHECKING

from sqlalchemy import Enum, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.future import select
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lazycloud_api.database.base import (
    BaseDbPydanticModel,
    BaseTable,
    DatabaseService,
    UUIDStr,
)
from shared.models.secrets import SecretSource, SecretState

if TYPE_CHECKING:
    from lazycloud_api.database.compose import ComposeDeploymentTable


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


class SecretEncryptedPydantic(BaseDbPydanticModel):
    """Pydantic model for encrypted deployment secrets (database storage)"""

    deployment_id: UUIDStr
    key: str
    value: str
    source: SecretSource
    state: SecretState


class SecretPydantic(BaseDbPydanticModel):
    """Pydantic model for deployment secrets"""

    deployment_id: UUIDStr
    key: str
    value: str
    source: SecretSource
    state: SecretState


class SecretService(DatabaseService[SecretTable, SecretEncryptedPydantic]):
    """Service layer for secret operations"""

    def __init__(self):
        super().__init__(SecretTable, SecretEncryptedPydantic)

    async def aget_secrets(
        self, deployment_id: str, source: SecretSource | None = None
    ) -> list[SecretPydantic]:
        """Get a secret by deployment id"""
        async with self._session_manager.get_session() as session:
            query = select(SecretTable).where(
                SecretTable.deployment_id == deployment_id
            )
            if source:
                query = query.where(SecretTable.source == source)

            result = await session.execute(query)
            db_secrets = result.scalars().all()
            return [self._to_pydantic(secret) for secret in db_secrets]

    async def aget_secret_by_key(
        self, deployment_id: str, key: str
    ) -> SecretPydantic | None:
        """Get a single secret by deployment_id and key"""
        async with self._session_manager.get_session() as session:
            query = select(SecretTable).where(
                SecretTable.deployment_id == deployment_id,
                SecretTable.key == key,
            )
            result = await session.execute(query)
            db_secret = result.scalar_one_or_none()
            return self._to_pydantic(db_secret) if db_secret else None

    async def aupdate_by_key(
        self,
        deployment_id: str,
        key: str,
        value: str,
        source: SecretSource,
        state: SecretState,
    ) -> SecretPydantic | None:
        """Update an existing secret by deployment_id and key. Returns None if not found."""
        existing = await self.aget_secret_by_key(deployment_id, key)
        if not existing:
            return None

        # Create encrypted version for database storage with the existing ID
        encrypted_model = SecretEncryptedPydantic(
            id=existing.id,
            deployment_id=deployment_id,
            key=key,
            value=value,
            source=source,
            state=state,
        )

        await self.aupdate(encrypted_model)
        return await self.aget_secret_by_key(deployment_id, key)

    async def adelete_secret_by_key(self, deployment_id: str, key: str) -> bool:
        """Delete a single secret by deployment_id and key"""
        async with self._session_manager.get_session() as session:
            query = select(SecretTable).where(
                SecretTable.deployment_id == deployment_id,
                SecretTable.key == key,
            )
            result = await session.execute(query)
            db_secret = result.scalar_one_or_none()

            if db_secret:
                await self.adelete(str(db_secret.id))
                return True
            return False
