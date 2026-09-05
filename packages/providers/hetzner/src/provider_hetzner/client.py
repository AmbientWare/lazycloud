from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from email.utils import parsedate_to_datetime

import httpx
from coordination.request_cooldown import RedisRequestCooldown
from pydantic import BaseModel, ConfigDict, Field, JsonValue, SecretStr


class HetznerError(RuntimeError):
    def __init__(self, code: str, status_code: int, *, retry_at: datetime | None = None) -> None:
        message = f"Hetzner request failed: {code} ({status_code})"
        if retry_at is not None:
            message += f"; retry at {retry_at.isoformat()}"
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retry_at = retry_at


def _rate_limit_retry_at(headers: httpx.Headers) -> datetime:
    now = datetime.now(UTC)
    deadlines: list[datetime] = []
    retry_after = headers.get("Retry-After")
    if retry_after is not None:
        try:
            deadlines.append(now + timedelta(seconds=max(float(retry_after), 0)))
        except (ValueError, OverflowError):
            try:
                parsed = parsedate_to_datetime(retry_after)
                if parsed.tzinfo is not None:
                    deadlines.append(parsed.astimezone(UTC))
            except (ValueError, TypeError, OverflowError):
                pass
    reset = headers.get("RateLimit-Reset")
    if reset is not None:
        with suppress(ValueError, OverflowError, OSError):
            deadlines.append(datetime.fromtimestamp(float(reset), tz=UTC))
    return max(
        (deadline for deadline in deadlines if deadline > now), default=now + timedelta(seconds=60)
    )


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class Location(ApiModel):
    name: str
    description: str
    network_zone: str


class Price(ApiModel):
    net: Decimal


class LocationPrice(ApiModel):
    location: str
    price_hourly: Price
    price_monthly: Price


class ServerType(ApiModel):
    id: int
    name: str
    cores: int
    memory: Decimal
    disk: int
    architecture: str
    cpu_type: str
    prices: tuple[LocationPrice, ...]
    locations: tuple[ServerTypeLocation, ...] = ()


class ServerTypeLocation(ApiModel):
    id: int
    name: str
    deprecation: Deprecation | None = None


class Deprecation(ApiModel):
    announced: datetime
    unavailable_after: datetime


class PublicIP(ApiModel):
    id: int
    ip: str
    blocked: bool = False


class PublicNet(ApiModel):
    ipv4: PublicIP | None = None
    ipv6: PublicIP | None = None


class Image(ApiModel):
    id: int
    status: str
    architecture: str
    disk_size: int = Field(ge=0)
    labels: dict[str, str] = Field(default_factory=dict)


class Server(ApiModel):
    id: int
    name: str
    status: str
    created: datetime
    labels: dict[str, str]
    location: Location
    server_type: ServerType
    public_net: PublicNet
    volumes: tuple[int, ...]
    image: Image | None = None


class PrimaryIP(ApiModel):
    id: int
    ip: str
    assignee_id: int | None = None
    auto_delete: bool
    labels: dict[str, str]


class Volume(ApiModel):
    id: int
    server: int | None = None
    labels: dict[str, str]


class Pagination(ApiModel):
    next_page: int | None = None


class Meta(ApiModel):
    pagination: Pagination


class ServersResponse(ApiModel):
    servers: tuple[Server, ...]
    meta: Meta


class TypesResponse(ApiModel):
    server_types: tuple[ServerType, ...]
    meta: Meta


class PrimaryIPPrice(ApiModel):
    type: str
    prices: tuple[LocationPrice, ...]


class Pricing(ApiModel):
    currency: str
    primary_ips: tuple[PrimaryIPPrice, ...]


class PricingResponse(ApiModel):
    pricing: Pricing


class ServerResponse(ApiModel):
    server: Server


class ImageResponse(ApiModel):
    image: Image


class ImagesResponse(ApiModel):
    images: tuple[Image, ...]
    meta: Meta


class PrimaryIPsResponse(ApiModel):
    primary_ips: tuple[PrimaryIP, ...]
    meta: Meta


class PrimaryIPResponse(ApiModel):
    primary_ip: PrimaryIP


class VolumeResponse(ApiModel):
    volume: Volume


class ErrorDetail(ApiModel):
    code: str


class ErrorResponse(ApiModel):
    error: ErrorDetail


