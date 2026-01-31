from models.secrets import SecretSource, SecretState
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from backend.database.models import SecretPydantic
from backend.database.services.base import DatabaseService
from backend.database.tables import SecretTable


class SecretService(DatabaseService[SecretTable, SecretPydantic]):
    """Service layer for secret operations.

    Encryption/decryption is handled automatically by Pydantic validators/serializers.
    """

    def __init__(self, session: AsyncSession):
        super().__init__(SecretTable, SecretPydantic, session)

    async def get_secrets(
        self, deployment_id: str, source: SecretSource | None = None
    ) -> list[SecretPydantic]:
        """Get secrets by deployment id."""
        query = select(SecretTable).where(SecretTable.deployment_id == deployment_id)
        if source:
            query = query.where(SecretTable.source == source)

        result = await self._session.execute(query)
        db_secrets = result.scalars().all()
        return [self._to_pydantic(secret) for secret in db_secrets]

    async def get_secret_by_key(
        self, deployment_id: str, key: str
    ) -> SecretPydantic | None:
        """Get a single secret by deployment_id and key."""
        query = select(SecretTable).where(
            SecretTable.deployment_id == deployment_id,
            SecretTable.key == key,
        )
        result = await self._session.execute(query)
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
        query = select(SecretTable).where(
            SecretTable.deployment_id == deployment_id,
            SecretTable.key == key,
        )
        result = await self._session.execute(query)
        db_secret = result.scalar_one_or_none()

        if db_secret:
            await self.delete(str(db_secret.id))
            return True
        return False
