from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from coordination.redis_client import RedisClient
from database.repositories.identity import UserIdentityRepository, UserRepository
from pydantic import Field as PydanticField
from pydantic import ValidationError
from shared.contracts import ContractModel
from shared.errors import ConflictError
from shared.external_identity import ExternalIdentityProfile, ExternalIdentityProvider
from shared.identity import (
    AuthTokenRecord,
    TokenKind,
    UserRecord,
    UserStatus,
    WorkspaceRecord,
)
from shared.timestamps import utc_now

from identity.auth import AuthError, IdentityContext, TokenIssuer

SESSION_TTL_SECONDS = 12 * 60 * 60

SIGN_IN_STATE_TTL_SECONDS = 600
"""Long enough to read GitHub's consent screen and decide."""

SIGN_IN_EXCHANGE_TTL_SECONDS = 120
"""Long enough for a browser to follow one redirect, and no longer."""

_SIGN_IN_STATE_PREFIX = "sis_"
_SIGN_IN_EXCHANGE_PREFIX = "sic_"
_SIGN_IN_NONCE_PREFIX = "sin_"
_SIGN_IN_STATE_KEY_NAMESPACE = "identity:sign-in-state"
_SIGN_IN_EXCHANGE_KEY_NAMESPACE = "identity:sign-in-exchange"


class SignInStateStoreError(RuntimeError):
    pass


class _SignInStatePayload(ContractModel):
    code_verifier: str = PydanticField(repr=False)
    nonce_hash: str = ""
    return_to: str = ""


class _SignInExchangePayload(ContractModel):
    user_id: str
    nonce_hash: str = ""
    return_to: str = ""


@dataclass(frozen=True, slots=True)
class SignInStart:
    authorize_url: str
    nonce: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    token: str = field(repr=False)
    record: AuthTokenRecord
    user: UserRecord
    expires_at: datetime
    return_to: str = ""