@dataclass(frozen=True, slots=True)
class HetznerClient:
    token: SecretStr
    cooldown: RedisRequestCooldown | None = None

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        body: dict[str, JsonValue] | None = None,
    ) -> bytes:
        if self.cooldown is not None:
            retry_at = self.cooldown.blocked_until()
            if retry_at is not None:
                raise HetznerError("rate_limit_cooldown", 429, retry_at=retry_at)
        try:
            with httpx.Client(
                base_url="https://api.hetzner.cloud/v1",
                headers={"Authorization": f"Bearer {self.token.get_secret_value()}"},
                timeout=httpx.Timeout(20.0, connect=5.0),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = client.request(method, path, params=params, json=body)
        except httpx.HTTPError as exc:
            raise HetznerError("transport_unavailable", 0) from exc
        if not response.is_success:
            retry_at = None
            if response.status_code == 429:
                retry_at = _rate_limit_retry_at(response.headers)
                if self.cooldown is not None:
                    retry_at = self.cooldown.defer_until(retry_at)
            try:
                code = ErrorResponse.model_validate_json(response.content).error.code
            except ValueError:
                code = "invalid_response"
            raise HetznerError(code, response.status_code, retry_at=retry_at)
        return response.content

    def server_types(self) -> Iterator[ServerType]:
        page: int | None = 1
        while page is not None:
            response = TypesResponse.model_validate_json(
                self._request("GET", "/server_types", params={"page": page, "per_page": 50})
            )
            yield from response.server_types
            page = response.meta.pagination.next_page

    def pricing(self) -> Pricing:
        return PricingResponse.model_validate_json(self._request("GET", "/pricing")).pricing

    def servers(self, labels: str) -> Iterator[Server]:
        page: int | None = 1
        while page is not None:
            response = ServersResponse.model_validate_json(
                self._request(
                    "GET",
                    "/servers",
                    params={"page": page, "per_page": 50, "label_selector": labels},
                )
            )
            yield from response.servers
            page = response.meta.pagination.next_page

    def server(self, server_id: int) -> Server | None:
        try:
            data = self._request("GET", f"/servers/{server_id}")
        except HetznerError as exc:
            if exc.status_code == 404:
                return None
            raise
        return ServerResponse.model_validate_json(data).server

    def create_server(self, body: dict[str, JsonValue]) -> Server:
        return ServerResponse.model_validate_json(
            self._request("POST", "/servers", body=body)
        ).server

    def delete_server(self, server_id: int) -> None:
        self._request("DELETE", f"/servers/{server_id}")

    def image(self, image_id: int) -> Image:
        return ImageResponse.model_validate_json(self._request("GET", f"/images/{image_id}")).image

    def snapshots(self, labels: str) -> Iterator[Image]:
        page: int | None = 1
        while page is not None:
            response = ImagesResponse.model_validate_json(
                self._request(
                    "GET",
                    "/images",
                    params={
                        "page": page,
                        "per_page": 50,
                        "type": "snapshot",
                        "label_selector": labels,
                    },
                )
            )
            yield from response.images
            page = response.meta.pagination.next_page

    def primary_ips(self, labels: str) -> Iterator[PrimaryIP]:
        page: int | None = 1
        while page is not None:
            response = PrimaryIPsResponse.model_validate_json(
                self._request(
                    "GET",
                    "/primary_ips",
                    params={"page": page, "per_page": 50, "label_selector": labels},
                )
            )
            yield from response.primary_ips
            page = response.meta.pagination.next_page

    def primary_ip(self, ip_id: int) -> PrimaryIP:
        return PrimaryIPResponse.model_validate_json(
            self._request("GET", f"/primary_ips/{ip_id}")
        ).primary_ip

    def tag_primary_ip(self, address: PrimaryIP, labels: dict[str, str]) -> None:
        if address.assignee_id is None or str(address.assignee_id) != labels["lazycloud-server"]:
            raise ValueError("Hetzner primary IP is not attached to the expected server")
        merged = {**address.labels, **labels}
        if address.auto_delete and merged == address.labels:
            return
        self._request(
            "PUT",
            f"/primary_ips/{address.id}",
            body={"auto_delete": True, "labels": dict(merged)},
        )

    def delete_primary_ip(self, ip_id: int) -> None:
        self._request("DELETE", f"/primary_ips/{ip_id}")

    def volume(self, volume_id: int) -> Volume | None:
        try:
            data = self._request("GET", f"/volumes/{volume_id}")
        except HetznerError as exc:
            if exc.status_code == 404:
                return None
            raise
        return VolumeResponse.model_validate_json(data).volume
