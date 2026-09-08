from __future__ import annotations

from base64 import b64encode
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from urllib.parse import urlsplit

import jwt
from shared.identity import WorkspaceStorageConfig
from shared.timestamps import utc_now
from shared.workspace_storage import WorkspaceStorageGrant
from storage_client.s3 import S3ObjectStoreClient, S3ObjectStoreSettings

WORKSPACE_CREDENTIAL_SECONDS = 900


@dataclass(frozen=True, slots=True)
class CloudflareWorkspaceStorageIssuer:
    settings: S3ObjectStoreSettings

    @property
    def account_id(self) -> str:
        endpoint = urlsplit(self.settings.endpoint_url)
        suffix = ".r2.cloudflarestorage.com"
        host = endpoint.hostname or ""
        account_id = host.removesuffix(suffix)
        if (
            endpoint.scheme != "https"
            or not host.endswith(suffix)
            or len(account_id) != 32
            or any(character not in "0123456789abcdef" for character in account_id)
            or endpoint.path not in {"", "/"}
            or endpoint.query
            or endpoint.fragment
            or endpoint.username
            or endpoint.password
        ):
            raise ValueError("managed workspace credentials require a Cloudflare R2 endpoint")
        return account_id

    def _bucket(self, workspace_id: str, storage: WorkspaceStorageConfig) -> str:
        if storage.access_key or storage.secret_key:
            raise ValueError("managed workspace storage cannot use customer credentials")
        expected_bucket = f"{self.settings.workspace_bucket_prefix}-{workspace_id}".replace(
            "_", "-"
        )
        if storage.bucket != expected_bucket or storage.endpoint_url != self.settings.endpoint_url:
            raise ValueError("workspace storage does not name its deployment-owned R2 bucket")
        if storage.key_prefix:
            raise ValueError("a workspace R2 bucket cannot carry an alternate storage prefix")
        return expected_bucket

    def issue(self, *, workspace_id: str, storage: WorkspaceStorageConfig) -> WorkspaceStorageGrant:
        expected_bucket = self._bucket(workspace_id, storage)
        now = utc_now()
        expires_at = now + timedelta(seconds=WORKSPACE_CREDENTIAL_SECONDS)
        signed = jwt.encode(
            {
                "sub": self.account_id,
                "iss": self.settings.access_key_id,
                "aud": f"{self.account_id}.r2.cloudflarestorage.com",
                "iat": int(now.timestamp()),
                "exp": int(expires_at.timestamp()),
                "bucket": expected_bucket,
                "scope": "object-read-write",
            },
            self.settings.secret_access_key,
            algorithm="HS256",
        )
        return WorkspaceStorageGrant(
            endpoint_url=self.settings.endpoint_url,
            region="auto",
            bucket_name=expected_bucket,
            force_path_style=True,
            access_key=self.settings.access_key_id,
            secret_key=sha256(signed.encode()).hexdigest(),
            session_token=b64encode(f"jwt/{signed}".encode()).decode(),
            expires_at=expires_at,
        )

    def retire(self, *, workspace_id: str, storage: WorkspaceStorageConfig) -> None:
        bucket = self._bucket(workspace_id, storage)
        client = S3ObjectStoreClient.from_settings(self.settings)
        try:
            client.retire_bucket(bucket)
        finally:
            client.close()


__all__ = ["CloudflareWorkspaceStorageIssuer"]
