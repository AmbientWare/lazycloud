"""EC2's instance metadata service, reached without the AWS SDK.

The agent reads interruption notices and disk attachments here on every machine,
so this module stays free of botocore and the instance catalog.
"""

from __future__ import annotations

import http.client
from dataclasses import dataclass
from typing import Protocol

AWS_IMDS_HOST = "169.254.169.254"
AWS_IMDS_TOKEN_TTL_SECONDS = 21_600
AWS_IMDS_TIMEOUT_SECONDS = 2.0
AWS_IMDS_MAX_TEXT_BYTES = 2048


class AwsProviderNodeProofError(RuntimeError):
    """Raised when strict EC2 instance identity proof creation fails."""


@dataclass(frozen=True, slots=True)
class AwsInstanceMetadataResponse:
    status_code: int
    body: bytes


class AwsInstanceMetadataTransport(Protocol):
    def request(
        self,
        *,
        method: str,
        path: str,
        headers: dict[str, str],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> AwsInstanceMetadataResponse: ...


@dataclass(frozen=True, slots=True)
class AwsDirectInstanceMetadataTransport:
    host: str = AWS_IMDS_HOST

    def request(
        self,
        *,
        method: str,
        path: str,
        headers: dict[str, str],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> AwsInstanceMetadataResponse:
        if self.host != AWS_IMDS_HOST:
            raise AwsProviderNodeProofError("EC2 metadata host must use the link-local endpoint")
        if method not in {"GET", "PUT"} or not path.startswith("/latest/"):
            raise AwsProviderNodeProofError("invalid EC2 metadata request")
        connection = http.client.HTTPConnection(self.host, timeout=timeout_seconds)
        try:
            connection.request(method, path, headers=headers)
            response = connection.getresponse()
            content_length = response.getheader("Content-Length")
            if content_length is not None:
                try:
                    declared_length = int(content_length)
                except ValueError as exc:
                    raise AwsProviderNodeProofError(
                        "EC2 metadata returned an invalid content length"
                    ) from exc
                if declared_length < 0 or declared_length > max_response_bytes:
                    raise AwsProviderNodeProofError("EC2 metadata response exceeds size limit")
            body = response.read(max_response_bytes + 1)
        except (OSError, http.client.HTTPException) as exc:
            raise AwsProviderNodeProofError("EC2 metadata service is unavailable") from exc
        finally:
            connection.close()
        if len(body) > max_response_bytes:
            raise AwsProviderNodeProofError("EC2 metadata response exceeds size limit")
        return AwsInstanceMetadataResponse(status_code=response.status, body=body)


__all__ = [
    "AWS_IMDS_HOST",
    "AWS_IMDS_MAX_TEXT_BYTES",
    "AWS_IMDS_TIMEOUT_SECONDS",
    "AWS_IMDS_TOKEN_TTL_SECONDS",
    "AwsDirectInstanceMetadataTransport",
    "AwsInstanceMetadataResponse",
    "AwsInstanceMetadataTransport",
    "AwsProviderNodeProofError",
]
