from __future__ import annotations

from _thread import LockType
from dataclasses import dataclass, field
from threading import Lock
from types import MappingProxyType
from typing import Protocol

from compute.providers import DirectMachineProvider, DirectMachineProviderRegistry
from networking.settings import (
    BackendRouteSettings,
    ProviderNetworkClass,
    TailnetControlSettings,
    TailnetRuntimeSettings,
    validate_provider_network_configuration,
)
from provider_aws import AwsProvider, AwsProviderSettings
from pydantic import JsonValue, TypeAdapter
from shared.provider_config import ProviderConfig

_PROVIDER_CONFIG_ADAPTER = TypeAdapter(dict[str, JsonValue])


class ProviderConfigLoader(Protocol):
    def list(
        self,
        *,
        enabled: bool | None = None,
        workspace: str = "default",
    ) -> list[ProviderConfig]: ...

    def list_for_workspace_deletion(
        self,
        workspace_id: str,
        *,
        enabled: bool | None = None,
    ) -> list[ProviderConfig]: ...


@dataclass(frozen=True, slots=True)
class _CachedProviderClient:
    revision: str
    client: DirectMachineProvider


@dataclass(slots=True)
class ConfiguredComputeProviderRegistry(DirectMachineProviderRegistry):
    provider_service: ProviderConfigLoader
    gateway_origin: str
    internal_origin: str
    tailnet_runtime: TailnetRuntimeSettings
    tailnet_control: TailnetControlSettings
    backend_route: BackendRouteSettings
    _cache: dict[tuple[str, str], _CachedProviderClient] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _cache_lock: LockType = field(
        default_factory=Lock,
        init=False,
        repr=False,
    )

    def snapshot(self, workspace: str) -> MappingProxyType[str, DirectMachineProvider]:
        records = self.provider_service.list(enabled=True, workspace=workspace)
        return self._snapshot_records(workspace, records)

    def snapshot_for_workspace_deletion(
        self,
        workspace_id: str,
    ) -> MappingProxyType[str, DirectMachineProvider]:
        records = self.provider_service.list_for_workspace_deletion(
            workspace_id,
            enabled=True,
        )
        return self._snapshot_records(workspace_id, records)

    def _snapshot_records(
        self,
        workspace_id: str,
        records: list[ProviderConfig],
    ) -> MappingProxyType[str, DirectMachineProvider]:
        workspace_cache: dict[tuple[str, str], _CachedProviderClient] = {}
        clients: dict[str, DirectMachineProvider] = {}
        with self._cache_lock:
            for record in records:
                cache_key = (workspace_id, record.name)
                revision = record.model_dump_json()
                cached = self._cache.get(cache_key)
                if cached is None or cached.revision != revision:
                    client = _provider_client_from_record(
                        record,
                        gateway_origin=self.gateway_origin,
                        internal_origin=self.internal_origin,
                        tailnet_runtime=self.tailnet_runtime,
                        tailnet_control=self.tailnet_control,
                        backend_route=self.backend_route,
                    )
                    cached = _CachedProviderClient(revision=revision, client=client)
                workspace_cache[cache_key] = cached
                clients[record.name] = cached.client
            self._cache = {
                key: value for key, value in self._cache.items() if key[0] != workspace_id
            }
            self._cache.update(workspace_cache)
        return MappingProxyType(dict(sorted(clients.items())))


def configured_compute_provider_registry(
    provider_service: ProviderConfigLoader,
    *,
    gateway_origin: str,
    internal_origin: str,
    tailnet_runtime: TailnetRuntimeSettings,
    tailnet_control: TailnetControlSettings,
    backend_route: BackendRouteSettings,
) -> ConfiguredComputeProviderRegistry:
    return ConfiguredComputeProviderRegistry(
        provider_service=provider_service,
        gateway_origin=gateway_origin,
        internal_origin=internal_origin,
        tailnet_runtime=tailnet_runtime,
        tailnet_control=tailnet_control,
        backend_route=backend_route,
    )


def _provider_client_from_record(
    record: ProviderConfig,
    *,
    gateway_origin: str,
    internal_origin: str,
    tailnet_runtime: TailnetRuntimeSettings,
    tailnet_control: TailnetControlSettings,
    backend_route: BackendRouteSettings,
) -> DirectMachineProvider:
    validate_provider_network_configuration(
        ProviderNetworkClass.Remote,
        gateway_origin=gateway_origin,
        internal_origin=internal_origin,
        runtime=tailnet_runtime,
        control=tailnet_control,
        backend_route=backend_route,
    )
    config = _provider_client_config(
        record,
        gateway_origin=gateway_origin,
    )
    return AwsProvider(AwsProviderSettings.model_validate(config))


def _provider_client_config(
    record: ProviderConfig,
    *,
    gateway_origin: str,
) -> dict[str, JsonValue]:
    resolved = _PROVIDER_CONFIG_ADAPTER.validate_python(record.config)
    configured_gateway = resolved.get("gateway_url")
    if configured_gateway is not None and configured_gateway != gateway_origin:
        raise ValueError(
            f"provider {record.name!r} gateway_url must match the control-plane gateway origin"
        )
    resolved["gateway_url"] = gateway_origin
    return resolved


__all__ = [
    "ConfiguredComputeProviderRegistry",
    "ProviderConfigLoader",
    "configured_compute_provider_registry",
]
