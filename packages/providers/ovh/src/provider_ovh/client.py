from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha1
from time import time
from urllib.parse import quote
from uuid import UUID

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


class OvhError(RuntimeError):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"OVHcloud API request failed ({status_code})")
        self.status_code = status_code


class ApiModel(BaseModel):
    model_config = ConfigDict(
        extra="ignore",
        frozen=True,
        populate_by_name=True,
        hide_input_in_errors=True,
    )


class OvhApiCredentials(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    application_key: SecretStr
    application_secret: SecretStr
    consumer_key: SecretStr


class FlavorPlanCodes(ApiModel):
    hourly: str | None


class Flavor(ApiModel):
    id: str
    name: str
    region: str
    available: bool
    quota: int = Field(ge=0)
    vcpus: int = Field(gt=0)
    ram: int = Field(gt=0)
    disk: int = Field(ge=0)
    os_type: str = Field(alias="osType")
    plan_codes: FlavorPlanCodes = Field(alias="planCodes")


class IpAddress(ApiModel):
    ip: str
    version: int
    type: str | None


class AttachedVolume(ApiModel):
    id: str


class Instance(ApiModel):
    id: str
    name: str
    region: str
    flavor_id: str = Field(alias="flavorId")
    image_id: str | None = Field(alias="imageId")
    status: str
    ip_addresses: tuple[IpAddress, ...] = Field(alias="addresses")
    attached_volumes: tuple[AttachedVolume, ...] = Field(alias="attachedVolumes")


class Image(ApiModel):
    id: str
    name: str
    region: str
    status: str
    min_disk: int = Field(alias="minDisk", ge=0)
    min_ram: int = Field(alias="minRam", ge=0)


class Volume(ApiModel):
    id: str
    attached_to: tuple[str, ...] = Field(alias="attachedTo")


class SubOperation(ApiModel):
    id: str
    status: str
    resource_id: str | None = Field(alias="resourceId")


class Operation(ApiModel):
    id: str
    status: str
    resource_id: str | None = Field(alias="resourceId")
    sub_operations: tuple[SubOperation, ...] | None = Field(alias="subOperations")


class Snapshot(ApiModel):
    image_id: str = Field(alias="imageId")
    operation_id: str = Field(alias="operationId")


class CatalogLocale(ApiModel):
    currency_code: str = Field(alias="currencyCode")
    subsidiary: str


class ConsumptionConfiguration(ApiModel):
    prorata_unit: str = Field(alias="prorataUnit")
    billing_strategy: str = Field(alias="billingStrategy")


class CatalogPrice(ApiModel):
    price: int = Field(ge=0)
    commitment: int
    mode: str
    type: str
    capacities: tuple[str, ...]
    phase: int


class CatalogAddonFamily(ApiModel):
    mandatory: bool
    name: str
    addons: tuple[str, ...]


class CatalogPlan(ApiModel):
    plan_code: str = Field(alias="planCode")
    pricing_type: str = Field(alias="pricingType")
    consumption_configuration: ConsumptionConfiguration | None = Field(
        alias="consumptionConfiguration"
    )
    pricings: tuple[CatalogPrice, ...]
    addon_families: tuple[CatalogAddonFamily, ...] = Field(alias="addonFamilies")

    def hourly_micros(self, *, quantity: int = 1) -> int:
        consumption = self.consumption_configuration
        if (
            self.pricing_type != "consumption"
            or consumption is None
            or consumption.prorata_unit != "second"
            or consumption.billing_strategy != "sum"
        ):
            raise ValueError("OVHcloud offer must use per-second on-demand billing")
        prices = [
            price
            for price in self.pricings
            if price.mode == "default"
            and price.type == "consumption"
            and price.commitment == 0
            and price.phase == 0
            and price.capacities == ("consumption",)
        ]
        if len(prices) != 1 or any(family.mandatory for family in self.addon_families):
            raise ValueError("OVHcloud offer has ambiguous or incomplete on-demand pricing")
        # OVH's catalog integers are hundred-millionths of the account currency.
        return (prices[0].price * quantity + 99) // 100


class Catalog(ApiModel):
    locale: CatalogLocale
    addons: tuple[CatalogPlan, ...]

    def plan(self, code: str) -> CatalogPlan:
        found = [plan for plan in self.addons if plan.plan_code == code]
        if len(found) != 1:
            raise ValueError("OVHcloud catalog has no unique requested pricing plan")
        return found[0]


@dataclass(frozen=True, slots=True)
class OvhClient:
    application_key: SecretStr
    application_secret: SecretStr
    consumer_key: SecretStr
    project_id: str

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        body: dict[str, JsonValue] | None = None,
        authenticated: bool = True,
    ) -> bytes:
        try:
            with httpx.Client(
                base_url="https://api.us.ovhcloud.com/1.0",
                timeout=httpx.Timeout(30, connect=5),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                request = client.build_request(method, path, params=params, json=body)
                if authenticated:
                    timestamp = str(int(time()))
                    signature = "+".join(
                        (
                            self.application_secret.get_secret_value(),
                            self.consumer_key.get_secret_value(),
                            method,
                            str(request.url),
                            request.content.decode(),
                            timestamp,
                        )
                    )
                    request.headers.update(
                        {
                            "X-Ovh-Application": self.application_key.get_secret_value(),
                            "X-Ovh-Consumer": self.consumer_key.get_secret_value(),
                            "X-Ovh-Timestamp": timestamp,
                            "X-Ovh-Signature": "$1$" + sha1(signature.encode()).hexdigest(),
                        }
                    )
                response = client.send(request)
        except httpx.HTTPError:
            raise OvhError(0) from None
        if not response.is_success:
            raise OvhError(response.status_code)
        return response.content

    def _project_path(self, suffix: str) -> str:
        return f"/cloud/project/{quote(self.project_id, safe='')}/{suffix}"

    def flavors(self, region: str) -> tuple[Flavor, ...]:
        return TypeAdapter(tuple[Flavor, ...]).validate_json(
            self._request("GET", self._project_path("flavor"), params={"region": region})
        )

    def catalog(self) -> Catalog:
        catalog = Catalog.model_validate_json(
            self._request(
                "GET",
                "/order/catalog/public/cloud",
                params={"ovhSubsidiary": "US"},
                authenticated=False,
            )
        )
        if catalog.locale.currency_code != "USD" or catalog.locale.subsidiary != "US":
            raise ValueError("OVHcloud provider requires the US dollar catalog")
        return catalog

    def instances(self, region: str) -> tuple[Instance, ...]:
        data = self._request("GET", self._project_path(f"region/{quote(region, safe='')}/instance"))
        try:
            return TypeAdapter(tuple[Instance, ...]).validate_json(data)
        except ValidationError:
            raise OvhError(502) from None

    def instance(self, instance_id: str, region: str) -> Instance | None:
        try:
            data = self._request(
                "GET",
                self._project_path(f"region/{quote(region, safe='')}/instance/{UUID(instance_id)}"),
            )
        except OvhError as exc:
            if exc.status_code == 404:
                return None
            raise
        return _instance_response(data)

    def create_instance(self, region: str, body: dict[str, JsonValue]) -> Operation:
        return Operation.model_validate_json(
            self._request(
                "POST",
                self._project_path(f"region/{quote(region, safe='')}/instance"),
                body=body,
            )
        )

    def operation(self, operation_id: str) -> Operation:
        return Operation.model_validate_json(
            self._request(
                "GET",
                self._project_path(f"operation/{quote(operation_id, safe='')}"),
            )
        )

    def delete_instance(self, instance_id: str) -> None:
        try:
            self._request("DELETE", self._project_path(f"instance/{UUID(instance_id)}"))
        except OvhError as exc:
            if exc.status_code != 404:
                raise

    def image(self, image_id: str) -> Image:
        return Image.model_validate_json(
            self._request("GET", self._project_path(f"image/{UUID(image_id)}"))
        )

    def images(self, region: str) -> tuple[Image, ...]:
        return TypeAdapter(tuple[Image, ...]).validate_json(
            self._request("GET", self._project_path("image"), params={"region": region})
        )

    def snapshots(self, region: str) -> tuple[Image, ...]:
        return TypeAdapter(tuple[Image, ...]).validate_json(
            self._request("GET", self._project_path("snapshot"), params={"region": region})
        )

    def create_snapshot(self, region: str, instance_id: str, name: str) -> Snapshot:
        return Snapshot.model_validate_json(
            self._request(
                "POST",
                self._project_path(
                    f"region/{quote(region, safe='')}/instance/{UUID(instance_id)}/snapshot"
                ),
                body={"snapshotName": name},
            )
        )

    def delete_snapshot(self, image_id: str) -> None:
        try:
            self._request("DELETE", self._project_path(f"snapshot/{UUID(image_id)}"))
        except OvhError as exc:
            if exc.status_code != 404:
                raise

    def volumes(self) -> tuple[Volume, ...]:
        return TypeAdapter(tuple[Volume, ...]).validate_json(
            self._request("GET", self._project_path("volume"))
        )


def _instance_response(data: bytes) -> Instance:
    try:
        return Instance.model_validate_json(data)
    except ValidationError:
        # Instance responses may contain a rescue password outside our contract.
        raise OvhError(502) from None
