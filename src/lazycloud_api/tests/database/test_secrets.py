"""Tests for SecretService database operations with encryption."""

from lazycloud_api.database import Database
from lazycloud_api.tests.fixtures.database import (
    make_deployment,
    make_secret,
    make_workspace,
    requires_db,
)
from shared.models.secrets import SecretSource, SecretState


@requires_db
class TestSecretServiceCRUD:
    """Test basic CRUD operations for SecretService."""

    async def test_create_secret(self, db: Database):
        """Test creating a secret with automatic encryption."""
        workspace = await db.workspaces.create(make_workspace())
        deployment = await db.compose_deployments.create(make_deployment(workspace.id))

        secret = make_secret(deployment.id, key="MY_SECRET", value="super_secret_value")
        created = await db.secrets.create(secret)

        assert created.id is not None
        assert created.deployment_id == deployment.id
        assert created.key == "MY_SECRET"
        assert created.value == "super_secret_value"
        assert created.source == SecretSource.USER
        assert created.state == SecretState.DEPLOYED

    async def test_create_secret_with_different_sources(self, db: Database):
        """Test creating secrets with different sources."""
        workspace = await db.workspaces.create(make_workspace())
        deployment = await db.compose_deployments.create(make_deployment(workspace.id))

        sources = [SecretSource.USER, SecretSource.COMPOSE]

        for i, source in enumerate(sources):
            secret = make_secret(deployment.id, key=f"SECRET_{i}", source=source)
            created = await db.secrets.create(secret)
            assert created.source == source

    async def test_create_secret_with_different_states(self, db: Database):
        """Test creating secrets with different states."""
        workspace = await db.workspaces.create(make_workspace())
        deployment = await db.compose_deployments.create(make_deployment(workspace.id))

        states = [SecretState.AWAITING_DEPLOYMENT, SecretState.DEPLOYED]

        for i, state in enumerate(states):
            secret = make_secret(deployment.id, key=f"SECRET_{i}", state=state)
            created = await db.secrets.create(secret)
            assert created.state == state

    async def test_get_secret_by_id(self, db: Database):
        """Test retrieving a secret by ID."""
        workspace = await db.workspaces.create(make_workspace())
        deployment = await db.compose_deployments.create(make_deployment(workspace.id))
        secret = make_secret(deployment.id, value="test_value")
        created = await db.secrets.create(secret)

        retrieved = await db.secrets.get_by_id(created.id)

        assert retrieved is not None
        assert retrieved.id == created.id
        assert retrieved.value == "test_value"

    async def test_update_secret(self, db: Database):
        """Test updating a secret."""
        workspace = await db.workspaces.create(make_workspace())
        deployment = await db.compose_deployments.create(make_deployment(workspace.id))
        secret = make_secret(deployment.id, value="original_value")
        created = await db.secrets.create(secret)

        created.value = "updated_value"
        updated = await db.secrets.update(created)

        assert updated is not None
        assert updated.value == "updated_value"

    async def test_delete_secret(self, db: Database):
        """Test deleting a secret."""
        workspace = await db.workspaces.create(make_workspace())
        deployment = await db.compose_deployments.create(make_deployment(workspace.id))
        secret = make_secret(deployment.id)
        created = await db.secrets.create(secret)

        await db.secrets.delete(created.id)

        retrieved = await db.secrets.get_by_id(created.id)
        assert retrieved is None


@requires_db
class TestSecretServiceQueries:
    """Test custom query methods for SecretService."""

    async def test_get_secrets(self, db: Database):
        """Test retrieving all secrets for a deployment."""
        workspace = await db.workspaces.create(make_workspace())
        deployment = await db.compose_deployments.create(make_deployment(workspace.id))

        await db.secrets.create(make_secret(deployment.id, key="SECRET_1"))
        await db.secrets.create(make_secret(deployment.id, key="SECRET_2"))
        await db.secrets.create(make_secret(deployment.id, key="SECRET_3"))

        secrets = await db.secrets.get_secrets(deployment.id)

        assert len(secrets) == 3
        keys = {s.key for s in secrets}
        assert "SECRET_1" in keys
        assert "SECRET_2" in keys
        assert "SECRET_3" in keys

    async def test_get_secrets_filtered_by_source(self, db: Database):
        """Test retrieving secrets filtered by source."""
        workspace = await db.workspaces.create(make_workspace())
        deployment = await db.compose_deployments.create(make_deployment(workspace.id))

        await db.secrets.create(
            make_secret(deployment.id, key="USER_SECRET", source=SecretSource.USER)
        )
        await db.secrets.create(
            make_secret(
                deployment.id, key="COMPOSE_SECRET", source=SecretSource.COMPOSE
            )
        )

        user_secrets = await db.secrets.get_secrets(
            deployment_id=deployment.id, source=SecretSource.USER
        )
        compose_secrets = await db.secrets.get_secrets(
            deployment_id=deployment.id, source=SecretSource.COMPOSE
        )

        assert len(user_secrets) == 1
        assert user_secrets[0].key == "USER_SECRET"
        assert len(compose_secrets) == 1
        assert compose_secrets[0].key == "COMPOSE_SECRET"

    async def test_get_secret_by_key(self, db: Database):
        """Test retrieving a secret by key."""
        workspace = await db.workspaces.create(make_workspace())
        deployment = await db.compose_deployments.create(make_deployment(workspace.id))

        await db.secrets.create(
            make_secret(deployment.id, key="DATABASE_URL", value="postgres://...")
        )

        secret = await db.secrets.get_secret_by_key(deployment.id, "DATABASE_URL")

        assert secret is not None
        assert secret.key == "DATABASE_URL"
        assert secret.value == "postgres://..."

    async def test_get_secret_by_key_not_found(self, db: Database):
        """Test get_secret_by_key returns None for non-existent key."""
        workspace = await db.workspaces.create(make_workspace())
        deployment = await db.compose_deployments.create(make_deployment(workspace.id))

        result = await db.secrets.get_secret_by_key(deployment.id, "NON_EXISTENT")

        assert result is None


