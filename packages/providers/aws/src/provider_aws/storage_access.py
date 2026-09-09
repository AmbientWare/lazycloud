from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime
from typing import Literal, Protocol, TypedDict, runtime_checkable
from urllib.parse import unquote, unquote_plus

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from botocore.response import StreamingBody
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.errors import UpstreamUnavailableError
from shared.storage_access import (
    StorageAccessDelivery,
    StorageAccessObservation,
    StorageRequestClass,
    StorageTransferEvidence,
)
from storage_client.s3 import S3ObjectStoreSettings

from provider_aws.boto3_clients import is_boto3_client_factory


class AwsStorageAccessSettings(BaseSettings):
    bucket: str = Field(default="", min_length=3, pattern=r"^[a-z0-9][a-z0-9.-]+[a-z0-9]$")
    queue_url: str = Field(
        default="",
        pattern=r"^https://sqs\.[a-z0-9-]+\.amazonaws\.com(?:\.cn)?/[0-9]{12}/[A-Za-z0-9_-]+$",
    )

    model_config = SettingsConfigDict(
        env_prefix="LAZYCLOUD_AWS_STORAGE_ACCESS_", extra="ignore", hide_input_in_errors=True
    )


class _Response(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)


class _Message(_Response):
    ReceiptHandle: str = Field(repr=False)
    Body: str = Field(repr=False)


class _Messages(_Response):
    Messages: list[_Message] = Field(default_factory=list)


class _Bucket(_Response):
    name: str


class _Object(_Response):
    key: str


class _Location(_Response):
    bucket: _Bucket
    object: _Object


class _Record(_Response):
    eventSource: Literal["aws:s3"]
    eventName: str
    awsRegion: str
    s3: _Location


class _Notification(_Response):
    Records: list[_Record]


class _TestNotification(_Response):
    Service: Literal["Amazon S3"]
    Event: Literal["s3:TestEvent"]
    Bucket: str


_NOTIFICATION = TypeAdapter[_Notification | _TestNotification](_Notification | _TestNotification)


class _ObjectResult(TypedDict):
    Body: StreamingBody


class _MessageResult(TypedDict):
    ReceiptHandle: str
    Body: str


class _ReceiveResult(TypedDict, total=False):
    Messages: list[_MessageResult]


@runtime_checkable
class _S3(Protocol):
    def get_object(self, *, Bucket: str, Key: str, ExpectedBucketOwner: str) -> _ObjectResult: ...
    def close(self) -> None: ...


@runtime_checkable
class _Sqs(Protocol):
    def receive_message(
        self,
        *,
        QueueUrl: str,
        MaxNumberOfMessages: int,
        WaitTimeSeconds: int,
        VisibilityTimeout: int,
    ) -> _ReceiveResult: ...
    def delete_message(self, *, QueueUrl: str, ReceiptHandle: str) -> None: ...
    def change_message_visibility(
        self, *, QueueUrl: str, ReceiptHandle: str, VisibilityTimeout: int
    ) -> None: ...
    def close(self) -> None: ...


# Request-URI has no literal whitespace inside its URL. The bounded request line
# prevents quotes in user-controlled headers from becoming response byte fields.
_LOG = re.compile(
    r"^(?P<owner>\S+) (?P<bucket>\S+) \[(?P<time>[^\]]+)\] \S+ \S+ "
    r"(?P<request>\S+) (?P<operation>\S+) (?P<key>\S+) "
    r'"(?:[A-Z]+ \S+ HTTP/[0-9.]+|-)" '
    r"(?P<status>[0-9]{3}) \S+ (?P<bytes>[0-9]+|-) \S+ \S+ \S+ "
    r'"[^\r\n]*" "[^\r\n]*" \S+ \S+ (?:SigV[24]|-) \S+ '
    r"(?:AuthHeader|QueryString|-) \S+ (?:TLSv[0-9.]+|-)"
    r"(?: \S+ (?:Yes|-)(?: (?P<region>[a-z0-9-]+))?)?(?: \S+)*$"
)


def parse_access_log(line: str, *, region: str) -> StorageAccessObservation:
    match = _LOG.fullmatch(line)
    if match is None:
        raise ValueError("S3 access record has an unsupported or malformed format")
    operation = match["operation"]
    if not re.fullmatch(r"[A-Z0-9_.]+", operation) or not re.fullmatch(
        r"[A-Za-z0-9_-]+", match["request"]
    ):
        raise ValueError("S3 access record has an invalid request identity")
    request_class = (
        StorageRequestClass.Read
        if operation in {"REST.GET.OBJECT", "REST.HEAD.OBJECT"}
        else StorageRequestClass.Write
        if operation
        in {
            "REST.PUT.OBJECT",
            "REST.POST.UPLOADS",
            "REST.PUT.PART",
            "REST.POST.UPLOAD",
            "REST.GET.BUCKET",
            "REST.COPY.OBJECT",
        }
        else StorageRequestClass.Delete
        if operation in {"REST.DELETE.OBJECT", "REST.POST.MULTI_OBJECT_DELETE"}
        else StorageRequestClass.Other
    )
    source = match["region"] or "-"
    try:
        return StorageAccessObservation(
            provider="aws",
            bucket=match["bucket"],
            key=unquote(match["key"]) if match["key"] != "-" else "",
            request_id=match["request"],
            operation=operation,
            request_class=request_class,
            occurred_at=datetime.strptime(match["time"], "%d/%b/%Y:%H:%M:%S %z"),
            status_code=int(match["status"]),
            response_bytes=int(match["bytes"]) if match["bytes"] != "-" else None,
            source_region="" if source == "-" else source,
            transfer_evidence=(
                StorageTransferEvidence.Unknown
                if source == "-"
                else StorageTransferEvidence.SameRegion
                if source == region
                else StorageTransferEvidence.OtherRegion
            ),
        )
    except (ValidationError, ValueError):
        raise ValueError("S3 access record has invalid observation fields") from None


