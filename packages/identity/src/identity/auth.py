from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import secrets
import threading
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from time import monotonic
from typing import Protocol
from uuid import UUID

from database.repositories.identity import (
    AccountTokenCursor,
    AccountTokenPage,
    IdentityAdminRecoveryRequestRepository,
    IdentityBootstrapClaimRepository,
    TokenRepository,
    UserIdentityRepository,
    UserRepository,
    WorkspaceAuditRepository,
    WorkspaceMemberRepository,
    WorkspaceRepository,
)
from database.types import DatabaseSession
from shared.errors import ConflictError, InvalidInputError, NotFoundError
from shared.http.workspaces import WorkspaceAuditAction, WorkspaceAuditTarget
from shared.identity import (
    SYSTEM_TOKEN_KINDS,
    USER_PRINCIPAL_TOKEN_KINDS,
    AuthScope,
    AuthTokenRecord,
    IdentityProvider,
    PlatformRole,
    TokenKind,
    TokenStatus,
    UserRecord,
    UserStatus,
    WorkspaceMemberRecord,
    WorkspaceRecord,
    WorkspaceStatus,
)
from shared.timestamps import utc_now

from database import AsyncDatabaseClient, DatabaseClient
from identity.authz import (
    AuthzDecision,
    AuthzRequirement,
    decide_authorization,
    token_has_scope,
)
from identity.cursors import decode_created_at_cursor, encode_created_at_cursor
from identity.secret_hashing import pbkdf2_encode, pbkdf2_matches
from identity.token_invalidation import (
    AsyncAuthTokenInvalidation,
    AuthTokenInvalidation,
    configured_token_invalidation,
)


class AuthError(PermissionError):
    """Authentication failure: missing, invalid, disabled, or out-of-scope token."""


class AuthorizationDeniedError(AuthError):
    """An authenticated, active principal was denied by an authorization decision.

    API boundaries map this to HTTP 403 (the caller is known but not allowed),
    while plain `AuthError` maps to HTTP 401 (the caller is not authenticated).
    """


AUTH_TOKEN_CACHE_TTL_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class _CachedAuthToken:
    record: AuthTokenRecord
    expires_at: float
    generation: int | None = None


class AuthTokenCache:
    """One runtime's bounded positive bearer-token cache.

    The cache deliberately owns no identity context. Composition roots may
    share one instance across their prebuilt auth services and close it during
    runtime shutdown; standalone services receive an isolated cache that dies
    with the service instead of being retained by process-global state.
    """

    def __init__(self, *, ttl_seconds: float = AUTH_TOKEN_CACHE_TTL_SECONDS) -> None:
        self._ttl_seconds = max(ttl_seconds, 0)
        self._entries: dict[str, _CachedAuthToken] = {}
        self._lock = threading.Lock()
        self._closed = False

    def get(
        self,
        token_digest: str,
        *,
        now: datetime,
        generation: int | None = None,
    ) -> AuthTokenRecord | None:
        current = monotonic()
        with self._lock:
            self._require_open()
            cached = self._entries.get(token_digest)
            if cached is None:
                return None
            if (
                cached.expires_at <= current
                or (generation is not None and cached.generation != generation)
                or not cached.record.reusable
                or cached.record.status != TokenStatus.Active
                or cached.record.disabled_by_admin
                or (cached.record.expires_at is not None and cached.record.expires_at <= now)
            ):
                self._entries.pop(token_digest, None)
                return None
            return cached.record.model_copy(deep=True)

    def store(
        self,
        token_digest: str,
        record: AuthTokenRecord,
        *,
        generation: int | None = None,
    ) -> None:
        with self._lock:
            self._require_open()
            if not record.reusable:
                self._entries.pop(token_digest, None)
                return
            self._entries[token_digest] = _CachedAuthToken(
                record=record.model_copy(deep=True),
                expires_at=monotonic() + self._ttl_seconds,
                generation=generation,
            )

    def reset(self) -> None:
        """Discard all positive lookups while keeping this runtime cache usable."""
        with self._lock:
            self._require_open()
            self._entries.clear()

    def close(self) -> None:
        """Terminally clear the cache; repeated runtime shutdown is safe."""
        with self._lock:
            if self._closed:
                return
            self._entries.clear()
            self._closed = True

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("auth token cache is closed")


@dataclass(slots=True)
class BootstrapAdminToken:
    token: str = field(repr=False)
    record: AuthTokenRecord
    request_id: str
    replayed: bool = False
    user_id: str = ""
    """The administrator account the credential belongs to, empty on a replay."""


@dataclass(frozen=True, slots=True)
class IdentityDatabaseContext:
    database: DatabaseClient

    def workspace(
        self,
        session: DatabaseSession,
        workspace: str = "default",
    ) -> WorkspaceRecord:
        repository = WorkspaceRepository(session)
        workspace_id = try_uuid(workspace)
        record = (
            repository.get(workspace_id)
            if workspace_id is not None
            else repository.by_name(workspace)
        )
        if record is None or record.status is not WorkspaceStatus.Active:
            raise NotFoundError(f"workspace not found: {workspace}")
        return record


class IdentityContext(Protocol):
    @property
    def database(self) -> DatabaseClient: ...

    def workspace(
        self,
        session: DatabaseSession,
        workspace: str = "default",
    ) -> WorkspaceRecord: ...


