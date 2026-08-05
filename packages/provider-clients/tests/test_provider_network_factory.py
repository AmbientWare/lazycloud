from __future__ import annotations

from dataclasses import dataclass

import pytest
from networking.settings import (
    BackendRouteSettings,
    TailnetControlSettings,
    TailnetRuntimeSettings,
)
from provider_aws import AwsProvider
from provider_clients.factory import (
    _provider_client_from_record,
    configured_compute_provider_registry,
)
from pydantic import SecretStr
from shared.provider_config import ProviderConfig, ProviderKind


@dataclass(slots=True)
class _ProviderLoader:
    records: list[ProviderConfig]
    calls: int = 0

    def list(
        self,
        *,
        enabled: bool | None = None,
        workspace: str = "default",
    ) -> list[ProviderConfig]:
        self.calls += 1
        assert workspace
        if enabled is None:
            return list(self.records)
        return [record for record in self.records if record.enabled is enabled]

    def list_for_workspace_deletion(
        self,
        workspace_id: str,
        *,
        enabled: bool | None = None,
    ) -> list[ProviderConfig]:
        assert workspace_id
        return self.list(enabled=enabled, workspace=workspace_id)


def _network() -> tuple[
    TailnetRuntimeSettings,
    TailnetControlSettings,
    BackendRouteSettings,
]:
    return (
        TailnetRuntimeSettings(),
        TailnetControlSettings(
            oauth_client_id="oauth-client-id",
            oauth_client_secret=SecretStr("oauth-client-secret"),
        ),
        BackendRouteSettings(auth_key=SecretStr("r" * 32)),
    )


def _client(record: ProviderConfig) -> AwsProvider:
    runtime, control, backend_route = _network()
    client = _provider_client_from_record(
        record,
        gateway_origin="https://control.example.com",
        internal_origin="http://lazycloud-control-plane.tailnet-example.ts.net:9000",
        tailnet_runtime=runtime,
        tailnet_control=control,
        backend_route=backend_route,
    )
    assert isinstance(client, AwsProvider)
    return client


def test_factory_rejects_record_gateway_different_from_canonical_origin() -> None:
    record = ProviderConfig(
        name="aws-primary",
        kind=ProviderKind.Aws,
        config={"gateway_url": "https://other.example.com"},
    )

    with pytest.raises(ValueError, match="must match the control-plane gateway origin"):
        _client(record)


def test_registry_reads_one_durable_aws_snapshot_per_resolution() -> None:
    runtime, control, backend_route = _network()
    loader = _ProviderLoader(
        [
            ProviderConfig(
                name="aws-primary",
                kind=ProviderKind.Aws,
                config={"region": "us-east-1"},
            )
        ]
    )
    registry = configured_compute_provider_registry(
        loader,
        gateway_origin="https://control.example.com",
        internal_origin="http://lazycloud-control-plane.tailnet-example.ts.net:9000",
        tailnet_runtime=runtime,
        tailnet_control=control,
        backend_route=backend_route,
    )

    first = registry.snapshot("default")
    assert list(first) == ["aws-primary"]
    assert loader.calls == 1

    second = registry.snapshot("default")
    assert second["aws-primary"] is first["aws-primary"]
    assert loader.calls == 2

    loader.records = [loader.records[0].model_copy(update={"priority": 50})]
    updated = registry.snapshot("default")
    assert updated["aws-primary"] is not first["aws-primary"]
    assert loader.calls == 3

    loader.records = []
    assert not registry.snapshot("default")
    assert loader.calls == 4
