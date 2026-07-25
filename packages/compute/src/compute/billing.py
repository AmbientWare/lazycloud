from __future__ import annotations

import http.client
import json
import ssl
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from urllib.parse import urljoin, urlsplit

from pydantic import Field, JsonValue, TypeAdapter
from shared.contracts import ContractModel

from observability import managed_billing

_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])


class ManagedBillingErrorCode(StrEnum):
    BillingUnavailable = "billing_unavailable"
    InsufficientCredits = "insufficient_credits"


class BillingCreditRequest(ContractModel):
    workspace_id: str
    pool_name: str
    required_cents: int = 0
    quantity: int = 0
    estimated_hourly_cost_micros: int = 0
    estimated_committed_micros: int = 0


class BillingDecision(ContractModel):
    ok: bool = True
    error_code: str = ""
    message: str = ""
    available_cents: int = 0
    required_cents: int = 0


class ManagedUsage(ContractModel):
    idempotency_key: str
    workspace_id: str
    pool_name: str
    reservation_id: str
    provider: str
    cloud: str = ""
    provider_instance_id: str = ""
    machine_id: str = ""
    gpu: str = ""
    gpu_count: int = 0
    node_count: int = 0
    cpu_millicores: int = 0
    memory_mb: int = 0
    storage_mb: int = 0
    network_ingress_bytes: int = 0
    network_egress_bytes: int = 0
    hourly_cost_micros: int = 0
    duration_seconds: float = 0.0
    cost_cents: float = 0.0
    start_at: datetime
    end_at: datetime


class ManagedComputeBillingClient(Protocol):
    def check_launch_credit(self, request: BillingCreditRequest) -> BillingDecision: ...

    def check_balance(self, workspace_id: str) -> BillingDecision: ...

    def record_usage(self, usage: ManagedUsage) -> None: ...


class CustomerCloudManagementFeeRates(ContractModel):
    vcpu_hourly_micros: int = Field(ge=0)
    memory_gib_hourly_micros: int = Field(ge=0)


@dataclass(slots=True)
class NoopManagedComputeBilling:
    minimum_credit_cents: int = 0

    def check_launch_credit(self, request: BillingCreditRequest) -> BillingDecision:
        required = request.required_cents or self.minimum_credit_cents
        return BillingDecision(ok=True, required_cents=required, available_cents=required)

    def check_balance(self, workspace_id: str) -> BillingDecision:
        del workspace_id
        return BillingDecision(
            ok=True,
            required_cents=self.minimum_credit_cents,
            available_cents=self.minimum_credit_cents,
        )

    def record_usage(self, usage: ManagedUsage) -> None:
        del usage


@dataclass(slots=True)
class DisabledManagedComputeBilling:
    def check_launch_credit(self, request: BillingCreditRequest) -> BillingDecision:
        return BillingDecision(
            ok=False,
            error_code=ManagedBillingErrorCode.BillingUnavailable.value,
            required_cents=request.required_cents,
            message="managed compute billing is not configured",
        )

    def check_balance(self, workspace_id: str) -> BillingDecision:
        del workspace_id
        return BillingDecision(
            ok=False,
            error_code=ManagedBillingErrorCode.BillingUnavailable.value,
            message="managed compute billing is not configured",
        )

    def record_usage(self, usage: ManagedUsage) -> None:
        del usage
        raise RuntimeError("managed compute billing is not configured")


@dataclass(slots=True)
class HttpManagedComputeBilling:
    settings: managed_billing.ManagedBillingSettings

    def check_launch_credit(self, request: BillingCreditRequest) -> BillingDecision:
        required_cents = request.required_cents or self.settings.minimum_credit_cents
        payload = _JSON_OBJECT_ADAPTER.validate_json(request.model_dump_json())
        payload["required_cents"] = required_cents
        decision = self._post("launch-check/", payload)
        result = BillingDecision.model_validate(decision or {})
        if result.required_cents <= 0:
            result.required_cents = required_cents
        return result

    def check_balance(self, workspace_id: str) -> BillingDecision:
        decision = self._post("balance/", {"workspace_id": workspace_id})
        result = BillingDecision.model_validate(decision or {})
        if result.required_cents <= 0:
            result.required_cents = self.settings.minimum_credit_cents
        return result

    def record_usage(self, usage: ManagedUsage) -> None:
        result = self._post(
            "usage/",
            _JSON_OBJECT_ADAPTER.validate_json(usage.model_dump_json()),
        )
        if result and result.get("ok") is False:
            msg = str(result.get("message") or "managed compute usage rejected")
            raise RuntimeError(msg)

    def _post(
        self,
        path: str,
        payload: Mapping[str, JsonValue],
    ) -> dict[str, JsonValue]:
        if not self.settings.endpoint:
            raise RuntimeError("managed compute billing is not configured")
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            **self.settings.headers,
        }
        if self.settings.auth_token:
            headers["Authorization"] = f"Bearer {self.settings.auth_token}"
        url = urljoin(self.settings.endpoint.rstrip("/") + "/", path)
        parsed = urlsplit(url)
        if parsed.hostname is None:
            raise RuntimeError("managed compute billing endpoint has no host")
        target = parsed.path or "/"
        if parsed.query:
            target = f"{target}?{parsed.query}"
        connection: http.client.HTTPConnection
        if parsed.scheme == "https":
            connection = http.client.HTTPSConnection(
                parsed.hostname,
                parsed.port,
                timeout=self.settings.timeout_seconds,
                context=ssl.create_default_context(),
            )
        else:
            connection = http.client.HTTPConnection(
                parsed.hostname,
                parsed.port,
                timeout=self.settings.timeout_seconds,
            )
        try:
            connection.request("POST", target, body=body, headers=headers)
            response = connection.getresponse()
            data = response.read((1 << 20) + 1)
            if len(data) > 1 << 20:
                raise RuntimeError("managed compute billing response exceeds 1 MiB")
            if response.status >= 400:
                detail = data.decode("utf-8", errors="replace")
                msg = (
                    f"managed compute billing {path} failed with status {response.status}: {detail}"
                )
                raise RuntimeError(msg)
        except (OSError, http.client.HTTPException) as exc:
            raise RuntimeError(f"managed compute billing {path} failed: {exc}") from exc
        finally:
            connection.close()
        if not data.strip():
            return {}
        try:
            return _JSON_OBJECT_ADAPTER.validate_json(data)
        except ValueError as exc:
            raise RuntimeError("managed compute billing response must be a JSON object") from exc


def managed_billing_client(
    settings: managed_billing.ManagedBillingSettings | None,
) -> ManagedComputeBillingClient:
    current = settings or managed_billing.ManagedBillingSettings()
    if current.mode is managed_billing.ManagedBillingMode.Disabled or (
        current.required and not current.endpoint
    ):
        return DisabledManagedComputeBilling()
    if current.mode is managed_billing.ManagedBillingMode.Http or current.endpoint:
        return HttpManagedComputeBilling(current)
    return NoopManagedComputeBilling(minimum_credit_cents=current.minimum_credit_cents)


def managed_cost_cents(hourly_cost_micros: int, duration_seconds: float) -> float:
    if hourly_cost_micros <= 0 or duration_seconds <= 0:
        return 0.0
    return hourly_cost_micros * (duration_seconds / 3600) / 10_000
