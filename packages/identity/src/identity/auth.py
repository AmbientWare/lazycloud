from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from time import monotonic
from typing import Protocol
from uuid import UUID

from database.repositories.identity import (
    IdentityAdminRecoveryRequestRepository,
    IdentityBootstrapClaimRepository,
    TokenRepository,
    UserRepository,
    WorkspaceAuditRepository,
    WorkspaceMemberRepository,
    WorkspaceRepository,
)
from database.types import DatabaseSession
from shared.errors import ConflictError, NotFoundError
from shared.http.workspaces import WorkspaceAuditAction, WorkspaceAuditTarget
from shared.identity import (
    SYSTEM_TOKEN_KINDS,
    USER_PRINCIPAL_TOKEN_KINDS,
    AuthScope,
    AuthTokenRecord,
    PlatformRole,
    TokenKind,
    TokenStatus,
    UserRecord,
    UserStatus,
    WorkspaceRecord,
    WorkspaceRole,
    WorkspaceStatus,
)
from shared.timestamps import utc_now

from database import DatabaseClient
from identity.authz import (
    AuthzDecision,
    AuthzRequirement,
    decide_authorization,
    token_has_scope,
)
from identity.passwords import hash_password, validate_password, validate_username
from identity.token_invalidation import (
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
    username: str = ""
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
        audit_actor: AuthTokenRecord | None = None,
    ) -> tuple[str, AuthTokenRecord]:
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
            audit_actor=audit_actor,
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
        audit_actor: AuthTokenRecord | None = None,
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
            _lock_active_user_for_issue(session, user_id)
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
        if audit_actor is not None and owner_workspace_id:
            WorkspaceAuditRepository(session).append(
                workspace_id=owner_workspace_id,
                action=WorkspaceAuditAction.TokenCreated,
                actor=audit_actor,
                target_type=WorkspaceAuditTarget.Token,
                target_id=record.id,
                target_name=record.name,
                summary=f"Created access token {record.name}",
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
    username: str,
    password: str,
) -> UserRecord:
    normalized = validate_username(username)
    repository = UserRepository(session)
    if repository.by_username(normalized) is not None:
        raise ConflictError(f"username is already in use: {normalized}")
    return repository.create(
        username=normalized,
        password_hash=hash_password(validate_password(password)),
        role=PlatformRole.Administrator,
    )


def _recovered_administrator(
    session: DatabaseSession,
    *,
    username: str,
    password: str | None,
) -> UserRecord:
    normalized = validate_username(username)
    repository = UserRepository(session)
    user = repository.by_username(normalized)
    if user is None:
        raise NotFoundError(f"user not found: {normalized}")
    if user.status is not UserStatus.Active:
        user = repository.set_status(user.id, status=UserStatus.Active)
    if user.role is not PlatformRole.Administrator:
        user = repository.set_role(user.id, role=PlatformRole.Administrator)
    if password is not None:
        user = repository.set_password(
            user.id,
            password_hash=hash_password(validate_password(password)),
        )
        TokenRepository(session).revoke_user_sessions(user.id, now=utc_now())
    return user


def _lock_active_user_for_issue(session: DatabaseSession, user_id: str) -> None:
    user = UserRepository(session).get(user_id)
    if user is None or user.status is not UserStatus.Active:
        raise NotFoundError(f"user not found: {user_id}")


def _hash_token(token: str, salt: str | None = None) -> str:
    token_salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        token.encode("utf-8"),
        token_salt.encode("utf-8"),
        200_000,
    ).hex()
    return f"pbkdf2_sha256${token_salt}${digest}"


