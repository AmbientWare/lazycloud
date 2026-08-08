from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from database.records.identity import DeviceAuthorizationRecord
from database.repositories.identity import DeviceAuthorizationRepository, UserRepository
from shared.errors import ConflictError, InvalidInputError, NotFoundError
from shared.identity import DeviceAuthorizationStatus, TokenKind, UserStatus
from shared.timestamps import utc_now

from identity.auth import IdentityContext, TokenIssuer

DEVICE_CODE_TTL_SECONDS = 900
DEVICE_CODE_POLL_INTERVAL_SECONDS = 5

# RFC 8628 recommended consonant alphabet: unambiguous to read aloud and type.
_USER_CODE_ALPHABET = "BCDFGHJKLMNPQRSTVWXZ"
_USER_CODE_GROUP_LENGTH = 4
_USER_CODE_ATTEMPTS = 5
_CLIENT_NAME_MAX_LENGTH = 120


@dataclass(frozen=True, slots=True)
class DeviceAuthorizationStart:
    device_code: str
    record: DeviceAuthorizationRecord
    expires_in_seconds: int = DEVICE_CODE_TTL_SECONDS
    poll_interval_seconds: int = DEVICE_CODE_POLL_INTERVAL_SECONDS


@dataclass(frozen=True, slots=True)
class DeviceAuthorizationClaim:
    status: DeviceAuthorizationStatus
    token: str = ""
    username: str = ""


def normalize_user_code(value: str) -> str:
    compact = "".join(char for char in value.upper() if char.isalnum())
    if len(compact) != _USER_CODE_GROUP_LENGTH * 2:
        msg = "user code must be eight letters"
        raise InvalidInputError(msg)
    return f"{compact[:_USER_CODE_GROUP_LENGTH]}-{compact[_USER_CODE_GROUP_LENGTH:]}"


def _new_user_code() -> str:
    groups = (
        "".join(secrets.choice(_USER_CODE_ALPHABET) for _ in range(_USER_CODE_GROUP_LENGTH))
        for _ in range(2)
    )
    return "-".join(groups)


def _hash_device_code(device_code: str) -> str:
    return hashlib.sha256(device_code.encode("utf-8")).hexdigest()


