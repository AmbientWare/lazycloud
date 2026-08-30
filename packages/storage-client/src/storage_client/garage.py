from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.app_identity import ENV_PREFIX
from shared.identity import WorkspaceStorageConfig
from shared.timestamps import utc_now
from shared.workspace_storage import WorkspaceStorageGrant

LOGGER = logging.getLogger(__name__)

DEFAULT_GARAGE_CREDENTIAL_LIFETIME_SECONDS = 3600

_ACCESS_KEY_FIELD = "garage_access_key"
_SECRET_KEY_FIELD = "garage_secret_key"
_ISSUED_AT_FIELD = "garage_issued_at"
_PREVIOUS_ACCESS_KEY_FIELD = "garage_previous_access_key"
_PREVIOUS_EXPIRES_AT_FIELD = "garage_previous_expires_at"


class WorkspaceStorageStateStore(Protocol):
    """Persists what an issuer minted, so the next vend finds the same key.

    Narrow on purpose: an issuer decides credentials and must not otherwise reach
    into workspace records.
    """

    def save(self, *, workspace_id: str, fields: dict[str, str]) -> None: ...


class GarageAdminError(RuntimeError):
    """The Garage admin API refused or could not answer a request."""


class _GarageCreatedKey(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    access_key_id: str = Field(alias="accessKeyId", min_length=1)
    secret_access_key: str = Field(alias="secretAccessKey", min_length=1)


class _GarageLocalAlias(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    alias: str = Field(min_length=1)


class _GarageBucket(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    id: str = Field(min_length=1)
    global_aliases: tuple[str, ...] = Field(default=(), alias="globalAliases")
    local_aliases: tuple[_GarageLocalAlias, ...] = Field(default=(), alias="localAliases")


_GARAGE_BUCKETS = TypeAdapter(tuple[_GarageBucket, ...])


class GarageAdminSettings(BaseSettings):
    """How to reach the Garage admin API, which is not the S3 endpoint.

    Absent means this deployment does not use Garage, so the fields carry no
    local-looking default: a control plane that pointed at `localhost` here would
    mint credentials against whatever was listening.
    """

    endpoint_url: str = ""
    token: str = Field(default="", repr=False)
    credential_lifetime_seconds: int = Field(
        default=DEFAULT_GARAGE_CREDENTIAL_LIFETIME_SECONDS,
        gt=0,
    )

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_GARAGE_ADMIN_",
        extra="ignore",
    )


@dataclass(frozen=True, slots=True)
class GarageAdminClient:
    """The subset of Garage's admin API that grants a key on one bucket.

    Endpoints are POST under `/v2/` and authenticate with a bearer token. The
    secret is returned only when a key is created, so a caller that does not
    capture it there can never read it back.
    """

    endpoint_url: str
    token: str
    timeout_seconds: float = 20.0

    def create_key(self, name: str) -> tuple[str, str]:
        try:
            created = _GarageCreatedKey.model_validate(self._call("/v2/CreateKey", {"name": name}))
        except ValidationError as exc:
            raise GarageAdminError(f"Garage returned no key material for {name!r}") from exc
        return (created.access_key_id, created.secret_access_key)

    def delete_key(self, access_key: str) -> None:
        self._call(f"/v2/DeleteKey?id={access_key}", body=None)

    def bucket_id(self, alias: str) -> str:
        """Resolve a bucket by the name the platform knows it as.

        Both alias kinds are searched because the two ways a bucket comes to
        exist produce different ones: `--default-bucket` makes a global alias,
        while creating through the S3 API makes an alias local to the key that
        created it. A workspace bucket is made the second way.
        """
        try:
            buckets = _GARAGE_BUCKETS.validate_python(
                self._call("/v2/ListBuckets", body=None, method="GET")
            )
        except ValidationError as exc:
            raise GarageAdminError("Garage returned an invalid bucket list") from exc
        for bucket in buckets:
            if alias in bucket.global_aliases:
                return bucket.id
            if any(entry.alias == alias for entry in bucket.local_aliases):
                return bucket.id
        raise GarageAdminError(f"Garage holds no bucket aliased {alias!r}")

    def ensure_global_alias(self, *, bucket_id: str, alias: str) -> None:
        """Make the bucket addressable by name to every key granted on it.

        A local alias belongs to the key that created it, so a freshly minted key
        holding permission on the bucket still could not name it in an S3
        request. The name is already unique to one workspace, so promoting it to
        a global alias collides with nothing.
        """
        try:
            info = _GarageBucket.model_validate(
                self._call(f"/v2/GetBucketInfo?id={bucket_id}", body=None, method="GET")
            )
        except ValidationError as exc:
            raise GarageAdminError(
                f"Garage returned invalid bucket information for {bucket_id}"
            ) from exc
        if alias in info.global_aliases:
            return
        self._call("/v2/AddBucketAlias", {"bucketId": bucket_id, "globalAlias": alias})

    def allow_bucket_key(self, *, bucket_id: str, access_key: str) -> None:
        self._call(
            "/v2/AllowBucketKey",
            {
                "bucketId": bucket_id,
                "accessKeyId": access_key,
                # Never `owner`: that would let a worker rewrite the bucket's own
                # permissions, which is the boundary this exists to hold.
                "permissions": {"read": True, "write": True, "owner": False},
            },
        )

    def deny_bucket_key(self, *, bucket_id: str, access_key: str) -> None:
        self._call(
            "/v2/DenyBucketKey",
            {
                "bucketId": bucket_id,
                "accessKeyId": access_key,
                "permissions": {"read": True, "write": True, "owner": True},
            },
        )

    def _call(
        self,
        path: str,
        body: JsonValue = None,
        *,
        method: str = "POST",
    ) -> object:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            self.endpoint_url.rstrip("/") + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:200]
            raise GarageAdminError(f"Garage admin {path} failed: {exc.code} {detail}") from exc
        except OSError as exc:
            raise GarageAdminError(f"Garage admin {path} is unreachable: {exc}") from exc
        if not raw:
            return {}
        try:
            decoded: object = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GarageAdminError(f"Garage admin {path} returned invalid JSON") from exc
        return decoded


@dataclass(frozen=True, slots=True)
class GarageWorkspaceStorageIssuer:
    """Grants a workspace a key on its own bucket, and rotates it.

    Garage has no STS, so the credential is a real key rather than a session, and
    the expiry is this issuer's own decision rather than the store's. Rotating to
    that schedule is what keeps the refresh path identical to the one AWS forces:
    a deployment where credentials never aged would never exercise it.

    Two generations at most. The replacement is granted before the credential it
    replaces is revoked, because a mount reads its credential lazily and one cut
    mid-write loses the write.
    """

    admin: GarageAdminClient
    state: WorkspaceStorageStateStore
    lifetime_seconds: int = DEFAULT_GARAGE_CREDENTIAL_LIFETIME_SECONDS

    def issue(
        self,
        *,
        workspace_id: str,
        storage: WorkspaceStorageConfig,
    ) -> WorkspaceStorageGrant:
        bucket = storage.bucket or ""
        if not bucket:
            raise GarageAdminError(f"workspace {workspace_id!r} has no bucket to grant")
        now = utc_now()
        fields = _credential_fields(storage)
        if fields is None:
            # First ask for this workspace: mint through the same path that
            # replaces an aged key, so there is one way a key comes to exist.
            access_key, secret_key, expires_at = self._rotate(
                workspace_id=workspace_id,
                storage=storage,
                bucket=bucket,
                now=now,
            )
            return self._grant_for(storage, bucket, access_key, secret_key, expires_at)
        access_key, secret_key, issued_at = fields
        expires_at = issued_at + timedelta(seconds=self.lifetime_seconds)
        if now >= expires_at - timedelta(seconds=self.lifetime_seconds // 2):
            access_key, secret_key, expires_at = self._rotate(
                workspace_id=workspace_id,
                storage=storage,
                bucket=bucket,
                now=now,
            )
        return self._grant_for(storage, bucket, access_key, secret_key, expires_at)

    def _grant_for(
        self,
        storage: WorkspaceStorageConfig,
        bucket: str,
        access_key: str,
        secret_key: str,
        expires_at: datetime,
    ) -> WorkspaceStorageGrant:
        return WorkspaceStorageGrant(
            endpoint_url=storage.endpoint_url,
            region=storage.region,
            bucket_name=bucket,
            prefix=storage.key_prefix,
            force_path_style=storage.force_path_style,
            access_key=access_key,
            secret_key=secret_key,
            expires_at=expires_at,
        )

    def _rotate(
        self,
        *,
        workspace_id: str,
        storage: WorkspaceStorageConfig,
        bucket: str,
        now: datetime,
    ) -> tuple[str, str, datetime]:
        """Mint the next generation, and retire the one before last.

        The outgoing key is kept until its own lifetime lapses rather than revoked
        here, because a mount reads its credential lazily: revoking on rotation
        would cut whichever mount had not yet re-read the file.
        """
        self._retire_expired_previous(storage, bucket=bucket, now=now)
        access_key, secret_key = self._grant(workspace_id=workspace_id, bucket=bucket)
        outgoing = _config_text(storage, _ACCESS_KEY_FIELD)
        rotated = {
            _ACCESS_KEY_FIELD: access_key,
            _SECRET_KEY_FIELD: secret_key,
            _ISSUED_AT_FIELD: now.isoformat(),
        }
        if outgoing:
            rotated[_PREVIOUS_ACCESS_KEY_FIELD] = outgoing
            rotated[_PREVIOUS_EXPIRES_AT_FIELD] = (
                now + timedelta(seconds=self.lifetime_seconds)
            ).isoformat()
        self.state.save(workspace_id=workspace_id, fields=rotated)
        return (access_key, secret_key, now + timedelta(seconds=self.lifetime_seconds))

    def _grant(self, *, workspace_id: str, bucket: str) -> tuple[str, str]:
        bucket_id = self.admin.bucket_id(bucket)
        self.admin.ensure_global_alias(bucket_id=bucket_id, alias=bucket)
        access_key, secret_key = self.admin.create_key(f"workspace-{workspace_id}")
        try:
            self.admin.allow_bucket_key(bucket_id=bucket_id, access_key=access_key)
        except Exception:
            # A key that was created but never granted opens nothing and would
            # otherwise accumulate, one per failed attempt, with no record naming
            # it as ours.
            with _suppressed("Garage key could not be removed after a failed grant"):
                self.admin.delete_key(access_key)
            raise
        return (access_key, secret_key)

    def _retire_expired_previous(
        self,
        storage: WorkspaceStorageConfig,
        *,
        bucket: str,
        now: datetime,
    ) -> None:
        previous = _config_text(storage, _PREVIOUS_ACCESS_KEY_FIELD)
        expires_at = _config_time(storage, _PREVIOUS_EXPIRES_AT_FIELD)
        if not previous or expires_at is None or now < expires_at:
            return
        bucket_id = self.admin.bucket_id(bucket)
        with _suppressed(f"Garage key {previous} could not be retired"):
            self.admin.deny_bucket_key(bucket_id=bucket_id, access_key=previous)
            self.admin.delete_key(previous)


class _suppressed:
    """Log what went wrong and carry on, for cleanup that must not fail a caller."""

    def __init__(self, message: str) -> None:
        self._message = message

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        if exc_type is None:
            return False
        LOGGER.warning(self._message, exc_info=True)
        return True


def _credential_fields(
    storage: WorkspaceStorageConfig,
) -> tuple[str, str, datetime] | None:
    access_key = _config_text(storage, _ACCESS_KEY_FIELD)
    secret_key = _config_text(storage, _SECRET_KEY_FIELD)
    issued_at = _config_time(storage, _ISSUED_AT_FIELD)
    if not access_key or not secret_key or issued_at is None:
        return None
    return (access_key, secret_key, issued_at)


def _config_text(storage: WorkspaceStorageConfig, field: str) -> str:
    value = storage.config.get(field)
    return value if isinstance(value, str) else ""


def _config_time(storage: WorkspaceStorageConfig, field: str) -> datetime | None:
    raw = _config_text(storage, field)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


__all__ = [
    "DEFAULT_GARAGE_CREDENTIAL_LIFETIME_SECONDS",
    "GarageAdminClient",
    "GarageAdminError",
    "GarageAdminSettings",
    "GarageWorkspaceStorageIssuer",
]