class AwsStorageAccessSource:
    def __init__(self, settings: AwsStorageAccessSettings, store: S3ObjectStoreSettings) -> None:
        self.settings = settings
        self.store = store
        self.account = settings.queue_url.split("/")[3]
        suffix = "amazonaws.com.cn" if store.region_name.startswith("cn-") else "amazonaws.com"
        if (
            settings.queue_url.split(".")[1] != store.region_name
            or store.endpoint_url != f"https://s3.{store.region_name}.{suffix}"
            or store.access_key_id
            or store.secret_access_key
            or store.session_token
        ):
            raise ValueError(
                "S3 access ingestion requires regional platform storage and workload identity"
            )
        session = boto3.Session(region_name=store.region_name)
        if not is_boto3_client_factory(session):
            raise TypeError("AWS session lacks its client factory")
        s3 = session.client("s3")
        sqs = session.client("sqs")
        if not isinstance(s3, _S3) or not isinstance(sqs, _Sqs):
            raise TypeError("AWS clients lack storage access ingestion operations")
        self.s3 = s3
        self.sqs = sqs

    def receive(self) -> StorageAccessDelivery | None:
        try:
            response = _Messages.model_validate(
                self.sqs.receive_message(
                    QueueUrl=self.settings.queue_url,
                    MaxNumberOfMessages=1,
                    WaitTimeSeconds=0,
                    VisibilityTimeout=120,
                )
            )
            if not response.Messages:
                return None
            message = response.Messages[0]
            notification = _NOTIFICATION.validate_json(message.Body)
            if isinstance(notification, _TestNotification):
                if notification.Bucket != self.settings.bucket:
                    raise ValueError("S3 access test notification names an unexpected source")
                return StorageAccessDelivery((), message.ReceiptHandle)
            keys: list[str] = []
            for record in notification.Records:
                key = unquote_plus(record.s3.object.key)
                if (
                    record.awsRegion != self.store.region_name
                    or record.s3.bucket.name != self.settings.bucket
                    or not record.eventName.startswith("ObjectCreated:")
                    or not key.startswith("access/")
                ):
                    raise ValueError("S3 access notification names an unexpected source")
                keys.append(key)
            if not keys:
                raise ValueError("S3 access notification contains no log objects")
            return StorageAccessDelivery(tuple(keys), message.ReceiptHandle)
        except (BotoCoreError, ClientError, ValidationError, ValueError):
            raise UpstreamUnavailableError("S3 access notification could not be verified") from None

    def read(self, key: str) -> Iterator[StorageAccessObservation]:
        try:
            result = self.s3.get_object(
                Bucket=self.settings.bucket, Key=key, ExpectedBucketOwner=self.account
            )
            body = result["Body"]
            try:
                for raw in body.iter_lines():
                    observation = parse_access_log(
                        raw.decode("utf-8"), region=self.store.region_name
                    )
                    if (
                        observation.bucket != self.store.bucket
                        and not observation.bucket.startswith(
                            f"{self.store.workspace_bucket_prefix}-"
                        )
                    ):
                        raise ValueError("S3 access record names an unowned source bucket")
                    yield observation
            finally:
                body.close()
        except (BotoCoreError, ClientError, ValueError):
            raise UpstreamUnavailableError("S3 access log could not be verified") from None

    def renew(self, delivery: StorageAccessDelivery) -> None:
        try:
            self.sqs.change_message_visibility(
                QueueUrl=self.settings.queue_url,
                ReceiptHandle=delivery.receipt,
                VisibilityTimeout=120,
            )
        except (BotoCoreError, ClientError):
            raise UpstreamUnavailableError(
                "S3 access delivery lease could not be renewed"
            ) from None

    def acknowledge(self, delivery: StorageAccessDelivery) -> None:
        try:
            self.sqs.delete_message(
                QueueUrl=self.settings.queue_url, ReceiptHandle=delivery.receipt
            )
        except (BotoCoreError, ClientError):
            raise UpstreamUnavailableError("S3 access delivery could not be acknowledged") from None

    def close(self) -> None:
        try:
            self.s3.close()
        finally:
            self.sqs.close()