@dataclass(slots=True)
class TokenIssuer:
    """Create credentials inside a transaction owned by the calling workflow."""

    context: IdentityContext
    token_cache: AuthTokenCache | None = None

    def issue(
        self,
        session: DatabaseSession,
        name: str,
        *,
        scopes: list[str] | None = None,
        expires_in_seconds: int | None = None,
        kind: TokenKind | str = TokenKind.Workspace,
        workspace_id: str = "default",
        worker_id: str = "",
        reusable: bool = True,
    ) -> tuple[str, AuthTokenRecord]:
        """Mint a credential scoped to one workspace.

        The mirror of the guard in ``issue_for_user``. Without it a kind that means
        "this names a person" could be stamped on a row that names a workspace, and
        the result reads as a platform administrator with no account behind it —
        authorized everywhere, attributable to nobody.
        """
        if TokenKind(kind) in USER_PRINCIPAL_TOKEN_KINDS:
            raise ValueError(f"not a workspace principal token kind: {TokenKind(kind).value}")
        return self._issue(
            session,
            name,
            raw_token=f"rt_{secrets.token_urlsafe(32)}",
            scopes=scopes,
            expires_in_seconds=expires_in_seconds,
            kind=kind,
            workspace_id=workspace_id,
            worker_id=worker_id,
            reusable=reusable,
        )

    def issue_for_user(
        self,
        session: DatabaseSession,
        name: str,
        *,
        user_id: str,
        kind: TokenKind = TokenKind.User,
        scopes: list[str] | None = None,
        expires_in_seconds: int | None = None,
        reusable: bool = True,
    ) -> tuple[str, AuthTokenRecord]:
        """Mint a credential that names a person rather than one workspace."""
        if kind not in USER_PRINCIPAL_TOKEN_KINDS:
            raise ValueError(f"not a user principal token kind: {kind.value}")
        return self._issue(
            session,
            name,
            raw_token=f"rt_{secrets.token_urlsafe(32)}",
            scopes=scopes,
            expires_in_seconds=expires_in_seconds,
            kind=kind,
            user_id=user_id,
            reusable=reusable,
        )

    def issue_configured_administrator(
        self,
        session: DatabaseSession,
        name: str,
        *,
        configured_token: str,
        user_id: str,
    ) -> tuple[str, AuthTokenRecord]:
        validated = _validate_configured_admin_token(configured_token)
        return self._issue(
            session,
            name,
            raw_token=validated,
            scopes=["*"],
            kind=TokenKind.Admin,
            user_id=user_id,
            reusable=True,
        )

    def _issue(
        self,
        session: DatabaseSession,
        name: str,
        *,
        raw_token: str,
        scopes: list[str] | None,
        expires_in_seconds: int | None = None,
        kind: TokenKind | str,
        user_id: str = "",
        workspace_id: str = "",
        worker_id: str = "",
        reusable: bool,
    ) -> tuple[str, AuthTokenRecord]:
        if bool(user_id) == bool(workspace_id):
            msg = "a token names exactly one principal: a user or a workspace"
            raise ValueError(msg)
        now = utc_now()
        expires_at = (
            now + timedelta(seconds=expires_in_seconds) if expires_in_seconds is not None else None
        )
        # Fence the owner the same way on both paths: a credential must not be minted
        # against a principal another transaction is in the middle of deleting.
        owner_workspace_id = ""
        if workspace_id:
            workspace = self.context.workspace(session, workspace_id)
            owner_workspace_id = WorkspaceRepository(session).lock_active_owner(workspace.id).id
        else:
            UserRepository(session).lock_active(user_id)
        record = TokenRepository(session).create(
            name=name,
            token_hash=_hash_token(raw_token),
            prefix=raw_token[:10],
            kind=TokenKind(kind),
            user_id=user_id,
            workspace_id=owner_workspace_id,
            worker_id=worker_id,
            scopes=scopes if scopes is not None else ["*"],
            reusable=reusable,
            expires_at=expires_at,
        )
        return raw_token, record

    def committed(self) -> None:
        # A new credential cannot stale a positive lookup. Clear only this
        # process's cache after the transaction has actually committed.
        if self.token_cache is not None:
            self.token_cache.reset()


def _create_bootstrap_administrator(
    session: DatabaseSession,
    *,
    name: str,
    github_user_id: int | None = None,
    github_login: str = "",
) -> UserRecord:
    """The first administrator, and optionally the identity that can sign in as them.

    The credential this account carries never depends on a provider, which is the
    point of it: it has to work when the identity provider is exactly what is broken.
    Naming a GitHub id here is additive — it says who may also reach the account
    through the dashboard, and takes nothing away from the token path.

    An id that already reaches an account adopts that account rather than making a
    second one. Bootstrap runs offline against the database by someone who could do
    either; refusing here would only mean whoever signed in first can never be the
    administrator this names.
    """
    users = UserRepository(session)
    if github_user_id is None:
        return users.create(display_name=name, role=PlatformRole.Administrator)

    identities = UserIdentityRepository(session)
    subject = str(github_user_id)
    identities.lock_subject(provider=IdentityProvider.Github, subject=subject)
    existing = identities.by_subject(provider=IdentityProvider.Github, subject=subject)
    if existing is not None:
        adopted = users.get(existing.user_id)
        if adopted is None:
            raise ConflictError(f"github identity {subject} names an account that is gone")
        if adopted.status is not UserStatus.Active:
            adopted = users.set_status(adopted.id, status=UserStatus.Active)
        return users.set_role(adopted.id, role=PlatformRole.Administrator)

    administrator = users.create(display_name=name, role=PlatformRole.Administrator)
    identities.link(
        user_id=administrator.id,
        provider=IdentityProvider.Github,
        subject=subject,
        subject_login=github_login,
    )
    return administrator


def _recovered_administrator(
    session: DatabaseSession,
    *,
    admin_token_id: str | None,
) -> UserRecord:
    """The account the bootstrap credential was minted against.

    Resolved through the claim rather than named by the operator, because an operator
    recovering access under pressure can mistype a selector and promote the wrong
    account, and there is no second credential left to undo it with.
    """
    if admin_token_id is None:
        raise AuthError("administrator bootstrap recorded no credential to recover through")
    token = TokenRepository(session).get_across_workspaces(admin_token_id)
    if token is None or not token.user_id:
        raise AuthError("the bootstrap administrator credential no longer names an account")
    repository = UserRepository(session)
    user = repository.get(token.user_id)
    if user is None:
        raise NotFoundError(f"user not found: {token.user_id}")
    if user.status is not UserStatus.Active:
        user = repository.set_status(user.id, status=UserStatus.Active)
    if user.role is not PlatformRole.Administrator:
        user = repository.set_role(user.id, role=PlatformRole.Administrator)
    return user


