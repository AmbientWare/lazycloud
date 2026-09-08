from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SecretStr,
    TypeAdapter,
    ValidationError,
)


class HyperstackError(RuntimeError):
    def __init__(self, code: str, status_code: int) -> None:
        super().__init__(f"Hyperstack request failed: {code} ({status_code})")
        self.code = code
        self.status_code = status_code


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class ApiResponse(ApiModel):
    status: bool


class Flavor(ApiModel):
    name: str
    cpu: int = Field(gt=0)
    ram: Decimal = Field(gt=0)
    disk: int = Field(ge=0)
    ephemeral: int = Field(ge=0)
    gpu: str
    gpu_count: int = Field(ge=0)


class AvailableFlavor(Flavor):
    stock_available: bool
    region_name: str


class FlavorGroup(ApiModel):
    flavors: tuple[AvailableFlavor, ...]


class FlavorsResponse(ApiResponse):
    data: tuple[FlavorGroup, ...]


class Environment(ApiModel):
    name: str
    region: str


class EnvironmentsResponse(ApiResponse):
    environments: tuple[Environment, ...]


class Image(ApiModel):
    id: int = Field(gt=0)
    name: str
    region_name: str
    is_public: bool


class ImageGroup(ApiModel):
    images: tuple[Image, ...]


class ImagesResponse(ApiResponse):
    images: tuple[ImageGroup, ...]


class Price(ApiModel):
    name: str
    value: Decimal = Field(ge=0)
    start_time: datetime | None = None
    end_time: datetime | None = None


class AttachedVolume(ApiModel):
    id: int = Field(gt=0)


class VolumeAttachment(ApiModel):
    volume: AttachedVolume


class Server(ApiModel):
    id: int = Field(gt=0)
    name: str
    status: str
    created_at: datetime
    environment: Environment
    flavor: Flavor
    labels: tuple[str, ...]
    floating_ip: str | None = None
    contract_id: int | None = None
    volume_attachments: tuple[VolumeAttachment, ...]


class ServersResponse(ApiResponse):
    instances: tuple[Server, ...]
    count: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(gt=0)


class CreatedServersResponse(ApiResponse):
    instances: tuple[Server, ...]


class Keypair(ApiModel):
    id: int = Field(gt=0)
    name: str
    environment: Environment
    public_key: str


class KeypairsResponse(ApiResponse):
    keypairs: tuple[Keypair, ...]
    count: int
    page: int
    page_size: int


class ImportedKeypairResponse(ApiResponse):
    keypair: Keypair


class Snapshot(ApiModel):
    id: int = Field(gt=0)
    name: str
    vm_id: int = Field(gt=0)
    status: str
    is_image: bool


class SnapshotResponse(ApiResponse):
    snapshot: Snapshot


class SnapshotsResponse(ApiResponse):
    snapshots: tuple[Snapshot, ...]
    count: int
    page: int
    page_size: int


class CreatedImageResponse(ApiResponse):
    id: int = Field(gt=0)


class ImageResponse(ApiModel):
    image: Image


class ServerResponse(ApiResponse):
    instance: Server


