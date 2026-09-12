from __future__ import annotations

import threading
from base64 import b64decode, b64encode
from binascii import Error as BinasciiError
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import md5
from pathlib import Path
from types import ModuleType
from typing import (
    Generic,
    Literal,
    NotRequired,
    Protocol,
    TypedDict,
    TypeGuard,
    Unpack,
    runtime_checkable,
)
from uuid import uuid4

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.awsrequest import AWSRequest
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import ClientError
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.app_identity import (
    ENV_PREFIX,
    OBJECT_STORE_BUCKET,
    WORKSPACE_BUCKET_PREFIX,
)
from shared.contracts import ContractModel
from shared.deployment_settings import MissingDeploymentSettingError
from typing_extensions import TypeVar

S3_PRESIGNED_URL_MAX_EXPIRES_SECONDS = 604800


class S3Credentials(BaseSettings):
    endpoint_url: str = ""
    region_name: str = "us-east-1"
    access_key_id: str = Field(default="", repr=False)
    secret_access_key: str = Field(default="", repr=False)
    session_token: str = Field(default="", repr=False)
    force_path_style: bool = True

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_OBJECT_STORE_", extra="ignore", hide_input_in_errors=True
    )

    @model_validator(mode="after")
    def require_credentials(self) -> S3Credentials:
        if not self.endpoint_url:
            raise MissingDeploymentSettingError(
                f"{ENV_PREFIX}_OBJECT_STORE_ENDPOINT_URL", purpose="the object storage endpoint"
            )
        if bool(self.access_key_id) != bool(self.secret_access_key):
            raise ValueError(
                "object-store credentials require both access and secret keys, or neither"
            )
        if self.session_token and not self.access_key_id:
            raise ValueError("an object-store session token requires an access and secret key pair")
        return self

    def transport_settings(
        self, *, bucket: str, workspace_bucket_prefix: str = ""
    ) -> S3ObjectStoreSettings:
        return S3ObjectStoreSettings(
            endpoint_url=self.endpoint_url,
            region_name=self.region_name,
            access_key_id=self.access_key_id,
            secret_access_key=self.secret_access_key,
            session_token=self.session_token,
            force_path_style=self.force_path_style,
            bucket=bucket,
            workspace_bucket_prefix=workspace_bucket_prefix,
        )


class S3ObjectStoreSettings(S3Credentials):
    bucket: str = OBJECT_STORE_BUCKET
    workspace_bucket_prefix: str = WORKSPACE_BUCKET_PREFIX
    credential_expires_at: datetime | None = None
    transfer_multipart_threshold_bytes: int = 64 * 1024 * 1024
    transfer_multipart_chunk_size_bytes: int = 64 * 1024 * 1024
    transfer_max_concurrency: int = 2

    @field_validator("credential_expires_at", mode="before")
    @classmethod
    def normalize_optional_credential_expiration(
        cls,
        value: datetime | str | None,
    ) -> datetime | str | None:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def validate_credentials(self) -> S3ObjectStoreSettings:
        if self.credential_expires_at is not None:
            expiration = self.credential_expires_at
            if expiration.tzinfo is None or expiration.utcoffset() is None:
                raise ValueError("object-store credential expiration must include a timezone")
        return self


class S3ObjectInfo(ContractModel):
    bucket: str
    key: str
    size: int | None = None
    etag: str | None = None
    last_modified: datetime | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class S3PresignedUpload(ContractModel):
    url: str = Field(min_length=1)
    headers: dict[str, str]


class _S3ReadableBody(Protocol):
    def read(self, amount: int | None = None) -> bytes: ...


class _GetObjectResponse(TypedDict):
    Body: _S3ReadableBody


class _HeadObjectResponse(TypedDict, total=False):
    ContentLength: int
    ETag: str
    LastModified: datetime
    Metadata: dict[str, str]


class _CreateMultipartUploadResponse(TypedDict):
    UploadId: str


class _ListedMultipartUpload(TypedDict):
    Key: str
    UploadId: str


class _ListMultipartUploadsResponse(TypedDict, total=False):
    Uploads: list[_ListedMultipartUpload]
    IsTruncated: bool
    NextKeyMarker: str
    NextUploadIdMarker: str


class _ListedObject(TypedDict, total=False):
    Key: str
    Size: int
    LastModified: datetime


class _CommonPrefix(TypedDict):
    Prefix: str


class _ListObjectsResponse(TypedDict, total=False):
    Contents: list[_ListedObject]
    CommonPrefixes: list[_CommonPrefix]
    IsTruncated: bool
    NextContinuationToken: str


