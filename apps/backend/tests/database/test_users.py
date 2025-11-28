"""Tests for UserService database operations."""

from backend.database import Database
from backend.database.users import (
    SubscriptionState,
    UserRole,
    UserStatus,
)

from tests.fixtures.database import (
    make_admin_user,
    make_user,
    requires_db,
)


@requires_db
class TestUserServiceCRUD:
    """Test basic CRUD operations for UserService."""

    async def test_create_user(self, db: Database):
        """Test creating a user."""
        user = make_user()
        created = await db.users.create(user)

        assert created.id is not None
        assert created.name == user.name
        assert created.email == user.email
        assert created.clerk_id == user.clerk_id
        assert created.role == UserRole.USER
        assert created.status == UserStatus.ACTIVE
        assert created.subscription_state == SubscriptionState.WITHIN_LIMITS
        assert created.created_at is not None
        assert created.updated_at is not None

    async def test_create_admin_user(self, db: Database):
        """Test creating an admin user."""
        user = make_admin_user()
        created = await db.users.create(user)

        assert created.role == UserRole.ADMIN

    async def test_get_user_by_id(self, db: Database):
        """Test retrieving a user by ID."""
        user = make_user()
        created = await db.users.create(user)

        retrieved = await db.users.get_by_id(created.id)

        assert retrieved is not None
        assert retrieved.id == created.id
        assert retrieved.email == created.email

    async def test_get_user_by_id_not_found(self, db: Database):
        """Test retrieving a non-existent user returns None."""
        result = await db.users.get_by_id("00000000-0000-0000-0000-000000000000")
        assert result is None

    async def test_update_user(self, db: Database):
        """Test updating a user."""
        user = make_user()
        created = await db.users.create(user)

        created.name = "Updated Name"
        created.status = UserStatus.INACTIVE
        updated = await db.users.update(created)

        assert updated is not None
        assert updated.name == "Updated Name"
        assert updated.status == UserStatus.INACTIVE

    async def test_delete_user(self, db: Database):
        """Test deleting a user."""
        user = make_user()
        created = await db.users.create(user)

        await db.users.delete(created.id)

        retrieved = await db.users.get_by_id(created.id)
        assert retrieved is None


@requires_db
class TestUserServiceQueries:
    """Test custom query methods for UserService."""

    async def test_get_by_clerk_id(self, db: Database):
        """Test retrieving a user by Clerk ID."""
        user = make_user()
        created = await db.users.create(user)

        retrieved = await db.users.get_by_clerk_id(created.clerk_id)

        assert retrieved is not None
        assert retrieved.id == created.id
        assert retrieved.clerk_id == created.clerk_id

    async def test_get_by_clerk_id_not_found(self, db: Database):
        """Test retrieving by non-existent Clerk ID returns None."""
        result = await db.users.get_by_clerk_id("non_existent_clerk_id")
        assert result is None

    async def test_get_by_email(self, db: Database):
        """Test retrieving a user by email."""
        user = make_user()
        created = await db.users.create(user)

        retrieved = await db.users.get_by_email(created.email)

        assert retrieved is not None
        assert retrieved.id == created.id
        assert retrieved.email == created.email

    async def test_get_by_email_not_found(self, db: Database):
        """Test retrieving by non-existent email returns None."""
        result = await db.users.get_by_email("nonexistent@example.com")
        assert result is None

    async def test_get_all_users(self, db: Database):
        """Test retrieving all users."""
        await db.users.create(make_user("user1"))
        await db.users.create(make_user("user2"))

        all_users = await db.users.get_all()

        assert isinstance(all_users, list)
        assert len(all_users) >= 2

    async def test_exists(self, db: Database):
        """Test checking if a user exists."""
        user = make_user()
        created = await db.users.create(user)

        exists = await db.users.exists(created.id)
        assert exists is True

        not_exists = await db.users.exists("00000000-0000-0000-0000-000000000000")
        assert not_exists is False


@requires_db
class TestUserServiceSubscriptionStates:
    """Test subscription state handling."""

    async def test_create_user_with_different_subscription_states(self, db: Database):
        """Test creating users with different subscription states."""
        states = [
            SubscriptionState.WITHIN_LIMITS,
            SubscriptionState.OVER_LIMITS,
            SubscriptionState.PAYMENT_FAILED,
            SubscriptionState.TRIAL_EXPIRED,
            SubscriptionState.SUSPENDED,
        ]

        for state in states:
            user = make_user()
            user.subscription_state = state
            created = await db.users.create(user)

            assert created.subscription_state == state

    async def test_update_subscription_state(self, db: Database):
        """Test updating a user's subscription state."""
        user = make_user()
        created = await db.users.create(user)

        assert created.subscription_state == SubscriptionState.WITHIN_LIMITS

        created.subscription_state = SubscriptionState.OVER_LIMITS
        updated = await db.users.update(created)

        assert updated is not None
        assert updated.subscription_state == SubscriptionState.OVER_LIMITS
