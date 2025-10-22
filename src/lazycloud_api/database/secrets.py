import json
from typing import TYPE_CHECKING

from cryptography.fernet import Fernet
from sqlalchemy import ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.future import select
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lazycloud_api.config import app_config
from lazycloud_api.database.base import (
    BaseDbPydanticModel,
    BaseTable,
    DatabaseService,
    UUIDStr,
)

if TYPE_CHECKING:
    from lazycloud_api.database.compose import ComposeDeploymentTable

FERNET = Fernet(app_config.DB_SECRET_KEY.encode())


def _encrypt_secrets(secrets_dict: dict[str, str]) -> str:
    """Encrypt a secrets dictionary for storage."""
    json_str = json.dumps(secrets_dict)
    encrypted_bytes = FERNET.encrypt(json_str.encode("utf-8"))
    return encrypted_bytes.decode("utf-8")


def _decrypt_secrets(encrypted_str: str) -> dict[str, str]:
    """Decrypt stored secrets back to dictionary."""
    decrypted_bytes = FERNET.decrypt(encrypted_str.encode("utf-8"))
    return json.loads(decrypted_bytes.decode("utf-8"))


class SecretTable(BaseTable):
    """SQLAlchemy model for deployment secrets"""

    __tablename__ = "secrets"

    deployment_id: Mapped[UUID] = mapped_column(
        ForeignKey("compose_deployments.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    # Encrypted JSON string
    secrets: Mapped[str] = mapped_column(Text)

    # relationships
    deployment: Mapped["ComposeDeploymentTable"] = relationship(
        "ComposeDeploymentTable",
        back_populates="secrets",
        lazy="joined",
    )


class SecretEncryptedPydantic(BaseDbPydanticModel):
    """Pydantic model for encrypted deployment secrets (database storage)"""

    deployment_id: UUIDStr
    secrets: str  # Encrypted JSON string


class SecretPydantic(BaseDbPydanticModel):
    """Pydantic model for deployment secrets"""

    deployment_id: UUIDStr
    secrets: dict[str, str]


class SecretService(DatabaseService[SecretTable, SecretEncryptedPydantic]):
    """Service layer for secret operations"""

    def __init__(self):
        super().__init__(SecretTable, SecretEncryptedPydantic)

    async def aget_secret(self, deployment_id: str) -> SecretPydantic | None:
        """Get a secret by deployment id"""
        async with self._session_manager.get_session() as session:
            query = select(SecretTable).where(
                SecretTable.deployment_id == deployment_id
            )
            result = await session.execute(query)
            db_secret = result.scalar_one_or_none()
            if db_secret:
                # Decrypt the secrets
                decrypted_secrets = _decrypt_secrets(db_secret.secrets)
                return SecretPydantic(
                    id=str(db_secret.id),
                    deployment_id=str(db_secret.deployment_id),
                    secrets=decrypted_secrets,
                    created_at=db_secret.created_at,
                    updated_at=db_secret.updated_at,
                )

            return None

    async def aupdate_or_create(self, db_secrets: SecretPydantic) -> SecretPydantic:
        """Update existing secrets or create new ones"""
        existing = await self.aget_secret(db_secrets.deployment_id)

        # Create encrypted version for database storage
        encrypted_model = SecretEncryptedPydantic(
            deployment_id=db_secrets.deployment_id,
            secrets=_encrypt_secrets(db_secrets.secrets),
        )

        if existing:
            # Find the existing record by deployment_id to get its id
            async with self._session_manager.get_session() as session:
                query = select(SecretTable).where(
                    SecretTable.deployment_id == db_secrets.deployment_id
                )
                result = await session.execute(query)
                db_model = result.scalar_one()
                encrypted_model.id = str(db_model.id)
            await self.aupdate(encrypted_model)
        else:
            await self.acreate(encrypted_model)

        # Return the properly decrypted version
        return await self.aget_secret(db_secrets.deployment_id)