@dataclass(frozen=True, slots=True)
class AuthorizedPrincipal:
    """An authorized credential and the platform standing it carries.

    Resolving the role costs a read of the owning account, and a request that
    checks a workspace needs the same answer twice — once to authorize the
    credential, once to authorize the workspace. Carrying it makes those one read.
    """

    token: AuthTokenRecord
    platform_role: PlatformRole


@dataclass(frozen=True, slots=True)
class AccountTokenResult:
    page: AccountTokenPage
    next: str = ""


_TOKEN_ITERATIONS = 200_000
"""A token is 256 bits of urandom rather than a guess, so the cost here bounds what a
stolen table is worth, not what an online guessing attack can reach."""


def _hash_token(token: str, salt: str | None = None) -> str:
    return pbkdf2_encode(token, salt or secrets.token_hex(16), iterations=_TOKEN_ITERATIONS)


def _verify_token(token: str, encoded: str) -> bool:
    return pbkdf2_matches(token, encoded, iterations=_TOKEN_ITERATIONS)


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class _SingleFlight[KeyT, ResultT]:
    """One task per key and event loop, awaited by every caller that arrives meanwhile."""

    def __init__(self) -> None:
        self._tasks: dict[tuple[int, KeyT], asyncio.Task[ResultT]] = {}
        self._guard = threading.Lock()

    async def run(
        self,
        key: KeyT,
        operation: Callable[[], Coroutine[object, object, ResultT]],
    ) -> ResultT:
        flight = (id(asyncio.get_running_loop()), key)
        with self._guard:
            task = self._tasks.get(flight)
            if task is None:
                task = asyncio.create_task(operation())
                self._tasks[flight] = task
                task.add_done_callback(lambda done: self._discard(flight, done))
        return await asyncio.shield(task)

    def _discard(self, flight: tuple[int, KeyT], task: asyncio.Task[ResultT]) -> None:
        with self._guard:
            if self._tasks.get(flight) is task:
                del self._tasks[flight]