class _PresignParams(TypedDict):
    Bucket: str
    Key: str
    ContentType: NotRequired[str]
    ContentLength: NotRequired[int]
    ChecksumSHA256: NotRequired[str]
    Metadata: NotRequired[dict[str, str]]
    UploadId: NotRequired[str]
    PartNumber: NotRequired[int]


class _S3UploadExtraArgs(TypedDict):
    ContentType: str
    Metadata: dict[str, str]


class _CompletedPart(TypedDict):
    PartNumber: int
    ETag: str


class _MultipartUpload(TypedDict):
    Parts: list[_CompletedPart]


class _CopySource(TypedDict):
    Bucket: str
    Key: str


class _DeleteTarget(TypedDict):
    Key: str


class _DeleteRequest(TypedDict):
    Objects: list[_DeleteTarget]
    Quiet: bool


class _DeleteError(TypedDict):
    Key: str
    Code: str


class _DeleteObjectsResponse(TypedDict, total=False):
    Errors: list[_DeleteError]


class _CreateBucketConfiguration(TypedDict):
    LocationConstraint: str


class _BeforeSignHandler(Protocol):
    def __call__(self, request: AWSRequest, **context: str | int | bool | None) -> None: ...


class _S3Events(Protocol):
    def register(self, event_name: str, handler: _BeforeSignHandler) -> None: ...


class _S3ClientMeta(Protocol):
    events: _S3Events


@runtime_checkable
class _ClosableClient(Protocol):
    def close(self) -> None: ...


@runtime_checkable
class _ClientWithMeta(Protocol):
    meta: _S3ClientMeta


class _PutObjectOptions(TypedDict, total=False):
    CacheControl: str
    IfNoneMatch: Literal["*"]


@runtime_checkable
class _PutObjectClient(Protocol):
    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: bytes,
        ContentType: str,
        Metadata: dict[str, str],
        **options: Unpack[_PutObjectOptions],
    ) -> None: ...


@runtime_checkable
class _UploadFileClient(Protocol):
    def upload_file(
        self,
        Filename: str,
        Bucket: str,
        Key: str,
        *,
        ExtraArgs: _S3UploadExtraArgs,
        Config: TransferConfig,
    ) -> None: ...


@runtime_checkable
class _GetObjectClient(Protocol):
    def get_object(self, *, Bucket: str, Key: str) -> _GetObjectResponse: ...


@runtime_checkable
class _DownloadFileClient(Protocol):
    def download_file(self, Bucket: str, Key: str, Filename: str) -> None: ...


@runtime_checkable
class _HeadObjectClient(Protocol):
    def head_object(self, *, Bucket: str, Key: str) -> _HeadObjectResponse: ...


@runtime_checkable
class _PresignClient(Protocol):
    def generate_presigned_url(
        self,
        ClientMethod: str,
        *,
        Params: _PresignParams,
        ExpiresIn: int,
    ) -> str: ...


@runtime_checkable
class _MultipartClient(Protocol):
    def create_multipart_upload(
        self,
        *,
        Bucket: str,
        Key: str,
    ) -> _CreateMultipartUploadResponse: ...

    def complete_multipart_upload(
        self,
        *,
        Bucket: str,
        Key: str,
        UploadId: str,
        MultipartUpload: _MultipartUpload,
    ) -> None: ...

    def abort_multipart_upload(
        self,
        *,
        Bucket: str,
        Key: str,
        UploadId: str,
    ) -> None: ...


@runtime_checkable
class _DeleteObjectClient(Protocol):
    def delete_object(self, *, Bucket: str, Key: str) -> None: ...


@runtime_checkable
class _CopyObjectClient(Protocol):
    def copy_object(self, *, Bucket: str, Key: str, CopySource: _CopySource) -> None: ...


@runtime_checkable
class _BucketClient(Protocol):
    def create_bucket(
        self,
        *,
        Bucket: str,
        CreateBucketConfiguration: _CreateBucketConfiguration | None = None,
    ) -> None: ...

    def head_bucket(self, *, Bucket: str) -> None: ...


@runtime_checkable
class _ListMultipartUploadsClient(Protocol):
    def list_multipart_uploads(
        self, *, Bucket: str, Prefix: str = "", KeyMarker: str = "", UploadIdMarker: str = ""
    ) -> _ListMultipartUploadsResponse: ...


@runtime_checkable
class _RetireBucketClient(_ListMultipartUploadsClient, Protocol):
    def delete_bucket(self, *, Bucket: str) -> None: ...


class _CorsRule(TypedDict):
    AllowedOrigins: list[str]
    AllowedMethods: list[str]
    AllowedHeaders: list[str]
    ExposeHeaders: list[str]


