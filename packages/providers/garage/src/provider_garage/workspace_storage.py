from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.identity import WorkspaceStorageConfig
from shared.timestamps import utc_now
from shared.workspace_storage import WorkspaceStorageGrant
from storage_client.s3 import S3ObjectStoreClient, S3ObjectStoreSettings


class GarageSettings(BaseSettings):
    admin_endpoint_url: str = Field(default="", min_length=1)
    admin_token: SecretStr = Field(default=SecretStr(""), min_length=1)

    model_config = SettingsConfigDict(
        env_prefix="LAZYCLOUD_GARAGE_", extra="ignore", hide_input_in_errors=True
    )


class _Response(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)


class _BucketKey(_Response):
    access_key_id: str = Field(alias="accessKeyId", repr=False)
    name: str


class _Bucket(_Response):
    id: str
    keys: list[_BucketKey]


class _KeyBucket(_Response):
    id: str
    local_aliases: list[str] = Field(alias="localAliases")
    global_aliases: list[str] = Field(alias="globalAliases")


class _Key(_Response):
    access_key_id: str = Field(alias="accessKeyId", repr=False)
    secret_access_key: SecretStr | None = Field(default=None, alias="secretAccessKey")
    name: str
    expiration: AwareDatetime | None
    expired: bool
    buckets: list[_KeyBucket]


def _request(
    client: httpx.Client,
    method: str,
    operation: str,
    *,
    params: dict[str, str] | None = None,
    body: dict[str, str | dict[str, bool]] | None = None,
) -> httpx.Response:
    try:
        response = client.request(method, f"/v2/{operation}", params=params, json=body)
    except httpx.TransportError:
        raise RuntimeError(f"Garage {operation} could not reach the Admin API") from None
    if response.status_code == 404 and operation in {"GetKeyInfo", "DeleteKey", "GetBucketInfo"}:
        return response
    if response.is_error:
        raise RuntimeError(f"Garage {operation} failed with HTTP {response.status_code}")
    return response


@dataclass(frozen=True, slots=True)
class GarageWorkspaceStorageIssuer:
    settings: S3ObjectStoreSettings
    admin: GarageSettings
    credential_lifetime: timedelta = timedelta(minutes=15)

    def _bucket(self, workspace_id: str, storage: WorkspaceStorageConfig) -> str:
        if storage.access_key or storage.secret_key:
            raise ValueError("managed workspace storage cannot use customer credentials")
        expected_bucket = f"{self.settings.workspace_bucket_prefix}-{workspace_id}".replace(
            "_", "-"
        )
        if storage.bucket != expected_bucket or storage.endpoint_url != self.settings.endpoint_url:
            raise ValueError("workspace storage does not name its deployment-owned Garage bucket")
        if storage.key_prefix:
            raise ValueError("a workspace Garage bucket cannot carry an alternate storage prefix")
        return expected_bucket

    def _lookup_bucket(self, client: httpx.Client, name: str) -> _Bucket | None:
        parent = _Key.model_validate_json(
            _request(
                client, "GET", "GetKeyInfo", params={"id": self.settings.access_key_id}
            ).content
        )
        owned_buckets = [
            bucket
            for bucket in parent.buckets
            if name in bucket.local_aliases or name in bucket.global_aliases
        ]
        if not owned_buckets:
            return None
        if len(owned_buckets) != 1:
            raise ValueError("Garage platform key does not identify exactly one workspace bucket")
        response = _request(client, "GET", "GetBucketInfo", params={"id": owned_buckets[0].id})
        if response.status_code == 404:
            return None
        return _Bucket.model_validate_json(response.content)

    def _workspace_key(
        self, client: httpx.Client, candidate: _BucketKey, *, bucket_id: str, name: str
    ) -> _Key | None:
        if candidate.name != name or candidate.access_key_id == self.settings.access_key_id:
            return None
        response = _request(client, "GET", "GetKeyInfo", params={"id": candidate.access_key_id})
        if response.status_code == 404:
            return None
        key = _Key.model_validate_json(response.content)
        if (
            key.access_key_id != candidate.access_key_id
            or key.name != name
            or {bucket.id for bucket in key.buckets} != {bucket_id}
        ):
            return None
        return key

    def issue(self, *, workspace_id: str, storage: WorkspaceStorageConfig) -> WorkspaceStorageGrant:
        expected_bucket = self._bucket(workspace_id, storage)
        if self.credential_lifetime <= timedelta(0):
            raise ValueError("workspace credentials require a positive lifetime")
        name = f"lazycloud-workspace:{expected_bucket}"
        expiration = (utc_now() + self.credential_lifetime).replace(microsecond=0)
        with httpx.Client(
            base_url=self.admin.admin_endpoint_url,
            headers={"Authorization": f"Bearer {self.admin.admin_token.get_secret_value()}"},
            timeout=15,
        ) as client:
            bucket = self._lookup_bucket(client, expected_bucket)
            if bucket is None:
                raise ValueError(
                    "Garage platform key does not identify exactly one workspace bucket"
                )
            for candidate in bucket.keys:
                key = self._workspace_key(client, candidate, bucket_id=bucket.id, name=name)
                # Expiration is enforced by Garage. Cleanup only removes spent keys
                # belonging exclusively to this workspace, never a live mount's key.
                if key is not None and key.expired:
                    _request(client, "POST", "DeleteKey", params={"id": key.access_key_id})
            key = _Key.model_validate_json(
                _request(
                    client,
                    "POST",
                    "CreateKey",
                    body={"name": name, "expiration": expiration.isoformat()},
                ).content
            )
            try:
                if key.expiration != expiration or key.expired or key.secret_access_key is None:
                    raise RuntimeError("Garage did not issue the requested expiring credential")
                _request(
                    client,
                    "POST",
                    "AllowBucketKey",
                    body={
                        "bucketId": bucket.id,
                        "accessKeyId": key.access_key_id,
                        "permissions": {"read": True, "write": True, "owner": False},
                    },
                )
                _request(
                    client,
                    "POST",
                    "AddBucketAlias",
                    body={
                        "bucketId": bucket.id,
                        "accessKeyId": key.access_key_id,
                        "localAlias": expected_bucket,
                    },
                )
                return WorkspaceStorageGrant(
                    endpoint_url=self.settings.endpoint_url,
                    region=self.settings.region_name,
                    bucket_name=expected_bucket,
                    force_path_style=self.settings.force_path_style,
                    access_key=key.access_key_id,
                    secret_key=key.secret_access_key.get_secret_value(),
                    expires_at=key.expiration,
                )
            except Exception:
                _request(client, "POST", "DeleteKey", params={"id": key.access_key_id})
                raise

    def retire(self, *, workspace_id: str, storage: WorkspaceStorageConfig) -> None:
        expected_bucket = self._bucket(workspace_id, storage)
        with httpx.Client(
            base_url=self.admin.admin_endpoint_url,
            headers={"Authorization": f"Bearer {self.admin.admin_token.get_secret_value()}"},
            timeout=15,
        ) as client:
            bucket = self._lookup_bucket(client, expected_bucket)
            if bucket is not None:
                name = f"lazycloud-workspace:{expected_bucket}"
                for candidate in bucket.keys:
                    key = self._workspace_key(client, candidate, bucket_id=bucket.id, name=name)
                    if key is not None:
                        _request(client, "POST", "DeleteKey", params={"id": key.access_key_id})
        object_store = S3ObjectStoreClient.from_settings(self.settings)
        try:
            object_store.retire_bucket(expected_bucket)
        finally:
            object_store.close()


__all__ = ["GarageSettings", "GarageWorkspaceStorageIssuer"]