@dataclass(frozen=True, slots=True)
class HyperstackClient:
    api_key: SecretStr

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        body: dict[str, JsonValue] | None = None,
    ) -> httpx.Response:
        try:
            with httpx.Client(timeout=30) as http:
                response = http.request(
                    method,
                    f"https://infrahub-api.nexgencloud.com/v1{path}",
                    headers={"api_key": self.api_key.get_secret_value()},
                    params=params,
                    json=body,
                )
        except httpx.TransportError:
            raise HyperstackError("transport_unavailable", 0) from None
        if response.is_error:
            raise HyperstackError("http_error", response.status_code)
        return response

    def _response[T: ApiResponse](self, response: httpx.Response, model: type[T]) -> T:
        try:
            value = model.model_validate_json(response.content)
        except ValidationError:
            raise HyperstackError("invalid_response", response.status_code) from None
        if not value.status:
            raise HyperstackError("operation_failed", response.status_code)
        return value

    def flavors(self, region: str) -> Iterator[AvailableFlavor]:
        result = self._response(
            self._request("GET", "/core/flavors", params={"region": region}), FlavorsResponse
        )
        for group in result.data:
            yield from group.flavors

    def prices(self) -> tuple[Price, ...]:
        try:
            return TypeAdapter(tuple[Price, ...]).validate_json(
                self._request("GET", "/pricebook").content
            )
        except ValidationError:
            raise HyperstackError("invalid_pricebook", 200) from None

    def environments(self) -> tuple[Environment, ...]:
        return self._response(
            self._request("GET", "/core/environments"), EnvironmentsResponse
        ).environments

    def images(self, *, region: str, search: str, include_public: bool = False) -> Iterator[Image]:
        page = 1
        seen: set[int] = set()
        while True:
            result = self._response(
                self._request(
                    "GET",
                    "/core/images",
                    params={
                        "region": region,
                        "search": search,
                        "include_public": str(include_public).lower(),
                        "page": str(page),
                        "per_page": "100",
                    },
                ),
                ImagesResponse,
            )
            images = tuple(image for group in result.images for image in group.images)
            if any(image.id in seen for image in images):
                raise HyperstackError("invalid_image_pagination", 200)
            for image in images:
                seen.add(image.id)
                yield image
            if len(images) < 100:
                return
            page += 1

    def servers(self, *, search: str) -> Iterator[Server]:
        page = 1
        seen: set[int] = set()
        while True:
            result = self._response(
                self._request(
                    "GET",
                    "/core/virtual-machines",
                    params={"page": str(page), "pageSize": "100", "search": search},
                ),
                ServersResponse,
            )
            if result.page != page or any(server.id in seen for server in result.instances):
                raise HyperstackError("invalid_pagination", 200)
            if not result.instances and len(seen) < result.count:
                raise HyperstackError("incomplete_pagination", 200)
            for server in result.instances:
                seen.add(server.id)
                yield server
            if page * result.page_size >= result.count:
                return
            page += 1

    def server(self, name: str) -> Server | None:
        matches = [server for server in self.servers(search=name) if server.name == name]
        if len(matches) > 1:
            raise HyperstackError("ambiguous_instance_name", 200)
        return matches[0] if matches else None

    def create_server(self, body: dict[str, JsonValue]) -> Server:
        result = self._response(
            self._request("POST", "/core/virtual-machines", body=body), CreatedServersResponse
        )
        if len(result.instances) != 1:
            raise HyperstackError("unexpected_create_count", 200)
        return result.instances[0]

    def delete_server(self, server_id: int) -> None:
        self._delete(f"/core/virtual-machines/{server_id}")

    def server_by_id(self, server_id: int) -> Server | None:
        try:
            return self._response(
                self._request("GET", f"/core/virtual-machines/{server_id}"), ServerResponse
            ).instance
        except HyperstackError as exc:
            if exc.status_code == 404:
                return None
            raise

    def keypairs(self) -> Iterator[Keypair]:
        page = 1
        seen: set[int] = set()
        while True:
            result = self._response(
                self._request(
                    "GET", "/core/keypairs", params={"page": str(page), "pageSize": "100"}
                ),
                KeypairsResponse,
            )
            if result.page != page or any(key.id in seen for key in result.keypairs):
                raise HyperstackError("invalid_keypair_pagination", 200)
            for key in result.keypairs:
                seen.add(key.id)
                yield key
            if page * result.page_size >= result.count:
                return
            if not result.keypairs:
                raise HyperstackError("incomplete_keypair_pagination", 200)
            page += 1

    def import_keypair(self, *, name: str, environment_name: str, public_key: str) -> Keypair:
        return self._response(
            self._request(
                "POST",
                "/core/keypairs",
                body={
                    "name": name,
                    "environment_name": environment_name,
                    "public_key": public_key,
                },
            ),
            ImportedKeypairResponse,
        ).keypair

    def delete_keypair(self, keypair_id: int) -> None:
        self._delete(f"/core/keypair/{keypair_id}")

    def snapshots(self, *, search: str) -> Iterator[Snapshot]:
        page = 1
        seen: set[int] = set()
        while True:
            result = self._response(
                self._request(
                    "GET",
                    "/core/snapshots",
                    params={"page": str(page), "pageSize": "100", "search": search},
                ),
                SnapshotsResponse,
            )
            if result.page != page or any(snapshot.id in seen for snapshot in result.snapshots):
                raise HyperstackError("invalid_snapshot_pagination", 200)
            for snapshot in result.snapshots:
                seen.add(snapshot.id)
                yield snapshot
            if page * result.page_size >= result.count:
                return
            if not result.snapshots:
                raise HyperstackError("incomplete_snapshot_pagination", 200)
            page += 1

    def snapshot(self, snapshot_id: int) -> Snapshot | None:
        try:
            return self._response(
                self._request("GET", f"/core/snapshots/{snapshot_id}"), SnapshotResponse
            ).snapshot
        except HyperstackError as exc:
            if exc.status_code == 404:
                return None
            raise

    def create_snapshot(self, server_id: int, *, name: str, labels: tuple[str, ...]) -> Snapshot:
        return self._response(
            self._request(
                "POST",
                f"/core/virtual-machines/{server_id}/snapshots",
                body={
                    "name": name,
                    "description": "Verified LazyCloud GPU host image",
                    "labels": list(labels),
                },
            ),
            SnapshotResponse,
        ).snapshot

    def create_image(self, snapshot_id: int, *, name: str, labels: tuple[str, ...]) -> int:
        return self._response(
            self._request(
                "POST",
                f"/core/snapshots/{snapshot_id}/image",
                body={
                    "name": name,
                    "labels": list(labels),
                },
            ),
            CreatedImageResponse,
        ).id

    def image(self, image_id: int) -> Image | None:
        try:
            response = self._request("GET", f"/core/images/{image_id}")
            return ImageResponse.model_validate_json(response.content).image
        except HyperstackError as exc:
            if exc.status_code == 404:
                return None
            raise
        except ValidationError:
            raise HyperstackError("invalid_image_response", 200) from None

    def delete_snapshot(self, snapshot_id: int) -> None:
        self._delete(f"/core/snapshots/{snapshot_id}")

    def delete_image(self, image_id: int) -> None:
        self._delete(f"/core/images/{image_id}")

    def _delete(self, path: str) -> None:
        try:
            response = self._request("DELETE", path)
        except HyperstackError as exc:
            if exc.status_code == 404:
                return
            raise
        self._response(response, ApiResponse)