def _verify_token(token: str, encoded: str) -> bool:
    _, salt, expected = encoded.split("$", 2)
    actual = _hash_token(token, salt).split("$", 2)[2]
    return hmac.compare_digest(actual, expected)


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


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

        Called after the revoking transaction commits—workspace deletion, a password
        change, a disabled account—so a cached positive cannot outlive it.
        """
        self._invalidate_token_caches()

    def platform_role(self, token: AuthTokenRecord) -> PlatformRole:
        """The one answer to whether a caller administers the platform.

        Two things confer it: the administrator token kind, which platform-minted
        operator credentials still carry, and an account whose role says so. Every
        caller—the authorization decision and the routes that branch on it—asks here,
        so the two cannot drift into disagreeing.
        """
        if token.kind is TokenKind.Admin:
            return PlatformRole.Administrator
        if not token.names_user or not token.user_id:
            return PlatformRole.Member
        with self.context.database.session() as session:
            user = UserRepository(session).get(token.user_id)
        if user is None or user.status is not UserStatus.Active:
            return PlatformRole.Member
        return user.role

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
        audit_actor: AuthTokenRecord | None = None,
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
                audit_actor=audit_actor,
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
        username: str,
        password: str,
        name: str = "first-admin",
        workspace: str = "default",
        staged_token: str | None = None,
        configured_token: str | None = None,
        stage_token: Callable[[str], None] | None = None,
    ) -> BootstrapAdminToken:
        """Create the first administrator: the person, their workspace, and a credential.

        The person is what the bootstrap produces. A token alone could not be signed in
        with, and every later credential is minted against an account, so the account
        has to exist before anything else can own something.
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
                username=username,
                password=password,
            )
            workspace_record = WorkspaceRepository(session).ensure_named(workspace)
            WorkspaceMemberRepository(session).add(
                workspace_id=workspace_record.id,
                user_id=administrator.id,
                role=WorkspaceRole.Owner,
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
            username=administrator.username,
        )

    def recover_admin_token(
        self,
        *,
        request_id: str,
        username: str,
        password: str | None = None,
        name: str = "recovery-admin",
        workspace: str = "default",
        staged_token: str | None = None,
        stage_token: Callable[[str], None] | None = None,
    ) -> BootstrapAdminToken:
        """Re-establish administrator access offline for an existing account.

        Recovery re-keys a person rather than minting a free-floating credential: the
        account is what owns the workspaces and the connected compute, so an operator
        who has lost access needs that account back, not a second identity beside it.
        Supplying ``password`` also resets it, which is the usual case when the reason
        for recovering is that nobody can sign in.
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
                username=username,
                password=password,
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
        return BootstrapAdminToken(
            token=raw_token,
            record=record,
            request_id=request_id,
            username=administrator.username,
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
    ) -> AuthTokenRecord:
        if not self.administrator_ready():
            raise AuthError("offline administrator bootstrap must complete first")
        record = self.authenticate(token)
        normalized_workspace_id = self._workspace_id(workspace_id)
        if (
            record.name != name
            or record.kind is not kind
            or record.workspace_id != normalized_workspace_id
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

    def toggle_token(self, token_id_or_name: str) -> AuthTokenRecord:
        record = self._find_token(token_id_or_name)
        if record.status is TokenStatus.Active:
            with self.context.database.session() as session:
                updated = TokenRepository(session).revoke_across_workspaces(
                    record.id,
                    now=utc_now(),
                )
            if updated is None:
                raise KeyError(f"token not found: {token_id_or_name}")
        else:
            with self.context.database.session() as session:
                updated = TokenRepository(session).activate(
                    record.id,
                    workspace_id=record.workspace_id,
                    now=utc_now(),
                )
            if updated is None:
                raise ConflictError("expired or consumed token cannot be reactivated")
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
        audit_actor: AuthTokenRecord | None = None,
    ) -> tuple[str, AuthTokenRecord]:
        workspace_id = self._workspace_id(workspace_id_or_name)
        return self.create_token(
            name or f"{workspace_id}-{TokenKind(kind).value}",
            kind=kind,
            workspace_id=workspace_id,
            scopes=scopes,
            reusable=reusable,
            audit_actor=audit_actor,
        )

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

    def toggle_workspace_token(
        self,
        workspace_id_or_name: str,
        token_id_or_name: str,
        *,
        audit_actor: AuthTokenRecord | None = None,
    ) -> AuthTokenRecord:
        record = self._find_workspace_token(workspace_id_or_name, token_id_or_name)
        if record.status is TokenStatus.Active:
            with self.context.database.session() as session:
                updated = TokenRepository(session).revoke(
                    record.id,
                    workspace_id=record.workspace_id,
                    now=utc_now(),
                )
                if updated is None:
                    raise NotFoundError(f"workspace token not found: {token_id_or_name}")
                if audit_actor is not None:
                    WorkspaceAuditRepository(session).append(
                        workspace_id=updated.workspace_id,
                        action=WorkspaceAuditAction.TokenDisabled,
                        actor=audit_actor,
                        target_type=WorkspaceAuditTarget.Token,
                        target_id=updated.id,
                        target_name=updated.name,
                        summary=f"Disabled access token {updated.name}",
                        previous_value=TokenStatus.Active.value,
                        new_value=updated.status.value,
                    )
        else:
            with self.context.database.session() as session:
                updated = TokenRepository(session).activate(
                    record.id,
                    workspace_id=record.workspace_id,
                    now=utc_now(),
                )
                if updated is None:
                    raise ConflictError("expired or consumed token cannot be reactivated")
                if audit_actor is not None:
                    WorkspaceAuditRepository(session).append(
                        workspace_id=updated.workspace_id,
                        action=WorkspaceAuditAction.TokenEnabled,
                        actor=audit_actor,
                        target_type=WorkspaceAuditTarget.Token,
                        target_id=updated.id,
                        target_name=updated.name,
                        summary=f"Enabled access token {updated.name}",
                        previous_value=TokenStatus.Revoked.value,
                        new_value=updated.status.value,
                    )
        self._invalidate_token_caches()
        return updated

    def revoke_workspace_token(
        self,
        workspace_id_or_name: str,
        token_id_or_name: str,
        *,
        audit_actor: AuthTokenRecord | None = None,
    ) -> AuthTokenRecord:
        record = self._find_workspace_token(workspace_id_or_name, token_id_or_name)
        previous = record.status
        with self.context.database.session() as session:
            updated = TokenRepository(session).revoke(
                record.id,
                workspace_id=record.workspace_id,
                now=utc_now(),
            )
            if updated is None:
                raise NotFoundError(f"workspace token not found: {token_id_or_name}")
            if audit_actor is not None and previous is not TokenStatus.Revoked:
                WorkspaceAuditRepository(session).append(
                    workspace_id=updated.workspace_id,
                    action=WorkspaceAuditAction.TokenDisabled,
                    actor=audit_actor,
                    target_type=WorkspaceAuditTarget.Token,
                    target_id=updated.id,
                    target_name=updated.name,
                    summary=f"Disabled access token {updated.name}",
                    previous_value=previous.value,
                    new_value=updated.status.value,
                )
        self._invalidate_token_caches()
        return updated

    def delete_workspace_token(
        self,
        workspace_id_or_name: str,
        token_id_or_name: str,
        *,
        audit_actor: AuthTokenRecord | None = None,
    ) -> AuthTokenRecord:
        record = self._find_workspace_token(workspace_id_or_name, token_id_or_name)
        with self.context.database.session() as session:
            if audit_actor is not None:
                WorkspaceAuditRepository(session).append(
                    workspace_id=record.workspace_id,
                    action=WorkspaceAuditAction.TokenDeleted,
                    actor=audit_actor,
                    target_type=WorkspaceAuditTarget.Token,
                    target_id=record.id,
                    target_name=record.name,
                    summary=f"Deleted access token {record.name}",
                )
            TokenRepository(session).delete(record.id, workspace_id=record.workspace_id)
        record.status = TokenStatus.Revoked
        record.revoked_at = utc_now()
        self._invalidate_token_caches()
        return record

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
                for record in repository.list_by_prefix(token[:10]):
                    if record.status != TokenStatus.Active:
                        continue
                    if record.expires_at is not None and record.expires_at <= now:
                        revoked_expired = (
                            repository.revoke_if_expired(record.id, now=now) or revoked_expired
                        )
                        continue
                    if not _verify_token(token, record.token_hash):
                        continue
                    if record.disabled_by_admin:
                        msg = "token has been disabled by an administrator"
                        raise AuthError(msg)
                    _require_scope(record, scope)
                    if record.reusable:
                        updated = repository.mark_reusable_used(record.id, now=now)
                    elif repository.consume_non_reusable(record.id, now=now):
                        record.last_used_at = now
                        updated = record
                        consumed = True
                    break
        finally:
            if revoked_expired or consumed:
                self._invalidate_token_caches()
        if updated is not None:
            if updated.reusable and cache_usable and not revoked_expired:
                self.token_cache.store(token_digest, updated, generation=generation)
            return updated
        msg = "invalid token"
        raise AuthError(msg)

    def authenticate_header(
        self,
        authorization: str | None,
        *,
        scope: AuthScope | str | None = None,
        allow_if_no_tokens: bool = False,
    ) -> AuthTokenRecord | None:
        if allow_if_no_tokens and not self.list_tokens():
            return None
        if authorization is None or not authorization.startswith("Bearer "):
            msg = "missing bearer token"
            raise AuthError(msg)
        return self.authenticate(authorization.removeprefix("Bearer ").strip(), scope=scope)

    def authorize_header(
        self,
        authorization: str | None,
        requirement: AuthzRequirement,
        *,
        allow_if_no_tokens: bool = False,
    ) -> AuthTokenRecord | None:
        token = self.authenticate_header(
            authorization,
            allow_if_no_tokens=allow_if_no_tokens,
        )
        if token is None:
            return None
        decision = decide_authorization(
            token,
            requirement,
            platform_role=self.platform_role(token),
        )
        if not decision.allowed:
            raise AuthorizationDeniedError(decision.message)
        return token

    def authorize_token_identity(
        self,
        token_id: str,
        *,
        token_user_id: str = "",
        token_workspace_id: str = "",
        requirement: AuthzRequirement,
    ) -> AuthTokenRecord:
        """Reload and authorize a previously authenticated identity from PostgreSQL.

        Short-lived credential exchanges call this after consuming their one-use
        coordination state. Deliberately bypassing the bearer-token cache ensures
        revocation, expiry, deletion, and scope changes take effect immediately.
        """

        now = utc_now()
        expired = False
        updated: AuthTokenRecord | None = None
        with self.context.database.session() as session:
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
                expired = True
            elif record.disabled_by_admin:
                raise AuthError("token has been disabled by an administrator")
            else:
                updated = repository.mark_reusable_used(record.id, now=now)
        if expired:
            self._invalidate_token_caches()
            raise AuthError("token has expired")
        if updated is None:
            raise AuthError("invalid token identity")
        decision = decide_authorization(
            updated,
            requirement,
            platform_role=self.platform_role(updated),
        )
        if not decision.allowed:
            raise AuthorizationDeniedError(decision.message)
        return updated

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