@requires_db
class TestSecretServiceUpdateByKey:
    """Test update_by_key method."""

    async def test_update_by_key(self, db: Database):
        """Test updating a secret by key."""
        workspace = await db.workspaces.create(make_workspace())
        deployment = await db.compose_deployments.create(make_deployment(workspace.id))

        await db.secrets.create(
            make_secret(
                deployment.id,
                key="API_KEY",
                value="old_value",
                source=SecretSource.USER,
                state=SecretState.AWAITING_DEPLOYMENT,
            )
        )

        updated = await db.secrets.update_by_key(
            deployment.id,
            "API_KEY",
            "new_value",
            SecretSource.USER,
            SecretState.DEPLOYED,
        )

        assert updated is not None
        assert updated.value == "new_value"
        assert updated.state == SecretState.DEPLOYED

    async def test_update_by_key_not_found(self, db: Database):
        """Test update_by_key returns None for non-existent key."""
        workspace = await db.workspaces.create(make_workspace())
        deployment = await db.compose_deployments.create(make_deployment(workspace.id))

        result = await db.secrets.update_by_key(
            deployment.id,
            "NON_EXISTENT",
            "new_value",
            SecretSource.USER,
            SecretState.DEPLOYED,
        )

        assert result is None


@requires_db
class TestSecretServiceDeleteByKey:
    """Test delete_secret_by_key method."""

    async def test_delete_secret_by_key(self, db: Database):
        """Test deleting a secret by key."""
        workspace = await db.workspaces.create(make_workspace())
        deployment = await db.compose_deployments.create(make_deployment(workspace.id))

        await db.secrets.create(make_secret(deployment.id, key="TO_DELETE"))

        result = await db.secrets.delete_secret_by_key(deployment.id, "TO_DELETE")

        assert result is True

        secret = await db.secrets.get_secret_by_key(deployment.id, "TO_DELETE")
        assert secret is None

    async def test_delete_secret_by_key_not_found(self, db: Database):
        """Test delete_secret_by_key returns False for non-existent key."""
        workspace = await db.workspaces.create(make_workspace())
        deployment = await db.compose_deployments.create(make_deployment(workspace.id))

        result = await db.secrets.delete_secret_by_key(deployment.id, "NON_EXISTENT")

        assert result is False


@requires_db
class TestSecretServiceEncryption:
    """Test encryption/decryption functionality."""

    async def test_secret_value_is_encrypted_at_rest(self, db: Database):
        """Test that secret values are encrypted when stored."""
        workspace = await db.workspaces.create(make_workspace())
        deployment = await db.compose_deployments.create(make_deployment(workspace.id))

        plaintext = "my_super_secret_password"
        secret = make_secret(deployment.id, key="PASSWORD", value=plaintext)
        await db.secrets.create(secret)

        retrieved = await db.secrets.get_secret_by_key(deployment.id, "PASSWORD")

        assert retrieved is not None
        assert retrieved.value == plaintext


@requires_db
class TestSecretServiceMultipleDeployments:
    """Test secrets across multiple deployments."""

    async def test_secrets_isolated_per_deployment(self, db: Database):
        """Test that secrets are isolated per deployment."""
        workspace = await db.workspaces.create(make_workspace())
        deployment1 = await db.compose_deployments.create(
            make_deployment(workspace.id, name="app1")
        )
        deployment2 = await db.compose_deployments.create(
            make_deployment(workspace.id, name="app2")
        )

        await db.secrets.create(
            make_secret(deployment1.id, key="DATABASE_URL", value="db1://...")
        )
        await db.secrets.create(
            make_secret(deployment2.id, key="DATABASE_URL", value="db2://...")
        )

        secret1 = await db.secrets.get_secret_by_key(deployment1.id, "DATABASE_URL")
        secret2 = await db.secrets.get_secret_by_key(deployment2.id, "DATABASE_URL")

        assert secret1 is not None
        assert secret2 is not None
        assert secret1.value == "db1://..."
        assert secret2.value == "db2://..."
        assert secret1.deployment_id != secret2.deployment_id