class DeviceAuthorizationService:
    """Device-code login: a CLI requests a code, a signed-in user approves it for
    their account, and the CLI poll exchanges the code for a user token reaching
    every workspace they belong to. Codes are short-lived, single-use, and pruned
    after expiry; the minted token is never persisted in the device row."""

    def __init__(self, context: IdentityContext) -> None:
        self.context = context

    def start(self, *, client_name: str) -> DeviceAuthorizationStart:
        name = client_name.strip()[:_CLIENT_NAME_MAX_LENGTH] or "cli"
        device_code = f"dc_{secrets.token_urlsafe(32)}"
        expires_at = utc_now() + timedelta(seconds=DEVICE_CODE_TTL_SECONDS)
        with self.context.database.session() as session:
            repository = DeviceAuthorizationRepository(session)
            for _ in range(_USER_CODE_ATTEMPTS):
                user_code = _new_user_code()
                record = repository.create_pending(
                    device_code_hash=_hash_device_code(device_code),
                    user_code=user_code,
                    client_name=name,
                    expires_at=expires_at,
                )
                if record is not None:
                    return DeviceAuthorizationStart(device_code=device_code, record=record)
        msg = "could not allocate a unique user code"
        raise ConflictError(msg)

    def get(self, user_code: str) -> DeviceAuthorizationRecord:
        current = utc_now()
        with self.context.database.session() as session:
            repository = DeviceAuthorizationRepository(session)
            record = repository.by_user_code(normalize_user_code(user_code))
            if record is None:
                msg = "device code not found"
                raise NotFoundError(msg)
            if self._expired(record, now=current):
                return record.model_copy(update={"status": DeviceAuthorizationStatus.Expired})
            return record

    def approve(self, user_code: str, *, user_id: str) -> DeviceAuthorizationRecord:
        return self._decide(
            user_code,
            status=DeviceAuthorizationStatus.Approved,
            user_id=user_id,
        )

    def deny(self, user_code: str) -> DeviceAuthorizationRecord:
        return self._decide(user_code, status=DeviceAuthorizationStatus.Denied)

    def claim(self, device_code: str) -> DeviceAuthorizationClaim:
        now = utc_now()
        issuer = TokenIssuer(self.context)
        issued = False
        with self.context.database.session() as session:
            repository = DeviceAuthorizationRepository(session)
            record = repository.by_device_code_hash(_hash_device_code(device_code))
            if record is None:
                msg = "device code not found"
                raise NotFoundError(msg)
            if self._expired(record, now=now):
                repository.delete_if_expired(record, expired_at=now)
                return DeviceAuthorizationClaim(status=DeviceAuthorizationStatus.Expired)
            if record.status is DeviceAuthorizationStatus.Pending:
                return DeviceAuthorizationClaim(status=DeviceAuthorizationStatus.Pending)
            if record.status is DeviceAuthorizationStatus.Expired:
                msg = "device code has expired"
                raise ConflictError(msg)
            if record.consumed_at is not None:
                msg = "device code was already consumed"
                raise ConflictError(msg)
            if record.user_id is None and record.status is DeviceAuthorizationStatus.Approved:
                msg = "approved device code is missing a user"
                raise ConflictError(msg)
            consumed = repository.consume_decided(record, consumed_at=now)
            if consumed is None:
                msg = "device code was already consumed or changed state"
                raise ConflictError(msg)
            if consumed.status is DeviceAuthorizationStatus.Denied:
                return DeviceAuthorizationClaim(status=DeviceAuthorizationStatus.Denied)
            if consumed.user_id is None:
                msg = "approved device code is missing a user"
                raise ConflictError(msg)
            user = UserRepository(session).get(consumed.user_id)
            if user is None or user.status is not UserStatus.Active:
                msg = "the approving account is no longer active"
                raise ConflictError(msg)
            raw_token, _ = issuer.issue_for_user(
                session,
                consumed.client_name,
                kind=TokenKind.User,
                user_id=user.id,
                reusable=True,
            )
            issued = True
        if issued:
            issuer.committed()
        return DeviceAuthorizationClaim(
            status=DeviceAuthorizationStatus.Approved,
            token=raw_token,
            username=user.username,
        )

    def prune_expired(self, *, now: datetime | None = None) -> int:
        current = now or utc_now()
        with self.context.database.session() as session:
            return DeviceAuthorizationRepository(session).prune_expired(now=current)

    def _decide(
        self,
        user_code: str,
        *,
        status: DeviceAuthorizationStatus,
        user_id: str | None = None,
    ) -> DeviceAuthorizationRecord:
        current = utc_now()
        with self.context.database.session() as session:
            repository = DeviceAuthorizationRepository(session)
            record = repository.by_user_code(normalize_user_code(user_code))
            if record is None:
                msg = "device code not found"
                raise NotFoundError(msg)
            if self._expired(record, now=current):
                repository.delete_if_expired(record, expired_at=current)
                msg = "device code has expired"
                raise ConflictError(msg)
            updated = repository.decide_pending(
                record,
                status=status,
                user_id=user_id,
                decided_at=current,
            )
            if updated is None:
                msg = f"device code was already {record.status.value}"
                raise ConflictError(msg)
            return updated

    @staticmethod
    def _expired(record: DeviceAuthorizationRecord, *, now: datetime | None = None) -> bool:
        return record.expires_at <= (now or utc_now())


__all__ = [
    "DEVICE_CODE_POLL_INTERVAL_SECONDS",
    "DEVICE_CODE_TTL_SECONDS",
    "DeviceAuthorizationClaim",
    "DeviceAuthorizationService",
    "DeviceAuthorizationStart",
    "normalize_user_code",
]