class _CorsConfiguration(TypedDict):
    CORSRules: list[_CorsRule]


class _AbortMultipartRule(TypedDict):
    DaysAfterInitiation: int


class _LifecycleRule(TypedDict):
    ID: str
    Status: str
    Filter: dict[str, str]
    AbortIncompleteMultipartUpload: _AbortMultipartRule


class _LifecycleConfiguration(TypedDict):
    Rules: list[_LifecycleRule]


@runtime_checkable
class _BucketPolicyClient(Protocol):
    def put_bucket_cors(self, *, Bucket: str, CORSConfiguration: _CorsConfiguration) -> None: ...

    def put_bucket_lifecycle_configuration(
        self, *, Bucket: str, LifecycleConfiguration: _LifecycleConfiguration
    ) -> None: ...


@runtime_checkable
class _ListObjectsClient(Protocol):
    def list_objects_v2(
        self,
        *,
        Bucket: str,
        Prefix: str,
        Delimiter: str = "",
        ContinuationToken: str = "",
    ) -> _ListObjectsResponse: ...


@runtime_checkable
class _DeleteObjectsClient(Protocol):
    def delete_objects(self, *, Bucket: str, Delete: _DeleteRequest) -> _DeleteObjectsResponse: ...


@runtime_checkable
class _FullS3Client(
    _ClosableClient,
    _ClientWithMeta,
    _PutObjectClient,
    _UploadFileClient,
    _GetObjectClient,
    _DownloadFileClient,
    _HeadObjectClient,
    _PresignClient,
    _MultipartClient,
    _DeleteObjectClient,
    _CopyObjectClient,
    _BucketClient,
    _RetireBucketClient,
    _BucketPolicyClient,
    _ListObjectsClient,
    _DeleteObjectsClient,
    Protocol,
):
    pass


type S3ClientCapabilities = (
    _FullS3Client
    | _PutObjectClient
    | _UploadFileClient
    | _GetObjectClient
    | _DownloadFileClient
    | _HeadObjectClient
    | _PresignClient
    | _MultipartClient
    | _DeleteObjectClient
    | _CopyObjectClient
    | _BucketClient
    | _ListMultipartUploadsClient
    | _RetireBucketClient
    | _BucketPolicyClient
    | _ListObjectsClient
    | _DeleteObjectsClient
)

S3ClientT = TypeVar("S3ClientT", default=S3ClientCapabilities)


@runtime_checkable
class _ClientErrorBoundary(Protocol):
    @property
    def response(
        self,
    ) -> Mapping[str, str | int | bool | dict[str, str | int | bool | None] | None]: ...


class _Boto3S3ClientFactory(Protocol):
    def client(
        self,
        service_name: Literal["s3"],
        *,
        endpoint_url: str | None,
        region_name: str,
        config: Config,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
        aws_session_token: str | None = None,
    ) -> BaseClient: ...


