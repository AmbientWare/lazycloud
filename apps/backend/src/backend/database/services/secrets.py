from datetime import datetime, timezone

from models.secrets import SecretSource, SecretState
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from backend.database.models import Secret, SecretInDb
from backend.database.services.base import DatabaseService
from backend.database.tables import SecretTable


class SecretService(DatabaseService[SecretTable, SecretInDb]):
    """Service layer for secret operations.

    Encryption/decryption is handled automatically by Pydantic validators/serializers.
    """

    def __init__(self, session: AsyncSession):
        super().__init__(SecretTable, SecretInDb, session)

    async def get_secrets(
        self, deployment_id: str, source: SecretSource | None = None
    ) -> list[SecretInDb]:
        """Get secrets by deployment id."""
        query = select(SecretTable).where(SecretTable.deployment_id == deployment_id)
        if source:
            query = query.where(SecretTable.source == source)

        result = await self._session.execute(query)
        return [self._to_pydantic(secret) for secret in result.scalars().all()]

    async def get_secret_by_key(
        self, deployment_id: str, key: str
    ) -> SecretInDb | None:
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
    ) -> SecretInDb | None:
        """Update an existing secret by deployment_id and key. Returns None if not found."""
        # Use temp model to trigger Pydantic's encryption serializer
        encrypted_value = Secret(
            deployment_id=deployment_id,
            key=key,
            value=value,
            source=source,
            state=state,
        ).model_dump()["value"]

        stmt = (
            update(SecretTable)
            .where(
                SecretTable.deployment_id == deployment_id,
                SecretTable.key == key,
            )
            .values(
                value=encrypted_value,
                source=source,
                state=state,
                updated_at=datetime.now(timezone.utc),
            )
            .returning(SecretTable)
        )
        result = await self._session.execute(
            stmt, execution_options={"populate_existing": True}
        )
        db_secret = result.scalar_one_or_none()
        return self._to_pydantic(db_secret) if db_secret else None

    async def delete_secret_by_key(self, deployment_id: str, key: str) -> bool:
        """Delete a single secret by deployment_id and key."""
        stmt = (
            delete(SecretTable)
            .where(
                SecretTable.deployment_id == deployment_id,
                SecretTable.key == key,
            )
            .returning(SecretTable.id)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none() is not None
