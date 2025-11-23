from typing import TYPE_CHECKING, Any

from cryptography.fernet import InvalidToken
from pydantic import field_serializer, field_validator
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
from lazycloud_api.database.utils import decrypt_string, encrypt_string
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


class SecretPydantic(BaseDbPydanticModel):
    """Pydantic model for deployment secrets with automatic encryption/decryption."""

    deployment_id: UUIDStr
    key: str
    value: str
    source: SecretSource
    state: SecretState

    @field_validator("value", mode="before")
    @classmethod
    def decrypt_value(cls, value: Any) -> str:
        """Automatically decrypt value when loading from database."""
        if isinstance(value, str):
            try:
                return decrypt_string(value)
            except InvalidToken:
                # Not encrypted (migration or new secret creation)
                return value
            except Exception as e:
                # Real decryption error
                raise ValueError(f"Failed to decrypt secret value: {e}") from e
        return value

    @field_serializer("value", when_used="always")
    def serialize_value(self, value: str) -> str:
        """Automatically encrypt value when dumping for database storage."""
        # Always encrypt when serializing for storage
        try:
            return encrypt_string(value)
        except Exception as e:
            raise ValueError(f"Failed to encrypt secret value: {e}") from e


class SecretService(DatabaseService[SecretTable, SecretPydantic]):
    """Service layer for secret operations.

    Encryption/decryption is handled automatically by Pydantic validators/serializers.
    """

    def __init__(self):
        super().__init__(SecretTable, SecretPydantic)

    async def get_secrets(
        self, deployment_id: str, source: SecretSource | None = None
    ) -> list[SecretPydantic]:
        """Get secrets by deployment id."""
        async with self._session_manager.get_session() as session:
            query = select(SecretTable).where(
                SecretTable.deployment_id == deployment_id
            )
            if source:
                query = query.where(SecretTable.source == source)

            result = await session.execute(query)
            db_secrets = result.scalars().all()
            return [self._to_pydantic(secret) for secret in db_secrets]

    async def get_secret_by_key(
        self, deployment_id: str, key: str
    ) -> SecretPydantic | None:
        """Get a single secret by deployment_id and key."""
        async with self._session_manager.get_session() as session:
            query = select(SecretTable).where(
                SecretTable.deployment_id == deployment_id,
                SecretTable.key == key,
            )
            result = await session.execute(query)
            db_secret = result.scalar_one_or_none()
            return self._to_pydantic(db_secret) if db_secret else None

    async def update_by_key(
        self,
        deployment_id: str,
        key: str,
        value: str,
        source: SecretSource,
        state: SecretState,
    ) -> SecretPydantic | None:
        """Update an existing secret by deployment_id and key. Returns None if not found."""
        existing = await self.get_secret_by_key(deployment_id, key)
        if not existing:
            return None

        # Create updated model
        updated_secret = SecretPydantic(
            id=existing.id,
            deployment_id=deployment_id,
            key=key,
            value=value,
            source=source,
            state=state,
        )

        return await self.update(updated_secret)

    async def delete_secret_by_key(self, deployment_id: str, key: str) -> bool:
        """Delete a single secret by deployment_id and key"""
        async with self._session_manager.get_session() as session:
            query = select(SecretTable).where(
                SecretTable.deployment_id == deployment_id,
                SecretTable.key == key,
            )
            result = await session.execute(query)
            db_secret = result.scalar_one_or_none()

            if db_secret:
                await self.delete(str(db_secret.id))
                return True
            return False