@dataclass(slots=True)
class S3ObjectStoreClient(Generic[S3ClientT]):
    settings: S3ObjectStoreSettings
    client: S3ClientT
    _close_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    @staticmethod
    def from_settings(
        settings: S3ObjectStoreSettings,
    ) -> S3ObjectStoreClient[S3ClientCapabilities]:
        return S3ObjectStoreClient[S3ClientCapabilities](
            settings=settings,
            client=_new_s3_client(settings, settings.endpoint_url),
        )

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            if isinstance(self.client, _ClosableClient):
                self.client.close()

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        bucket: str | None = None,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
        cache_control: str | None = None,
        if_absent: bool = False,
    ) -> S3ObjectInfo:
        target_bucket = bucket or self.settings.bucket
        client = self.client
        if not isinstance(client, _PutObjectClient):
            raise TypeError("configured S3 client does not support object uploads")
        options: _PutObjectOptions = {}
        if cache_control is not None:
            options["CacheControl"] = cache_control
        if if_absent:
            options["IfNoneMatch"] = "*"
        client.put_object(
            Bucket=target_bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            Metadata=metadata or {},
            **options,
        )
        return S3ObjectInfo(
            bucket=target_bucket,
            key=key,
            size=len(data),
            metadata=metadata or {},
        )

    def put_file(
        self,
        key: str,
        source: str | Path,
        *,
        bucket: str | None = None,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> S3ObjectInfo:
        target_bucket = bucket or self.settings.bucket
        source_path = Path(source).expanduser().resolve()
        extra_args = _S3UploadExtraArgs(
            ContentType=content_type,
            Metadata=metadata or {},
        )
        client = self.client
        if not isinstance(client, _UploadFileClient):
            raise TypeError("configured S3 client does not support managed file uploads")
        client.upload_file(
            str(source_path),
            target_bucket,
            key,
            ExtraArgs=extra_args,
            Config=_transfer_config(self.settings),
        )
        return S3ObjectInfo(
            bucket=target_bucket,
            key=key,
            size=source_path.stat().st_size,
            metadata=metadata or {},
        )

    def read_bytes(self, key: str, *, bucket: str | None = None) -> bytes:
        target_bucket = bucket or self.settings.bucket
        client = self.client
        if not isinstance(client, _GetObjectClient):
            raise TypeError("configured S3 client does not support object downloads")
        response = client.get_object(Bucket=target_bucket, Key=key)
        body = response["Body"]
        return body.read()

    def download_file(
        self,
        key: str,
        target: str | Path,
        *,
        bucket: str | None = None,
    ) -> S3ObjectInfo:
        target_bucket = bucket or self.settings.bucket
        target_path = Path(target).expanduser().resolve()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = target_path.with_name(f".{target_path.name}.{uuid4().hex}.tmp")
        try:
            client = self.client
            if not isinstance(client, _DownloadFileClient):
                raise TypeError("configured S3 client does not support managed file downloads")
            client.download_file(target_bucket, key, str(temp_path))
            temp_path.replace(target_path)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise
        return S3ObjectInfo(
            bucket=target_bucket,
            key=key,
            size=target_path.stat().st_size,
        )

    def head(self, key: str, *, bucket: str | None = None) -> S3ObjectInfo:
        target_bucket = bucket or self.settings.bucket
        client = self.client
        if not isinstance(client, _HeadObjectClient):
            raise TypeError("configured S3 client does not support object metadata")
        response = client.head_object(Bucket=target_bucket, Key=key)
        return S3ObjectInfo(
            bucket=target_bucket,
            key=key,
            size=response.get("ContentLength"),
            etag=response.get("ETag"),
            last_modified=response.get("LastModified"),
            metadata=response.get("Metadata", {}),
        )

    def exists(self, key: str, *, bucket: str | None = None) -> bool:
        target_bucket = bucket or self.settings.bucket
        client = self.client
        if not isinstance(client, _HeadObjectClient):
            raise TypeError("configured S3 client does not support object metadata")
        try:
            client.head_object(Bucket=target_bucket, Key=key)
        except ClientError as exc:
            code, status = _client_error_code_and_status(exc)
            if code in {"404", "NoSuchKey", "NotFound"} or status == 404:
                return False
            raise
        return True

    def generate_presigned_get_url(
        self,
        key: str,
        *,
        bucket: str | None = None,
        expires_seconds: int = 3600,
    ) -> str:
        return str(
            self._presigner.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket or self.settings.bucket, "Key": key},
                ExpiresIn=_effective_presign_expiration(self.settings, expires_seconds),
            )
        )

    def generate_presigned_head_url(
        self,
        key: str,
        *,
        bucket: str | None = None,
        expires_seconds: int = 3600,
    ) -> str:
        return str(
            self._presigner.generate_presigned_url(
                "head_object",
                Params={"Bucket": bucket or self.settings.bucket, "Key": key},
                ExpiresIn=_effective_presign_expiration(self.settings, expires_seconds),
            )
        )

    def generate_presigned_put_url(
        self,
        key: str,
        *,
        bucket: str | None = None,
        expires_seconds: int = 3600,
        content_length: int = 0,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> str:
        return self.generate_presigned_put(
            key,
            bucket=bucket,
            expires_seconds=expires_seconds,
            content_length=content_length,
            content_type=content_type,
            metadata=metadata,
        ).url

    def generate_presigned_put(
        self,
        key: str,
        *,
        bucket: str | None = None,
        expires_seconds: int = 3600,
        content_length: int,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
        checksum_sha256: str = "",
    ) -> S3PresignedUpload:
        if content_length < 0:
            raise ValueError("presigned upload content length must be nonnegative")
        normalized_content_type = content_type.strip()
        if (
            not normalized_content_type
            or "\r" in normalized_content_type
            or "\n" in normalized_content_type
        ):
            raise ValueError("presigned upload content type is invalid")
        normalized_metadata = _validated_presign_metadata(metadata)
        normalized_checksum = _validated_presign_checksum_sha256(checksum_sha256)
        params = _PresignParams(
            Bucket=bucket or self.settings.bucket,
            Key=key,
            ContentType=normalized_content_type,
            ContentLength=content_length,
        )
        if normalized_checksum:
            # Signed into the URL, so the store rejects any body whose digest differs
            # from the one the control plane recorded. Without it the integrity chain
            # terminates at whatever digest the uploader declared about itself.
            params["ChecksumSHA256"] = normalized_checksum
        if normalized_metadata:
            params["Metadata"] = normalized_metadata
        url = str(
            self._presigner.generate_presigned_url(
                "put_object",
                Params=params,
                ExpiresIn=_effective_presign_expiration(self.settings, expires_seconds),
            )
        )
        headers = {
            "content-length": str(content_length),
            "content-type": normalized_content_type,
            **({"x-amz-checksum-sha256": normalized_checksum} if normalized_checksum else {}),
            **{f"x-amz-meta-{key}": value for key, value in normalized_metadata.items()},
        }
        return S3PresignedUpload(url=url, headers=headers)

    def create_multipart_upload(self, key: str, *, bucket: str | None = None) -> str:
        client = self.client
        if not isinstance(client, _MultipartClient):
            raise TypeError("configured S3 client does not support multipart uploads")
        response = client.create_multipart_upload(
            Bucket=bucket or self.settings.bucket,
            Key=key,
        )
        return str(response["UploadId"])

    def generate_presigned_upload_part_url(
        self,
        key: str,
        *,
        upload_id: str,
        part_number: int,
        bucket: str | None = None,
        expires_seconds: int = 3600,
    ) -> str:
        return str(
            self._presigner.generate_presigned_url(
                "upload_part",
                Params={
                    "Bucket": bucket or self.settings.bucket,
                    "Key": key,
                    "UploadId": upload_id,
                    "PartNumber": part_number,
                },
                ExpiresIn=_effective_presign_expiration(self.settings, expires_seconds),
            )
        )

    def complete_multipart_upload(
        self,
        key: str,
        *,
        upload_id: str,
        completed_parts: list[tuple[int, str]] | tuple[tuple[int, str], ...],
        bucket: str | None = None,
    ) -> None:
        parts: list[_CompletedPart] = [
            _CompletedPart(PartNumber=number, ETag=etag)
            for number, etag in sorted(completed_parts, key=lambda item: item[0])
        ]
        client = self.client
        if not isinstance(client, _MultipartClient):
            raise TypeError("configured S3 client does not support multipart uploads")
        client.complete_multipart_upload(
            Bucket=bucket or self.settings.bucket,
            Key=key,
            UploadId=upload_id,
            MultipartUpload=_MultipartUpload(Parts=parts),
        )

    def abort_multipart_upload(
        self,
        key: str,
        *,
        upload_id: str,
        bucket: str | None = None,
    ) -> None:
        client = self.client
        if not isinstance(client, _MultipartClient):
            raise TypeError("configured S3 client does not support multipart uploads")
        client.abort_multipart_upload(
            Bucket=bucket or self.settings.bucket,
            Key=key,
            UploadId=upload_id,
        )

    def delete(self, key: str, *, bucket: str | None = None) -> None:
        client = self.client
        if not isinstance(client, _DeleteObjectClient):
            raise TypeError("configured S3 client does not support object deletion")
        client.delete_object(Bucket=bucket or self.settings.bucket, Key=key)

    def copy(
        self,
        source_key: str,
        destination_key: str,
        *,
        bucket: str | None = None,
        source_bucket: str | None = None,
    ) -> None:
        client = self.client
        if not isinstance(client, _CopyObjectClient):
            raise TypeError("configured S3 client does not support server-side copies")
        target_bucket = bucket or self.settings.bucket
        client.copy_object(
            Bucket=target_bucket,
            Key=destination_key,
            CopySource=_CopySource(Bucket=source_bucket or target_bucket, Key=source_key),
        )

    def create_bucket(self, bucket: str | None = None) -> None:
        target_bucket = bucket or self.settings.bucket
        client = self.client
        if not isinstance(client, _BucketClient):
            raise TypeError("configured S3 client does not support bucket management")
        try:
            if self.settings.region_name and self.settings.region_name != "us-east-1":
                client.create_bucket(
                    Bucket=target_bucket,
                    CreateBucketConfiguration=_CreateBucketConfiguration(
                        LocationConstraint=self.settings.region_name
                    ),
                )
            else:
                client.create_bucket(Bucket=target_bucket)
        except ClientError as exc:
            code, _status = _client_error_code_and_status(exc)
            if code == "BucketAlreadyOwnedByYou":
                return
            raise

    def validate_bucket_access(self, bucket: str | None = None) -> None:
        client = self.client
        if not isinstance(client, _BucketClient):
            raise TypeError("configured S3 client does not support bucket management")
        client.head_bucket(Bucket=bucket or self.settings.bucket)

    def retire_bucket(self, bucket: str) -> None:
        """Purge an exclusively owned bucket after its writers have stopped."""
        if not bucket or bucket == self.settings.bucket:
            raise ValueError("cannot retire the shared platform object bucket")
        client = self.client
        if not isinstance(client, _RetireBucketClient):
            raise TypeError("configured S3 client does not support bucket retirement")
        try:
            self.abort_multipart_uploads("", bucket=bucket)
            self.delete_prefix("", bucket=bucket)
            client.delete_bucket(Bucket=bucket)
        except ClientError as exc:
            code, _ = _client_error_code_and_status(exc)
            if code != "NoSuchBucket":
                raise

    def abort_multipart_uploads(self, prefix: str, *, bucket: str | None = None) -> None:
        client = self.client
        if not isinstance(client, _ListMultipartUploadsClient):
            raise TypeError("configured S3 client does not support multipart listing")
        target_bucket = bucket or self.settings.bucket
        key_marker = ""
        upload_marker = ""
        while True:
            response = client.list_multipart_uploads(
                Bucket=target_bucket,
                Prefix=prefix,
                KeyMarker=key_marker,
                UploadIdMarker=upload_marker,
            )
            for upload in response.get("Uploads", ()):
                if not upload["Key"].startswith(prefix):
                    raise RuntimeError("multipart listing returned an upload outside the prefix")
                try:
                    self.abort_multipart_upload(
                        upload["Key"], upload_id=upload["UploadId"], bucket=target_bucket
                    )
                except ClientError as exc:
                    code, _ = _client_error_code_and_status(exc)
                    if code != "NoSuchUpload":
                        raise
            if not response.get("IsTruncated"):
                return
            next_key = response.get("NextKeyMarker", "")
            next_upload = response.get("NextUploadIdMarker", "")
            if not next_key or (next_key, next_upload) == (key_marker, upload_marker):
                raise RuntimeError("multipart listing did not advance during upload cleanup")
            key_marker, upload_marker = next_key, next_upload

    def configure_workspace_bucket(self, bucket: str, *, public_origin: str) -> None:
        client = self.client
        if not isinstance(client, _BucketPolicyClient):
            raise TypeError("configured S3 client does not support bucket CORS and lifecycle")
        if not public_origin:
            raise ValueError("workspace bucket CORS requires the public gateway origin")
        client.put_bucket_cors(
            Bucket=bucket,
            CORSConfiguration={
                "CORSRules": [
                    {
                        "AllowedOrigins": [public_origin],
                        "AllowedMethods": ["GET", "HEAD", "PUT"],
                        "AllowedHeaders": ["*"],
                        "ExposeHeaders": ["ETag", "x-amz-checksum-sha256"],
                    }
                ]
            },
        )
        client.put_bucket_lifecycle_configuration(
            Bucket=bucket,
            LifecycleConfiguration={
                "Rules": [
                    {
                        "ID": "abort-incomplete-uploads",
                        "Status": "Enabled",
                        "Filter": {"Prefix": ""},
                        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
                    }
                ]
            },
        )

    def list_directory(self, prefix: str, *, bucket: str | None = None) -> tuple[S3ObjectInfo, ...]:
        target_bucket = bucket or self.settings.bucket
        directory = prefix if prefix.endswith("/") else f"{prefix}/"
        objects: list[S3ObjectInfo] = []
        continuation_token: str | None = None
        client = self.client
        if not isinstance(client, _ListObjectsClient):
            raise TypeError("configured S3 client does not support object listing")
        while True:
            if continuation_token is None:
                response = client.list_objects_v2(
                    Bucket=target_bucket,
                    Prefix=directory,
                    Delimiter="/",
                )
            else:
                response = client.list_objects_v2(
                    Bucket=target_bucket,
                    Prefix=directory,
                    Delimiter="/",
                    ContinuationToken=continuation_token,
                )
            for item in response.get("Contents", ()):
                key = item.get("Key")
                if not key or key == directory:
                    continue
                objects.append(
                    S3ObjectInfo(
                        bucket=target_bucket,
                        key=key,
                        size=item.get("Size"),
                        last_modified=item.get("LastModified"),
                    )
                )
            for item in response.get("CommonPrefixes", ()):
                key = item.get("Prefix")
                if key:
                    objects.append(
                        S3ObjectInfo(
                            bucket=target_bucket,
                            key=key,
                            size=0,
                            last_modified=datetime.now(UTC),
                        )
                    )
            if not response.get("IsTruncated"):
                return tuple(objects)
            continuation_token = response.get("NextContinuationToken")
            if continuation_token is None:
                return tuple(objects)

    def list_prefix(self, prefix: str, *, bucket: str | None = None) -> tuple[S3ObjectInfo, ...]:
        target_bucket = bucket or self.settings.bucket
        objects: list[S3ObjectInfo] = []
        continuation_token: str | None = None
        client = self.client
        if not isinstance(client, _ListObjectsClient):
            raise TypeError("configured S3 client does not support object listing")
        while True:
            if continuation_token is None:
                response = client.list_objects_v2(Bucket=target_bucket, Prefix=prefix)
            else:
                response = client.list_objects_v2(
                    Bucket=target_bucket,
                    Prefix=prefix,
                    ContinuationToken=continuation_token,
                )
            for item in response.get("Contents", ()):
                key = item.get("Key")
                if not key:
                    continue
                objects.append(
                    S3ObjectInfo(
                        bucket=target_bucket,
                        key=key,
                        size=item.get("Size"),
                        last_modified=item.get("LastModified"),
                    )
                )
            if not response.get("IsTruncated"):
                return tuple(objects)
            continuation_token = response.get("NextContinuationToken")
            if continuation_token is None:
                return tuple(objects)

    def delete_prefix(self, prefix: str, *, bucket: str | None = None) -> tuple[str, ...]:
        target_bucket = bucket or self.settings.bucket
        deleted: list[str] = []
        continuation_token: str | None = None
        client = self.client
        if not isinstance(client, _ListObjectsClient):
            raise TypeError("configured S3 client does not support object listing")
        if not isinstance(client, _DeleteObjectsClient):
            raise TypeError("configured S3 client does not support bulk object deletion")
        while True:
            if continuation_token is None:
                response = client.list_objects_v2(Bucket=target_bucket, Prefix=prefix)
            else:
                response = client.list_objects_v2(
                    Bucket=target_bucket,
                    Prefix=prefix,
                    ContinuationToken=continuation_token,
                )
            key_list: list[str] = []
            for item in response.get("Contents", ()):
                item_key = item.get("Key")
                if isinstance(item_key, str):
                    key_list.append(item_key)
            keys = tuple(key_list)
            for index in range(0, len(keys), 1000):
                batch = keys[index : index + 1000]
                result = client.delete_objects(
                    Bucket=target_bucket,
                    Delete=_DeleteRequest(
                        Objects=[_DeleteTarget(Key=key) for key in batch],
                        Quiet=True,
                    ),
                )
                if result.get("Errors"):
                    raise RuntimeError("object store did not delete every requested object")
            deleted.extend(keys)
            if not response.get("IsTruncated"):
                return tuple(deleted)
            continuation_token = response.get("NextContinuationToken")
            if continuation_token is None:
                return tuple(deleted)

    @property
    def _presigner(self) -> _PresignClient:
        client = self.client
        if not isinstance(client, _PresignClient):
            raise TypeError("configured S3 client does not support presigned URLs")
        return client


# A connect to a reachable store is milliseconds; the read timeout is per socket
# read rather than a whole-transfer deadline, so it does not cap large uploads.
OBJECT_STORE_CONNECT_TIMEOUT_SECONDS = 5
OBJECT_STORE_READ_TIMEOUT_SECONDS = 60
OBJECT_STORE_MAX_ATTEMPTS = 3


def _new_s3_client(settings: S3ObjectStoreSettings, endpoint_url: str | None) -> _FullS3Client:
    s3_config = Config(
        region_name=settings.region_name,
        signature_version="s3v4",
        s3={"addressing_style": "path" if settings.force_path_style else "virtual"},
        # Unbounded by default, which turns an unreachable object store into a
        # parked thread rather than an error. Sync routes run on a bounded
        # worker pool, so that is how one outage becomes a stalled API.
        connect_timeout=OBJECT_STORE_CONNECT_TIMEOUT_SECONDS,
        read_timeout=OBJECT_STORE_READ_TIMEOUT_SECONDS,
        retries={"mode": "standard", "max_attempts": OBJECT_STORE_MAX_ATTEMPTS},
    )
    boto3_module: ModuleType = boto3
    if not _is_boto3_s3_client_factory(boto3_module):
        raise TypeError("boto3 module is missing the client factory operation")
    base_client = boto3_module.client(
        "s3",
        endpoint_url=endpoint_url,
        region_name=settings.region_name,
        aws_access_key_id=settings.access_key_id or None,
        aws_secret_access_key=settings.secret_access_key or None,
        aws_session_token=settings.session_token or None,
        config=s3_config,
    )
    candidate = base_client
    if not _is_full_s3_client(candidate):
        raise TypeError("boto3 S3 client is missing required object-store operations")
    candidate.meta.events.register("before-sign.s3.DeleteObjects", _add_delete_objects_content_md5)
    return candidate


def _is_boto3_s3_client_factory(value: ModuleType) -> TypeGuard[_Boto3S3ClientFactory]:
    return callable(getattr(value, "client", None))


def _is_full_s3_client(value: BaseClient) -> TypeGuard[_FullS3Client]:
    return all(
        callable(getattr(value, operation, None))
        for operation in (
            "put_object",
            "upload_file",
            "get_object",
            "download_file",
            "head_object",
            "generate_presigned_url",
            "create_multipart_upload",
            "complete_multipart_upload",
            "abort_multipart_upload",
            "delete_object",
            "create_bucket",
            "head_bucket",
            "delete_bucket",
            "list_multipart_uploads",
            "put_bucket_cors",
            "put_bucket_lifecycle_configuration",
            "list_objects_v2",
            "delete_objects",
            "copy_object",
            "close",
        )
    ) and hasattr(value, "meta")


def _client_error_code_and_status(error: BaseException) -> tuple[str, int | None]:
    if not isinstance(error, _ClientErrorBoundary):
        return "", None
    error_section = error.response.get("Error")
    code = ""
    if isinstance(error_section, dict):
        raw_code = error_section.get("Code")
        if isinstance(raw_code, str | int):
            code = str(raw_code)
    metadata_section = error.response.get("ResponseMetadata")
    status: int | None = None
    if isinstance(metadata_section, dict):
        raw_status = metadata_section.get("HTTPStatusCode")
        if isinstance(raw_status, int):
            status = raw_status
    return code, status


def _add_delete_objects_content_md5(
    request: AWSRequest,
    **_: str | int | bool | None,
) -> None:
    body = request.body
    if not isinstance(body, bytes):
        raise TypeError("S3 DeleteObjects request body must be bytes before signing")
    request.headers["Content-MD5"] = b64encode(md5(body, usedforsecurity=False).digest()).decode(
        "ascii"
    )


def _transfer_config(settings: S3ObjectStoreSettings) -> TransferConfig:
    return TransferConfig(
        multipart_threshold=max(settings.transfer_multipart_threshold_bytes, 5 * 1024 * 1024),
        multipart_chunksize=max(settings.transfer_multipart_chunk_size_bytes, 5 * 1024 * 1024),
        max_concurrency=max(settings.transfer_max_concurrency, 1),
        use_threads=True,
    )


def _effective_presign_expiration(
    settings: S3ObjectStoreSettings,
    requested_seconds: int,
) -> int:
    if requested_seconds <= 0:
        raise ValueError("presigned URL expiration must be positive")
    effective_seconds = min(requested_seconds, S3_PRESIGNED_URL_MAX_EXPIRES_SECONDS)
    if settings.credential_expires_at is None:
        return effective_seconds
    expiration = settings.credential_expires_at.astimezone(UTC)
    remaining_seconds = int((expiration - datetime.now(UTC)).total_seconds()) - 30
    if remaining_seconds <= 0:
        raise RuntimeError("object-store temporary credentials are too close to expiration")
    return min(effective_seconds, remaining_seconds)


def _validated_presign_metadata(metadata: Mapping[str, str] | None) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for raw_key, raw_value in (metadata or {}).items():
        key = raw_key.strip().lower()
        value = raw_value.strip()
        if (
            not key
            or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for character in key)
            or not value
            or "\r" in value
            or "\n" in value
        ):
            raise ValueError("presigned upload metadata is invalid")
        if key in normalized:
            raise ValueError("presigned upload metadata contains duplicate keys")
        normalized[key] = value
    return normalized


def _validated_presign_checksum_sha256(checksum_sha256: str) -> str:
    """Base64 of the raw 32 digest bytes, which is what S3 signs.

    The repository carries sha256 as lowercase hex everywhere else, so this is the
    one boundary where the encoding changes and the one place to catch a caller
    that forgot to convert.
    """

    value = checksum_sha256.strip()
    if not value:
        return ""
    try:
        raw = b64decode(value, validate=True)
    except BinasciiError as exc:
        raise ValueError("presigned upload checksum must be base64-encoded sha256") from exc
    if len(raw) != 32 or b64encode(raw).decode() != value:
        raise ValueError("presigned upload checksum must be base64-encoded sha256")
    return value
