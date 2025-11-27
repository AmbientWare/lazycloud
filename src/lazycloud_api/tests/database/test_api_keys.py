"""Tests for ApiKeyService database operations."""

from datetime import datetime, timedelta, timezone

from lazycloud_api.database import Database
from lazycloud_api.tests.fixtures.database import (
    make_api_key,
    make_user,
    requires_db,
)


@requires_db
class TestApiKeyServiceCRUD:
    """Test basic CRUD operations for ApiKeyService."""

    async def test_create_api_key(self, db: Database):
        """Test creating an API key."""
        user = await db.users.create(make_user())
        api_key = make_api_key(user.id)
        created = await db.api_keys.create(api_key)

        assert created.id is not None
        assert created.name == api_key.name
        assert created.user_id == user.id
        assert created.value == api_key.value
        assert created.expires_at is not None
        assert created.created_at is not None

    async def test_create_api_key_with_custom_name(self, db: Database):
        """Test creating an API key with a custom name."""
        user = await db.users.create(make_user())
        api_key = make_api_key(user.id, name="My Custom Key")
        created = await db.api_keys.create(api_key)

        assert created.name == "My Custom Key"

    async def test_create_api_key_with_expiration(self, db: Database):
        """Test creating an API key with custom expiration."""
        user = await db.users.create(make_user())
        api_key = make_api_key(user.id, expires_in_days=365)
        created = await db.api_keys.create(api_key)

        expected_min = datetime.now(timezone.utc) + timedelta(days=364)
        assert created.expires_at > expected_min

    async def test_get_api_key_by_id(self, db: Database):
        """Test retrieving an API key by ID."""
        user = await db.users.create(make_user())
        api_key = make_api_key(user.id)
        created = await db.api_keys.create(api_key)

        retrieved = await db.api_keys.get_by_id(created.id)

        assert retrieved is not None
        assert retrieved.id == created.id
        assert retrieved.value == created.value

    async def test_update_api_key(self, db: Database):
        """Test updating an API key."""
        user = await db.users.create(make_user())
        api_key = make_api_key(user.id)
        created = await db.api_keys.create(api_key)

        new_expiry = datetime.now(timezone.utc) + timedelta(days=90)
        created.expires_at = new_expiry
        updated = await db.api_keys.update(created)

        assert updated is not None
        assert updated.expires_at == new_expiry

    async def test_delete_api_key(self, db: Database):
        """Test deleting an API key."""
        user = await db.users.create(make_user())
        api_key = make_api_key(user.id)
        created = await db.api_keys.create(api_key)

        await db.api_keys.delete(created.id)

        retrieved = await db.api_keys.get_by_id(created.id)
        assert retrieved is None


@requires_db
class TestApiKeyServiceQueries:
    """Test custom query methods for ApiKeyService."""

    async def test_get_by_user_id(self, db: Database):
        """Test retrieving all API keys for a user."""
        user = await db.users.create(make_user())

        key1 = make_api_key(user.id, name="Key 1")
        key2 = make_api_key(user.id, name="Key 2")
        key3 = make_api_key(user.id, name="Key 3")

        await db.api_keys.create(key1)
        await db.api_keys.create(key2)
        await db.api_keys.create(key3)

        keys = await db.api_keys.get_by_user_id(user.id)

        assert len(keys) == 3
        names = {k.name for k in keys}
        assert "Key 1" in names
        assert "Key 2" in names
        assert "Key 3" in names

    async def test_get_by_user_id_empty(self, db: Database):
        """Test get_by_user_id returns empty list for user with no keys."""
        user = await db.users.create(make_user())

        keys = await db.api_keys.get_by_user_id(user.id)

        assert keys == []

    async def test_user_by_value(self, db: Database):
        """Test looking up user ID by API key value."""
        user = await db.users.create(make_user())
        api_key = make_api_key(user.id)
        created = await db.api_keys.create(api_key)

        user_id = await db.api_keys.user_by_value(created.value)

        assert user_id == user.id

    async def test_user_by_value_not_found(self, db: Database):
        """Test user_by_value returns None for non-existent key."""
        result = await db.api_keys.user_by_value("non_existent_key_value")

        assert result is None


@requires_db
class TestApiKeyServiceMultipleUsers:
    """Test API key operations with multiple users."""

    async def test_different_users_have_separate_keys(self, db: Database):
        """Test that different users have separate API key collections."""
        user1 = await db.users.create(make_user("user1"))
        user2 = await db.users.create(make_user("user2"))

        await db.api_keys.create(make_api_key(user1.id, name="u1-key1"))
        await db.api_keys.create(make_api_key(user1.id, name="u1-key2"))
        await db.api_keys.create(make_api_key(user2.id, name="u2-key1"))

        user1_keys = await db.api_keys.get_by_user_id(user1.id)
        user2_keys = await db.api_keys.get_by_user_id(user2.id)

        assert len(user1_keys) == 2
        assert len(user2_keys) == 1

        for key in user1_keys:
            assert key.user_id == user1.id
        for key in user2_keys:
            assert key.user_id == user2.id

    async def test_user_by_value_returns_correct_user(self, db: Database):
        """Test that user_by_value returns the correct user for each key."""
        user1 = await db.users.create(make_user("user1"))
        user2 = await db.users.create(make_user("user2"))

        key1 = await db.api_keys.create(make_api_key(user1.id))
        key2 = await db.api_keys.create(make_api_key(user2.id))

        result1 = await db.api_keys.user_by_value(key1.value)
        result2 = await db.api_keys.user_by_value(key2.value)

        assert result1 == user1.id
        assert result2 == user2.id


@requires_db
class TestApiKeyServiceExpiration:
    """Test API key expiration scenarios."""

    async def test_create_keys_with_different_expirations(self, db: Database):
        """Test creating keys with various expiration periods."""
        user = await db.users.create(make_user())

        key_1_day = make_api_key(user.id, name="1-day", expires_in_days=1)
        key_30_day = make_api_key(user.id, name="30-day", expires_in_days=30)
        key_365_day = make_api_key(user.id, name="365-day", expires_in_days=365)

        created_1 = await db.api_keys.create(key_1_day)
        created_30 = await db.api_keys.create(key_30_day)
        created_365 = await db.api_keys.create(key_365_day)

        now = datetime.now(timezone.utc)

        assert created_1.expires_at < now + timedelta(days=2)
        assert created_30.expires_at > now + timedelta(days=29)
        assert created_365.expires_at > now + timedelta(days=364)

    async def test_update_key_expiration(self, db: Database):
        """Test updating an API key's expiration."""
        user = await db.users.create(make_user())
        api_key = make_api_key(user.id, expires_in_days=30)
        created = await db.api_keys.create(api_key)

        original_expiry = created.expires_at

        new_expiry = datetime.now(timezone.utc) + timedelta(days=365)
        created.expires_at = new_expiry
        updated = await db.api_keys.update(created)

        assert updated is not None
        assert updated.expires_at > original_expiry