class AuthService:
    def __init__(
        self,
        context: IdentityContext,
        *,
        token_invalidation: AuthTokenInvalidation | None = None,
        token_cache: AuthTokenCache | None = None,
    ) -> None:
        self.context = context
        self._token_invalidation = token_invalidation
        self.token_cache = token_cache or AuthTokenCache()
        self._authentication_flights: _SingleFlight[tuple[str, str | None], AuthTokenRecord] = (
            _SingleFlight()
        )
        self._platform_role_flights: _SingleFlight[str, PlatformRole] = _SingleFlight()
        self._workspace_access_flights: _SingleFlight[
            tuple[str, str],
            tuple[str, WorkspaceMemberRecord | None],
        ] = _SingleFlight()

    def _invalidation(self) -> AuthTokenInvalidation | None:
        return self._token_invalidation or configured_token_invalidation()

    def _invalidate_token_caches(self) -> None:
        """Drop this process's token cache and signal every replica to do the same."""
        self.token_cache.reset()
        invalidation = self._invalidation()
        if invalidation is not None:
            invalidation.emit()

    def credentials_revoked(self) -> None:
        """Publish a committed credential revocation to every replica's token cache.

        Called after the revoking transaction commits, whether for workspace deletion,
        a disabled account, or administrator recovery, so a cached positive cannot
        outlive it.
        """
        self._invalidate_token_caches()

    def platform_role(self, token: AuthTokenRecord) -> PlatformRole:
        """The one answer to whether a caller administers the platform.

        Two things confer it: the administrator token kind, which platform-minted
        operator credentials still carry, and an account whose role says so. Every
        caller, the authorization decision and the routes that branch on it alike,
        asks here, so the two cannot drift into disagreeing.
        """
        role = _platform_role_from_token(token)
        if role is not None:
            return role
        with self.context.database.session() as session:
            user = UserRepository(session).get(token.user_id)
        return _platform_role_from_user(user)

    async def platform_role_async(
        self,
        database: AsyncDatabaseClient,
        token: AuthTokenRecord,
    ) -> PlatformRole:
        role = _platform_role_from_token(token)
        if role is not None:
            return role
        user_id = token.user_id
        return await self._platform_role_flights.run(
            user_id,
            lambda: self._load_platform_role_async(database, user_id),
        )

    async def _load_platform_role_async(
        self,
        database: AsyncDatabaseClient,
        user_id: str,
    ) -> PlatformRole:
        user = await database.run_transaction(lambda session: UserRepository(session).get(user_id))
        return _platform_role_from_user(user)

    async def workspace_access_async(
        self,
        database: AsyncDatabaseClient,
        token: AuthTokenRecord,
        workspace: str,
    ) -> tuple[str, WorkspaceMemberRecord | None]:
        user_id = token.user_id if token.names_user and token.user_id else ""
        return await self._workspace_access_flights.run(
            (user_id, workspace),
            lambda: self._load_workspace_access_async(database, user_id, workspace),
        )

    async def _load_workspace_access_async(
        self,
        database: AsyncDatabaseClient,
        user_id: str,
        workspace: str,
    ) -> tuple[str, WorkspaceMemberRecord | None]:
        def resolve(session: DatabaseSession) -> tuple[str, WorkspaceMemberRecord | None]:
            record = self.context.workspace(session, workspace)
            membership = (
                WorkspaceMemberRepository(session).membership(
                    workspace_id=record.id,
                    user_id=user_id,
                )
                if user_id
                else None
            )
            return record.id, membership

        return await database.run_transaction(resolve)

    def create_token(
        self,
        name: str,
        *,
        scopes: list[str] | None = None,
        expires_in_seconds: int | None = None,
        kind: TokenKind | str = TokenKind.Workspace,
        workspace_id: str = "default",
        worker_id: str = "",
        reusable: bool = True,
    ) -> tuple[str, AuthTokenRecord]:
        issuer = TokenIssuer(self.context, self.token_cache)
        with self.context.database.session() as session:
            raw_token, record = issuer.issue(
                session,
                name,
                scopes=scopes,
                expires_in_seconds=expires_in_seconds,
                kind=kind,
                workspace_id=workspace_id,
                worker_id=worker_id,
                reusable=reusable,
            )
        issuer.committed()
        return raw_token, record

    def list_tokens(self) -> list[AuthTokenRecord]:
        """Admin/operator listing over every workspace's tokens."""
        with self.context.database.session() as session:
            records = TokenRepository(session).list_across_workspaces()
        records.sort(key=lambda item: item.created_at, reverse=True)
        return records

    def token_count(self) -> int:
        with self.context.database.session() as session:
            return len(TokenRepository(session).list_across_workspaces())

    def bootstrap_required(self) -> bool:
        with self.context.database.session() as session:
            return IdentityBootstrapClaimRepository(session).required()

    def administrator_ready(self) -> bool:
        with self.context.database.session() as session:
            return IdentityBootstrapClaimRepository(session).administrator_ready()

    def bootstrap_request_id(self) -> str | None:
        with self.context.database.session() as session:
            claim = IdentityBootstrapClaimRepository(session).get()
            return claim.request_id if claim is not None else None

    def recovery_request_exists(self, request_id: str) -> bool:
        _validate_offline_request_id(request_id)
        with self.context.database.session() as session:
            return IdentityAdminRecoveryRequestRepository(session).get(request_id) is not None

    def bootstrap_administrator(
        self,
        *,
        request_id: str,
        name: str = "first-admin",
        workspace: str = "default",
        github_user_id: int | None = None,
        github_login: str = "",
        staged_token: str | None = None,
        configured_token: str | None = None,
        stage_token: Callable[[str], None] | None = None,
    ) -> BootstrapAdminToken:
        """Create the first administrator: the account, their workspace, and a credential.

        The account is what the bootstrap produces. Every later credential is minted
        against one, so it has to exist before anything else can own something.
        """
        _validate_offline_request_id(request_id)
        selected_token = _bootstrap_token(staged_token, configured_token)
        issuer = TokenIssuer(self.context, self.token_cache)
        created = False
        with self.context.database.session() as session:
            claim_repository = IdentityBootstrapClaimRepository(session)
            claim = claim_repository.get(lock=True)
            if claim is not None:
                if claim.request_id != request_id:
                    raise AuthError(
                        "administrator bootstrap is already complete; use offline recovery"
                    )
                record = _claimed_token(
                    TokenRepository(session),
                    claim.admin_token_id,
                    selected_token,
                    action="bootstrap",
                )
                return BootstrapAdminToken(
                    token=selected_token or "",
                    record=record,
                    request_id=request_id,
                    replayed=True,
                )
            if not claim_repository.create(request_id=request_id):
                claim = claim_repository.get(lock=True)
                if claim is None or claim.request_id != request_id:
                    raise AuthError(
                        "administrator bootstrap is already complete; use offline recovery"
                    )
                record = _claimed_token(
                    TokenRepository(session),
                    claim.admin_token_id,
                    selected_token,
                    action="bootstrap",
                )
                return BootstrapAdminToken(
                    token=selected_token or "",
                    record=record,
                    request_id=request_id,
                    replayed=True,
                )
            administrator = _create_bootstrap_administrator(
                session,
                name=name,
                github_user_id=github_user_id,
                github_login=github_login,
            )
            workspace_record = WorkspaceRepository(session).ensure_named(workspace)
            # Bootstrapping into a workspace that already has an owner leaves that
            # owner in place: this creates the administrator credential, and an
            # administrator reaches every workspace through their platform role
            # without owning one.
            WorkspaceMemberRepository(session).ensure_owner(
                workspace_id=workspace_record.id,
                user_id=administrator.id,
            )
            if configured_token is None:
                raw_token, record = issuer.issue_for_user(
                    session,
                    name,
                    scopes=["*"],
                    kind=TokenKind.Admin,
                    user_id=administrator.id,
                    reusable=True,
                )
            else:
                raw_token, record = issuer.issue_configured_administrator(
                    session,
                    name,
                    configured_token=configured_token,
                    user_id=administrator.id,
                )
            claim_repository.attach_admin_token(record.id)
            WorkspaceAuditRepository(session).append(
                workspace_id=workspace_record.id,
                action=WorkspaceAuditAction.AdministratorBootstrapped,
                actor=record,
                target_type=WorkspaceAuditTarget.Token,
                target_id=record.id,
                target_name=record.name,
                summary="Bootstrapped the initial administrator credential offline",
                new_value=request_id,
            )
            if stage_token is not None:
                stage_token(raw_token)
            created = True
        issuer.committed()
        return BootstrapAdminToken(
            token=raw_token,
            record=record,
            request_id=request_id,
            replayed=not created,
            user_id=administrator.id,
        )

    def recover_admin_token(
        self,
        *,
        request_id: str,
        name: str = "recovery-admin",
        workspace: str = "default",
        staged_token: str | None = None,
        stage_token: Callable[[str], None] | None = None,
    ) -> BootstrapAdminToken:
        """Re-establish administrator access offline for the bootstrapped account.

        Recovery re-keys a person rather than minting a free-floating credential: the
        account is what owns the workspaces and the connected compute, so an operator
        who has lost access needs that account back, not a second identity beside it.
        """
        _validate_offline_request_id(request_id)
        issuer = TokenIssuer(self.context, self.token_cache)
        with self.context.database.session() as session:
            claim_repository = IdentityBootstrapClaimRepository(session)
            claim = claim_repository.get(lock=True)
            if claim is None:
                raise AuthError("administrator bootstrap has not completed")
            recovery_repository = IdentityAdminRecoveryRequestRepository(session)
            existing = recovery_repository.get(request_id)
            token_repository = TokenRepository(session)
            if existing is not None:
                record = _claimed_token(
                    token_repository,
                    existing.admin_token_id,
                    staged_token,
                    action="recovery",
                )
                return BootstrapAdminToken(
                    token=staged_token or "",
                    record=record,
                    request_id=request_id,
                    replayed=True,
                )
            workspace_repository = WorkspaceRepository(session)
            workspace_record = workspace_repository.by_name(workspace)
            if workspace_record is None or workspace_record.status is not WorkspaceStatus.Active:
                raise NotFoundError(f"workspace not found: {workspace}")
            administrator = _recovered_administrator(
                session,
                admin_token_id=claim.admin_token_id,
            )
            raw_token, record = issuer.issue_for_user(
                session,
                name,
                scopes=["*"],
                kind=TokenKind.Admin,
                user_id=administrator.id,
                reusable=True,
            )
            recovery_repository.create(
                request_id=request_id,
                workspace_id=workspace_record.id,
                admin_token_id=record.id,
            )
            WorkspaceAuditRepository(session).append(
                workspace_id=workspace_record.id,
                action=WorkspaceAuditAction.AdministratorRecovered,
                actor=record,
                target_type=WorkspaceAuditTarget.Token,
                target_id=record.id,
                target_name=record.name,
                summary="Recovered administrator access while the control plane was stopped",
                new_value=request_id,
            )
            if stage_token is not None:
                stage_token(raw_token)
        issuer.committed()
        # Recovery can reactivate and promote an existing account, neither of which a
        # replica sees until its token cache is dropped.
        self._invalidate_token_caches()
        return BootstrapAdminToken(
            token=raw_token,
            record=record,
            request_id=request_id,
            user_id=administrator.id,
        )

    def mark_admin_token_published(self, *, request_id: str, recovery: bool) -> None:
        _validate_offline_request_id(request_id)
        with self.context.database.session() as session:
            if recovery:
                repository = IdentityAdminRecoveryRequestRepository(session)
                if repository.get(request_id) is None:
                    raise AuthError(f"administrator recovery request not found: {request_id}")
                repository.mark_published(request_id)
                return
            repository = IdentityBootstrapClaimRepository(session)
            claim = repository.get(lock=True)
            if claim is None or claim.request_id != request_id:
                raise AuthError("administrator bootstrap request does not own the claim")
            repository.mark_published()

    def create_service_token(
        self,
        name: str,
        *,
        kind: TokenKind,
        workspace_id: str = "default",
        scopes: list[str] | None = None,
        stage_token: Callable[[str], None] | None = None,
    ) -> tuple[str, AuthTokenRecord]:
        if kind not in SYSTEM_TOKEN_KINDS:
            raise ValueError(f"not a platform service token kind: {kind.value}")
        issuer = TokenIssuer(self.context, self.token_cache)
        with self.context.database.session() as session:
            claim_repository = IdentityBootstrapClaimRepository(session)
            claim_repository.get(lock=True)
            if not claim_repository.administrator_ready():
                raise AuthError("offline administrator bootstrap must complete first")
            workspace_repository = WorkspaceRepository(session)
            normalized_workspace_id = try_uuid(workspace_id)
            workspace = (
                workspace_repository.get(normalized_workspace_id)
                if normalized_workspace_id is not None
                else workspace_repository.by_name(workspace_id)
            )
            if workspace is None or workspace.status is not WorkspaceStatus.Active:
                raise NotFoundError(f"workspace not found: {workspace_id}")
            token_repository = TokenRepository(session)
            for existing in token_repository.list_owned_credentials(
                workspace_id=workspace.id,
                name=name,
                kind=kind,
            ):
                token_repository.delete(existing.id, workspace_id=workspace.id)
            raw_token, record = issuer.issue(
                session,
                name,
                scopes=scopes,
                kind=kind,
                workspace_id=workspace.id,
            )
            if stage_token is not None:
                stage_token(raw_token)
        issuer.committed()
        return raw_token, record

    def validate_service_token(
        self,
        token: str,
        *,
        name: str,
        kind: TokenKind,
        workspace_id: str,
        scopes: list[str] | None = None,
    ) -> AuthTokenRecord:
        """A published service credential still matching what this owner would mint.

        Scopes are part of that match: a published credential granting more than the
        caller asks for stays valid forever otherwise, so narrowing what a service
        may do would never reach a host that already has a file.
        """
        if not self.administrator_ready():
            raise AuthError("offline administrator bootstrap must complete first")
        record = self.authenticate(token)
        normalized_workspace_id = self._workspace_id(workspace_id)
        if (
            record.name != name
            or record.kind is not kind
            or record.workspace_id != normalized_workspace_id
            or (scopes is not None and sorted(record.scopes) != sorted(scopes))
        ):
            raise AuthError("credential does not match the service-token owner")
        return record

    def prune_expired_system_tokens(self, *, now: datetime | None = None) -> int:
        """Delete expired platform-minted tokens (gateway runtime, worker, machine).

        User-created tokens are never pruned here; expired ones stay visible so
        their owners can see and delete them explicitly.
        """
        current = now or utc_now()
        with self.context.database.session() as session:
            pruned = TokenRepository(session).prune_expired(
                now=current,
                kinds=SYSTEM_TOKEN_KINDS,
            )
        if pruned:
            self._invalidate_token_caches()
        return pruned

    def revoke_token(self, token_id_or_name: str) -> AuthTokenRecord:
        record = self._find_token(token_id_or_name)
        with self.context.database.session() as session:
            updated = TokenRepository(session).revoke_across_workspaces(
                record.id,
                now=utc_now(),
            )
        if updated is None:
            raise KeyError(f"token not found: {token_id_or_name}")
        self._invalidate_token_caches()
        return updated

    def create_workspace_token(
        self,
        workspace_id_or_name: str,
        *,
        kind: TokenKind | str = TokenKind.Workspace,
        name: str | None = None,
        scopes: list[str] | None = None,
        reusable: bool = True,
    ) -> tuple[str, AuthTokenRecord]:
        workspace_id = self._workspace_id(workspace_id_or_name)
        return self.create_token(
            name or f"{workspace_id}-{TokenKind(kind).value}",
            kind=kind,
            workspace_id=workspace_id,
            scopes=scopes,
            reusable=reusable,
        )

    def create_account_token(
        self,
        user_id: str,
        name: str,
        *,
        expires_in_seconds: int | None = None,
    ) -> tuple[str, AuthTokenRecord]:
        """Mint a named, long-lived credential for one account.

        The account is the tenant, so this reaches every workspace its owner belongs
        to, decided per request from membership exactly as a session is. It differs
        from a session only in living past a sign-in and in being revocable by name.
        """
        issuer = TokenIssuer(self.context)
        with self.context.database.session() as session:
            raw_token, record = issuer.issue_for_user(
                session,
                name,
                kind=TokenKind.User,
                user_id=user_id,
                expires_in_seconds=expires_in_seconds,
            )
        issuer.committed()
        return raw_token, record

    def list_account_tokens(
        self,
        user_id: str,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> AccountTokenResult:
        if limit < 1 or limit > 100:
            raise InvalidInputError("account token limit must be between 1 and 100")
        decoded = decode_created_at_cursor(
            cursor, build=AccountTokenCursor, subject="account token"
        )
        with self.context.database.session() as session:
            page = TokenRepository(session).list_manageable_for_user(
                user_id,
                limit=limit,
                cursor=decoded,
            )
        return AccountTokenResult(
            page=page,
            next=(
                encode_created_at_cursor(page.next.created_at, page.next.id)
                if page.next is not None
                else ""
            ),
        )

    def revoke_account_token(self, user_id: str, token_id: str) -> AuthTokenRecord:
        with self.context.database.session() as session:
            updated = TokenRepository(session).revoke_for_user(
                token_id,
                user_id=user_id,
                now=utc_now(),
            )
        if updated is None:
            raise NotFoundError(f"account token not found: {token_id}")
        self._invalidate_token_caches()
        return updated

    def list_workspace_tokens(self, workspace_id_or_name: str) -> list[AuthTokenRecord]:
        workspace_id = self._workspace_id(workspace_id_or_name)
        with self.context.database.session() as session:
            records = TokenRepository(session).list(workspace_id=workspace_id)
        records.sort(key=lambda item: item.created_at, reverse=True)
        return records

    def get_workspace_token(
        self,
        workspace_id_or_name: str,
        token_id: str,
    ) -> AuthTokenRecord | None:
        workspace_id = self._workspace_id(workspace_id_or_name)
        with self.context.database.session() as session:
            return TokenRepository(session).get(token_id, workspace_id=workspace_id)

    def revoke_workspace_token(
        self,
        workspace_id_or_name: str,
        token_id_or_name: str,
    ) -> AuthTokenRecord:
        record = self._find_workspace_token(workspace_id_or_name, token_id_or_name)
        with self.context.database.session() as session:
            updated = TokenRepository(session).revoke(
                record.id,
                workspace_id=record.workspace_id,
                now=utc_now(),
            )
            if updated is None:
                raise NotFoundError(f"workspace token not found: {token_id_or_name}")
        self._invalidate_token_caches()
        return updated

    def set_workspace_tokens_admin_disabled(
        self,
        workspace_id_or_name: str,
        *,
        disabled: bool,
    ) -> list[AuthTokenRecord]:
        workspace_id = self._workspace_id(workspace_id_or_name)
        records: list[AuthTokenRecord] = []
        with self.context.database.session() as session:
            repository = TokenRepository(session)
            for record in repository.list(workspace_id=workspace_id):
                updated = repository.set_admin_disabled(
                    record.id,
                    workspace_id=workspace_id,
                    disabled=disabled,
                    now=utc_now(),
                )
                if updated is not None:
                    records.append(updated)
        records.sort(key=lambda item: item.created_at, reverse=True)
        self._invalidate_token_caches()
        return records

    def authenticate(self, token: str, *, scope: AuthScope | str | None = None) -> AuthTokenRecord:
        now = utc_now()
        token_digest = _token_digest(token)
        invalidation = self._invalidation()
        generation: int | None = None
        cache_usable = True
        if invalidation is not None:
            generation = invalidation.current_generation()
            # Fail safe: when the invalidation generation cannot be read, a cached
            # entry cannot be proven current, so authenticate against the database.
            cache_usable = generation is not None
        if cache_usable:
            cached = self.token_cache.get(token_digest, now=now, generation=generation)
            if cached is not None:
                _require_scope(cached, scope)
                return cached
        updated: AuthTokenRecord | None = None
        revoked_expired = False
        consumed = False
        try:
            with self.context.database.session() as session:
                repository = TokenRepository(session)
                expired_ids: list[str] = []
                matched: AuthTokenRecord | None = None
                for record in repository.list_by_prefix(token[:10]):
                    if record.status is not TokenStatus.Active:
                        continue
                    if record.expires_at is not None and record.expires_at <= now:
                        expired_ids.append(record.id)
                        continue
                    if _verify_token(token, record.token_hash):
                        matched = record
                        break
                updated, revoked_expired, consumed = _settle_authentication(
                    repository,
                    matched,
                    expired_ids,
                    now=now,
                    scope=scope,
                )
        finally:
            if revoked_expired or consumed:
                self._invalidate_token_caches()
        if updated is None:
            raise AuthError("invalid token")
        if updated.reusable and cache_usable and not revoked_expired:
            self.token_cache.store(token_digest, updated, generation=generation)
        return updated

    async def authenticate_async(
        self,
        database: AsyncDatabaseClient,
        invalidation: AsyncAuthTokenInvalidation,
        token: str,
        *,
        scope: AuthScope | str | None = None,
    ) -> AuthTokenRecord:
        now = utc_now()
        token_digest = _token_digest(token)
        generation = await invalidation.current_generation()
        cache_usable = generation is not None
        if cache_usable:
            cached = self.token_cache.get(token_digest, now=now, generation=generation)
            if cached is not None:
                _require_scope(cached, scope)
                return cached

        # Scope is part of the flight so a refused scope never records a use,
        # which is what keeps a single-use token intact for its rightful caller.
        scope_value = scope.value if isinstance(scope, AuthScope) else scope
        updated = await self._authentication_flights.run(
            (token_digest, scope_value),
            lambda: self._authenticate_uncached_async(
                database,
                invalidation,
                token,
                token_digest=token_digest,
                now=now,
                generation=generation,
                cache_usable=cache_usable,
                scope=scope,
            ),
        )
        _require_scope(updated, scope)
        return updated

    async def _authenticate_uncached_async(
        self,
        database: AsyncDatabaseClient,
        invalidation: AsyncAuthTokenInvalidation,
        token: str,
        *,
        token_digest: str,
        now: datetime,
        generation: int | None,
        cache_usable: bool,
        scope: AuthScope | str | None,
    ) -> AuthTokenRecord:
        if cache_usable:
            cached = self.token_cache.get(token_digest, now=now, generation=generation)
            if cached is not None:
                return cached

        records = await database.run_transaction(
            lambda session: TokenRepository(session).list_by_prefix(token[:10])
        )
        expired_ids: list[str] = []
        matched: AuthTokenRecord | None = None
        for record in records:
            if record.status is not TokenStatus.Active:
                continue
            if record.expires_at is not None and record.expires_at <= now:
                expired_ids.append(record.id)
                continue
            if await asyncio.to_thread(_verify_token, token, record.token_hash):
                matched = record
                break

        updated, revoked_expired, consumed = await database.run_transaction(
            lambda session: _settle_authentication(
                TokenRepository(session),
                matched,
                expired_ids,
                now=now,
                scope=scope,
            )
        )
        if revoked_expired or consumed:
            self.token_cache.reset()
            await invalidation.emit()
        if updated is None:
            raise AuthError("invalid token")
        if updated.reusable and cache_usable and not revoked_expired:
            self.token_cache.store(token_digest, updated, generation=generation)
        return updated

    async def authorize_principal_async(
        self,
        database: AsyncDatabaseClient,
        invalidation: AsyncAuthTokenInvalidation,
        authorization: str | None,
        requirement: AuthzRequirement,
        *,
        allow_if_no_tokens: bool = False,
    ) -> AuthorizedPrincipal | None:
        if allow_if_no_tokens:
            tokens = await database.run_transaction(
                lambda session: TokenRepository(session).list_across_workspaces()
            )
            if not tokens:
                return None
        token = await self.authenticate_async(database, invalidation, _bearer_token(authorization))
        platform_role = await self.platform_role_async(database, token)
        return _authorized_principal(token, requirement, platform_role)

    async def authenticate_header_async(
        self,
        database: AsyncDatabaseClient,
        invalidation: AsyncAuthTokenInvalidation,
        authorization: str | None,
        *,
        scope: AuthScope | str | None = None,
    ) -> AuthTokenRecord:
        return await self.authenticate_async(
            database,
            invalidation,
            _bearer_token(authorization),
            scope=scope,
        )

    def authenticate_header(
        self,
        authorization: str | None,
        *,
        scope: AuthScope | str | None = None,
        allow_if_no_tokens: bool = False,
    ) -> AuthTokenRecord | None:
        if allow_if_no_tokens and not self.list_tokens():
            return None
        return self.authenticate(_bearer_token(authorization), scope=scope)

    def authorize_principal(
        self,
        authorization: str | None,
        requirement: AuthzRequirement,
        *,
        allow_if_no_tokens: bool = False,
    ) -> AuthorizedPrincipal | None:
        token = self.authenticate_header(
            authorization,
            allow_if_no_tokens=allow_if_no_tokens,
        )
        if token is None:
            return None
        return _authorized_principal(token, requirement, self.platform_role(token))

    def authorize_header(
        self,
        authorization: str | None,
        requirement: AuthzRequirement,
        *,
        allow_if_no_tokens: bool = False,
    ) -> AuthTokenRecord | None:
        principal = self.authorize_principal(
            authorization,
            requirement,
            allow_if_no_tokens=allow_if_no_tokens,
        )
        return principal.token if principal is not None else None

    async def authorize_token_identity(
        self,
        database: AsyncDatabaseClient,
        invalidation: AsyncAuthTokenInvalidation,
        token_id: str,
        *,
        token_user_id: str = "",
        token_workspace_id: str = "",
        requirement: AuthzRequirement,
    ) -> AuthTokenRecord:
        """Reload and authorize an identity a credential exchange recorded earlier.

        The bearer-token cache is bypassed on purpose: a ticket is redeemed after
        the token was authenticated, and revocation, expiry, deletion, or a scope
        change in between must refuse it.
        """

        now = utc_now()

        def authorize(session: DatabaseSession) -> tuple[AuthTokenRecord | None, bool]:
            repository = TokenRepository(session)
            # Reloaded through the principal the exchange recorded, so a credential
            # cannot be redeemed as one belonging to someone else.
            record = (
                repository.get_for_user(token_id, user_id=token_user_id)
                if token_user_id
                else repository.get(token_id, workspace_id=token_workspace_id)
            )
            if (
                record is None
                or record.status is not TokenStatus.Active
                or not record.reusable
                or repository.is_consumed(token_id, workspace_id=token_workspace_id or None)
            ):
                raise AuthError("invalid token identity")
            if record.expires_at is not None and record.expires_at <= now:
                repository.revoke_if_expired(record.id, now=now)
                return None, True
            elif record.disabled_by_admin:
                raise AuthError("token has been disabled by an administrator")
            return repository.mark_reusable_used(record.id, now=now), False

        updated, expired = await database.run_transaction(authorize)
        if expired:
            self.token_cache.reset()
            await invalidation.emit()
            raise AuthError("token has expired")
        if updated is None:
            raise AuthError("invalid token identity")
        platform_role = await self.platform_role_async(database, updated)
        return _authorized_principal(updated, requirement, platform_role).token

    def decide_header(
        self,
        authorization: str | None,
        requirement: AuthzRequirement,
        *,
        allow_if_no_tokens: bool = False,
    ) -> AuthzDecision:
        token = self.authenticate_header(
            authorization,
            allow_if_no_tokens=allow_if_no_tokens,
        )
        if token is None:
            return decide_authorization(None, requirement)
        return decide_authorization(
            token,
            requirement,
            platform_role=self.platform_role(token),
        )

    def _find_token(self, token_id_or_name: str) -> AuthTokenRecord:
        """Admin/operator token lookup across every workspace."""
        with self.context.database.session() as session:
            repository = TokenRepository(session)
            token_id = try_uuid(token_id_or_name)
            if token_id is not None:
                record = repository.get_across_workspaces(token_id)
                if record is not None:
                    return record
            matches = [
                record
                for record in repository.list_across_workspaces()
                if record.name == token_id_or_name
            ]
        if not matches:
            msg = f"token not found: {token_id_or_name}"
            raise KeyError(msg)
        return matches[0]

    def _workspace_id(self, workspace_id_or_name: str) -> str:
        with self.context.database.session() as session:
            repository = WorkspaceRepository(session)
            workspace_id = try_uuid(workspace_id_or_name)
            workspace = (
                repository.get(workspace_id)
                if workspace_id is not None
                else repository.by_name(workspace_id_or_name)
            )
            if workspace is None or workspace.status is not WorkspaceStatus.Active:
                raise NotFoundError(f"workspace not found: {workspace_id_or_name}")
            return workspace.id

    def _find_workspace_token(
        self,
        workspace_id_or_name: str,
        token_id_or_name: str,
    ) -> AuthTokenRecord:
        workspace_id = self._workspace_id(workspace_id_or_name)
        with self.context.database.session() as session:
            repository = TokenRepository(session)
            token_id = try_uuid(token_id_or_name)
            if token_id is not None:
                record = repository.get(token_id, workspace_id=workspace_id)
                if record is not None:
                    return record
            matches = [
                record
                for record in repository.list(workspace_id=workspace_id)
                if record.name == token_id_or_name
            ]
        if not matches:
            msg = f"workspace token not found: {token_id_or_name}"
            raise NotFoundError(msg)
        return matches[0]


def _require_scope(record: AuthTokenRecord, scope: AuthScope | str | None) -> None:
    if not token_has_scope(record, scope):
        scope_value = scope.value if isinstance(scope, AuthScope) else scope
        msg = f"token is missing scope: {scope_value}"
        raise AuthError(msg)


def _bearer_token(authorization: str | None) -> str:
    if authorization is None or not authorization.startswith("Bearer "):
        raise AuthError("missing bearer token")
    return authorization.removeprefix("Bearer ").strip()


def _platform_role_from_token(token: AuthTokenRecord) -> PlatformRole | None:
    """The role the token alone settles; None when the account's own row decides."""

    if token.kind is TokenKind.Admin:
        return PlatformRole.Administrator
    if not token.names_user or not token.user_id:
        return PlatformRole.Member
    return None


def _platform_role_from_user(user: UserRecord | None) -> PlatformRole:
    if user is None or user.status is not UserStatus.Active:
        return PlatformRole.Member
    return user.role


def _authorized_principal(
    token: AuthTokenRecord,
    requirement: AuthzRequirement,
    platform_role: PlatformRole,
) -> AuthorizedPrincipal:
    decision = decide_authorization(token, requirement, platform_role=platform_role)
    if not decision.allowed:
        raise AuthorizationDeniedError(decision.message)
    return AuthorizedPrincipal(token, platform_role)


def _settle_authentication(
    repository: TokenRepository,
    matched: AuthTokenRecord | None,
    expired_ids: Sequence[str],
    *,
    now: datetime,
    scope: AuthScope | str | None = None,
) -> tuple[AuthTokenRecord | None, bool, bool]:
    """Revoke what expired and record the match's use.

    Returns the usable record, whether any expired credential was revoked, and
    whether a single-use credential was consumed. Every expired id is revoked,
    not only the first. A disabled match, or one missing `scope` when a scope
    is given, is refused before its use is recorded, so the refusal leaves a
    single-use token intact.
    """

    revoked_expired = any(
        [repository.revoke_if_expired(token_id, now=now) for token_id in expired_ids]
    )
    if matched is None:
        return None, revoked_expired, False
    if matched.disabled_by_admin:
        raise AuthError("token has been disabled by an administrator")
    _require_scope(matched, scope)
    if matched.reusable:
        return repository.mark_reusable_used(matched.id, now=now), revoked_expired, False
    if repository.consume_non_reusable(matched.id, now=now):
        matched.last_used_at = now
        return matched, revoked_expired, True
    return None, revoked_expired, False


def _claimed_token(
    repository: TokenRepository,
    token_id: str | None,
    raw_token: str | None,
    *,
    action: str,
) -> AuthTokenRecord:
    if token_id is None:
        raise AuthError(f"offline administrator {action} has no recoverable credential")
    record = repository.get_across_workspaces(token_id)
    if (
        record is None
        or record.kind is not TokenKind.Admin
        or record.status is not TokenStatus.Active
        or record.disabled_by_admin
    ):
        raise AuthError(f"offline administrator {action} credential is no longer active")
    if raw_token is None or not _verify_token(raw_token, record.token_hash):
        raise AuthError(
            f"offline administrator {action} already committed; retry with its staged output"
        )
    return record


def _validate_offline_request_id(request_id: str) -> None:
    if not 8 <= len(request_id) <= 128:
        raise ValueError("request ID must contain between 8 and 128 characters")
    if not all(
        character.isalnum() or character in {"-", "_", ".", ":"} for character in request_id
    ):
        raise ValueError(
            "request ID may contain only letters, numbers, hyphens, underscores, dots, and colons"
        )


def _bootstrap_token(staged_token: str | None, configured_token: str | None) -> str | None:
    if configured_token is not None:
        validated = _validate_configured_admin_token(configured_token)
        if staged_token is not None and not hmac.compare_digest(staged_token, configured_token):
            raise AuthError("configured administrator credential does not match published state")
        return validated
    return staged_token


def _validate_configured_admin_token(token: str) -> str:
    if re.fullmatch(r"rt_[A-Za-z0-9_-]{43}", token) is None:
        raise AuthError("configured administrator credential is invalid")
    return token


def try_uuid(value: str) -> str | None:
    try:
        return str(UUID(str(value)))
    except ValueError:
        return None