@dataclass(slots=True)
class SignInService:
    """Signing a person in through an external identity provider.

    Nothing here ever puts a credential in a URL. The callback mints a single-use
    exchange code and the session is minted only when that code is redeemed, so an
    abandoned tab leaves no live session behind and Redis never holds something
    that authenticates on its own.
    """

    context: IdentityContext
    redis: RedisClient
    provider_factory: Callable[[], ExternalIdentityProvider]
    provision_default_workspace: Callable[[str, str], WorkspaceRecord]
    ttl_seconds: int = SESSION_TTL_SECONDS

    def start(self, *, return_to: str = "") -> SignInStart:
        """Begin a sign-in, returning where to send the browser and the nonce to keep.

        The state secret is not returned: it is already inside the authorize URL, and
        handing the caller a second copy only creates another place it can be logged.
        """
        verifier = _random_secret()
        nonce = f"{_SIGN_IN_NONCE_PREFIX}{_random_secret()}"
        payload = _SignInStatePayload(
            code_verifier=verifier,
            nonce_hash=_digest(nonce),
            return_to=return_to,
        )
        state = self._store(
            _SIGN_IN_STATE_KEY_NAMESPACE,
            _SIGN_IN_STATE_PREFIX,
            payload.model_dump_json(),
            ttl_seconds=SIGN_IN_STATE_TTL_SECONDS,
        )
        authorize_url = self._provider().authorize_url(
            state=state,
            code_challenge=_code_challenge(verifier),
        )
        return SignInStart(authorize_url=authorize_url, nonce=nonce)

    def complete(self, *, code: str, state: str) -> str:
        """Redeem the provider's authorization code and return an exchange code."""
        # GETDEL first: a bad payload, a wrong nonce, or a refused provider call all
        # burn the state permanently rather than leaving it replayable.
        encoded = self.redis.getdel(self._key(_SIGN_IN_STATE_KEY_NAMESPACE, state))
        if encoded is None:
            raise AuthError("this sign-in link has expired or was already used")
        try:
            payload = _SignInStatePayload.model_validate_json(encoded)
        except ValidationError as exc:
            raise AuthError("invalid sign-in state") from exc
        profile = self._provider().identify(code=code, code_verifier=payload.code_verifier)
        user = self._resolve_account(profile)
        # Unconditional, and outside the transaction because it reaches object storage.
        # Idempotent, so an account whose workspace creation failed once is repaired at
        # the next sign-in rather than left without one forever.
        self.provision_default_workspace(user.id, profile.login)
        return self._store(
            _SIGN_IN_EXCHANGE_KEY_NAMESPACE,
            _SIGN_IN_EXCHANGE_PREFIX,
            _SignInExchangePayload(
                user_id=user.id,
                nonce_hash=payload.nonce_hash,
                return_to=payload.return_to,
            ).model_dump_json(),
            ttl_seconds=SIGN_IN_EXCHANGE_TTL_SECONDS,
        )

    def redeem(self, *, code: str, nonce: str) -> AuthenticatedSession:
        """Trade a single-use exchange code for the session credential."""
        encoded = self.redis.getdel(self._key(_SIGN_IN_EXCHANGE_KEY_NAMESPACE, code))
        if encoded is None:
            raise AuthError("this sign-in code has expired or was already used")
        try:
            payload = _SignInExchangePayload.model_validate_json(encoded)
        except ValidationError as exc:
            raise AuthError("invalid sign-in code") from exc
        # The nonce lives in a cookie only the tab that started the flow holds, so a
        # code read out of history, a screenshot, or a referrer is not redeemable.
        if not hmac.compare_digest(_digest(nonce), payload.nonce_hash):
            raise AuthError("this sign-in code belongs to a different browser session")
        issuer = TokenIssuer(self.context)
        with self.context.database.session() as session:
            user = UserRepository(session).get(payload.user_id)
            if user is None:
                raise AuthError("this account no longer exists")
            if user.status is not UserStatus.Active:
                raise AuthError("this account is disabled")
            raw_token, record = issuer.issue_for_user(
                session,
                "session",
                kind=TokenKind.Session,
                user_id=user.id,
                expires_in_seconds=self.ttl_seconds,
                reusable=True,
            )
        issuer.committed()
        expires_at = record.expires_at or (utc_now() + timedelta(seconds=self.ttl_seconds))
        return AuthenticatedSession(
            token=raw_token,
            record=record,
            user=user,
            expires_at=expires_at,
            return_to=payload.return_to,
        )

    def _resolve_account(self, profile: ExternalIdentityProfile) -> UserRecord:
        try:
            return self._link_or_refresh(profile)
        except ConflictError:
            # Another first sign-in for the same account committed between the lock
            # and the insert. Its row is the one that exists now, so read it.
            return self._link_or_refresh(profile)

    def _link_or_refresh(self, profile: ExternalIdentityProfile) -> UserRecord:
        now = utc_now()
        with self.context.database.session() as session:
            identities = UserIdentityRepository(session)
            identities.lock_subject(provider=profile.provider, subject=profile.subject)
            identity = identities.by_subject(provider=profile.provider, subject=profile.subject)
            users = UserRepository(session)
            if identity is None:
                user = users.create(
                    display_name=profile.display_name,
                    email=profile.email,
                    avatar_url=profile.avatar_url,
                )
                identity = identities.link(
                    user_id=user.id,
                    provider=profile.provider,
                    subject=profile.subject,
                    subject_login=profile.login,
                    provider_account_created_at=profile.account_created_at,
                )
            else:
                existing = users.get(identity.user_id)
                if existing is None:
                    raise AuthError("this account no longer exists")
                if existing.status is not UserStatus.Active:
                    raise AuthError("this account is disabled")
                user = users.set_profile(
                    existing.id,
                    display_name=profile.display_name,
                    email=profile.email,
                    avatar_url=profile.avatar_url,
                )
            identities.record_authentication(
                identity.id,
                subject_login=profile.login,
                authenticated_at=now,
            )
            return user

    def _provider(self) -> ExternalIdentityProvider:
        return self.provider_factory()

    def _store(self, namespace: str, prefix: str, payload: str, *, ttl_seconds: int) -> str:
        for _attempt in range(3):
            secret = f"{prefix}{_random_secret()}"
            key = self._key(namespace, secret)
            try:
                stored = self.redis.set_single_use(key, payload, ttl_seconds=ttl_seconds)
            except Exception as exc:
                with suppress(Exception):
                    self.redis.delete(key)
                raise SignInStateStoreError("sign-in storage is temporarily unavailable") from exc
            if stored:
                return secret
        raise SignInStateStoreError("sign-in storage is temporarily unavailable")

    def _key(self, namespace: str, secret: str) -> str:
        # Redis holds the digest, so a dump of it yields nothing that can be replayed.
        return self.redis.key(namespace, _digest(secret))


def _random_secret() -> str:
    return secrets.token_urlsafe(32)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


__all__ = [
    "SESSION_TTL_SECONDS",
    "SIGN_IN_EXCHANGE_TTL_SECONDS",
    "SIGN_IN_STATE_TTL_SECONDS",
    "AuthenticatedSession",
    "SignInService",
    "SignInStart",
    "SignInStateStoreError",
]
