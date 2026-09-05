from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from urllib.parse import urlparse

from compute.node_bootstrap import (
    NodeBootstrapProfile,
    NodeBootstrapSettings,
    node_bootstrap_script,
)
from compute.offers import ComputeOffer, pooled_cloud_offer, recorded_unit_offer
from compute.provider_launches import ProviderNodeLaunchCredential, ProviderNodeLaunchCredentials
from compute.providers import (
    ProviderCapacityPhase,
    ProviderMachineStatus,
    ProviderUnitInstance,
    ProviderUnitRequest,
    ProviderUnitSnapshot,
)
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from shared.compute_policy import ComputeUnitProviderState, ComputeUnitRecord
from shared.timestamps import utc_now

from provider_hetzner.client import HetznerClient, HetznerError, Server
from provider_hetzner.identity import provider_label

_MANAGED_LABEL = "lazycloud-managed"
_UNIT_LABEL = "lazycloud-unit"
_GENERATION_LABEL = "lazycloud-generation"
_RELEASE_LABEL = "lazycloud-release"
_SERVER_LABEL = "lazycloud-server"
_LAUNCH_LABEL = "lazycloud-launch"
_PROVIDER_LABEL = "lazycloud-provider"


class HetznerNodeImage(BaseModel):
    model_config = ConfigDict(frozen=True)

    image_id: int = Field(gt=0)
    recipe_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class HetznerPooledProvider:
    provider_ref: str
    client: HetznerClient
    images_by_location: Mapping[str, HetznerNodeImage]
    allowed_server_types: frozenset[str]
    usd_per_currency_unit: Decimal
    primary_ipv4_hourly_micros: int
    launch_credentials: ProviderNodeLaunchCredentials

    def unit_offer(self, unit: ComputeUnitRecord) -> ComputeOffer:
        location, separator, instance_type = unit.offer_id.partition(":")
        if (
            unit.provider_ref != self.provider_ref
            or location != unit.region
            or not separator
            or not instance_type
        ):
            raise ValueError("Hetzner unit has invalid provider offer identity")
        return recorded_unit_offer(unit, cloud="hetzner", instance_type=instance_type)

    def list_offers(self) -> Iterable[ComputeOffer]:
        if self.usd_per_currency_unit <= 0:
            raise ValueError("Hetzner supplier currency conversion must be positive")
        for shape in self.client.server_types():
            if shape.name not in self.allowed_server_types or shape.architecture != "x86":
                continue
            if shape.cpu_type != "dedicated":
                continue
            for price in shape.prices:
                if price.location not in self.images_by_location:
                    continue
                location = next(
                    (item for item in shape.locations if item.name == price.location), None
                )
                if (
                    location is not None
                    and location.deprecation is not None
                    and location.deprecation.unavailable_after <= utc_now()
                ):
                    continue
                micros = (
                    int(
                        (
                            price.price_hourly.net * self.usd_per_currency_unit * 1_000_000
                        ).to_integral_value(rounding=ROUND_CEILING)
                    )
                    + self.primary_ipv4_hourly_micros
                )
                if micros <= 0:
                    raise ValueError("Hetzner returned a nonpositive offer price")
                yield pooled_cloud_offer(
                    offer_id=f"{price.location}:{shape.name}",
                    provider=self.provider_ref,
                    cloud="hetzner",
                    instance_type=shape.name,
                    region=price.location,
                    cpu_millicores=shape.cores * 1000,
                    memory_mb=int(shape.memory * 1024),
                    storage_mb=shape.disk * 1024,
                    hourly_cost_micros=micros,
                    capability_key=f"hetzner:{price.location}:{shape.name}:amd64:runsc",
                )

    def ensure_unit(self, request: ProviderUnitRequest) -> ProviderUnitSnapshot:
        return self.set_unit_capacity(
            request,
            desired_machines=request.desired_machines,
            max_machines=request.max_machines,
        )

    def describe_unit(self, request: ProviderUnitRequest) -> ProviderUnitSnapshot:
        return self._snapshot(request, self._servers(request))

    def set_unit_capacity(
        self,
        request: ProviderUnitRequest,
        *,
        desired_machines: int,
        max_machines: int,
    ) -> ProviderUnitSnapshot:
        if not 0 <= desired_machines <= max_machines:
            raise ValueError("invalid Hetzner capacity bounds")
        request = request.model_copy(
            update={
                "desired_machines": desired_machines,
                "max_machines": max_machines,
            }
        )
        servers = self._servers(request)
        if len(servers) > desired_machines:
            # Only release_machine can identify the worker the drain owner fenced.
            return self._snapshot(request, servers)
        if len(servers) == desired_machines:
            return self._snapshot(request, servers)
        image = self.images_by_location[request.offer.region]
        observed_image = self.client.image(image.image_id)
        if (
            observed_image.status != "available"
            or observed_image.architecture != "x86"
            or observed_image.labels.get(_RELEASE_LABEL) != image.recipe_sha256[:63]
        ):
            raise ValueError("Hetzner node image does not match the published release")
        names = {server.name for server in servers}
        for index in range(max_machines):
            if len(servers) >= desired_machines:
                break
            name = f"lc-{request.unit_id}-{request.generation}-{index}"
            if name in names:
                continue
            prior = self.launch_credentials.for_server(request, name)
            if prior is not None and (prior.redeemed or prior.expires_at <= utc_now()):
                if (
                    prior.provider_instance_id is not None
                    and self.client.server(_server_id(prior.provider_instance_id)) is not None
                ):
                    raise ValueError("Hetzner launch still has a live provider instance")
                self.launch_credentials.revoke(prior.launch_id)
            launch = self.launch_credentials.prepare(request, name)
            labels = self._labels(request)
            labels[_LAUNCH_LABEL] = launch.launch_id
            body: dict[str, JsonValue] = {
                "name": name,
                "server_type": request.offer.instance_type,
                "location": request.offer.region,
                "image": image.image_id,
                "labels": dict(labels),
                "public_net": {"enable_ipv4": True, "enable_ipv6": False},
                "start_after_create": True,
                "automount": False,
                "user_data": bootstrap_script(request, launch),
            }
            try:
                server = self.client.create_server(body)
            except HetznerError as exc:
                if exc.code not in {"uniqueness_error", "conflict", "transport_unavailable"}:
                    raise
                # A concurrent or timed-out create is recovered through its provider name.
                servers = self._servers(request)
                if name not in {item.name for item in servers}:
                    raise
                names = {item.name for item in servers}
                continue
            self._validate_server(request, server)
            self._bind_server(server)
            self._tag_ips(request, server)
            servers.append(server)
            names.add(name)
        return self._snapshot(request, servers)

    def release_machine(
        self,
        request: ProviderUnitRequest,
        provider_instance_id: str,
    ) -> ProviderUnitSnapshot:
        server = self.client.server(_server_id(provider_instance_id))
        if server is not None:
            self._validate_server(request, server)
            self._tag_ips(request, server)
            self.client.delete_server(server.id)
        self._cleanup_ips(request, provider_instance_id)
        return self.describe_unit(request)

    def delete_unit(self, request: ProviderUnitRequest) -> ProviderUnitSnapshot:
        for server in self._servers(request):
            self._tag_ips(request, server)
            self.client.delete_server(server.id)
        self._cleanup_ips(request)
        request = request.model_copy(update={"desired_machines": 0})
        servers = self._servers(request)
        snapshot = self._snapshot(request, servers)
        return snapshot.model_copy(
            update={
                "phase": ProviderCapacityPhase.Deleting
                if servers
                else ProviderCapacityPhase.Deleted,
            }
        )

    def machine_storage_destroyed(
        self,
        request: ProviderUnitRequest,
        provider_instance_id: str,
        storage_volume_ids: tuple[str, ...],
    ) -> bool:
        if self.client.server(_server_id(provider_instance_id)) is not None:
            return False
        for volume_id in storage_volume_ids:
            if self.client.volume(_server_id(volume_id)) is not None:
                return False
        self._cleanup_ips(request, provider_instance_id)
        selector = f"{self._selector(request)},{_SERVER_LABEL}={provider_instance_id}"
        return not tuple(self.client.primary_ips(selector))

    def _servers(self, request: ProviderUnitRequest) -> list[Server]:
        servers = list(self.client.servers(self._selector(request)))
        for server in servers:
            self._validate_server(request, server)
            self._bind_server(server)
            self._tag_ips(request, server)
        return servers

    def _validate_server(self, request: ProviderUnitRequest, server: Server) -> None:
        if (
            server.labels.get(_MANAGED_LABEL) != "true"
            or server.labels.get(_UNIT_LABEL) != request.unit_id
            or server.labels.get(_PROVIDER_LABEL) != provider_label(self.provider_ref)
            or not server.labels.get(_LAUNCH_LABEL)
            or server.location.name != request.offer.region
            or server.server_type.name != request.offer.instance_type
        ):
            raise ValueError("Hetzner server does not belong to the requested capacity unit")
        if server.volumes:
            raise ValueError("Hetzner managed nodes cannot contain externally attached volumes")

    def _bind_server(self, server: Server) -> None:
        self.launch_credentials.bind(
            server.labels[_LAUNCH_LABEL],
            provider_ref=self.provider_ref,
            provider_instance_id=str(server.id),
            unit_id=server.labels[_UNIT_LABEL],
            server_name=server.name,
            region=server.location.name,
            generation=int(server.labels[_GENERATION_LABEL]),
        )

    def _tag_ips(self, request: ProviderUnitRequest, server: Server) -> None:
        labels = {
            _MANAGED_LABEL: "true",
            _UNIT_LABEL: request.unit_id,
            _PROVIDER_LABEL: provider_label(self.provider_ref),
            _SERVER_LABEL: str(server.id),
        }
        for address in (server.public_net.ipv4, server.public_net.ipv6):
            if address is not None:
                self.client.tag_primary_ip(address.id, labels)

    def _cleanup_ips(self, request: ProviderUnitRequest, server_id: str = "") -> None:
        selector = self._selector(request)
        if server_id:
            selector += f",{_SERVER_LABEL}={server_id}"
        for address in self.client.primary_ips(selector):
            if address.labels.get(_UNIT_LABEL) != request.unit_id:
                raise ValueError("Hetzner IP does not belong to the capacity unit")
            if address.assignee_id is None:
                try:
                    self.client.delete_primary_ip(address.id)
                except HetznerError as exc:
                    if exc.status_code != 404:
                        raise

    def _labels(self, request: ProviderUnitRequest) -> dict[str, str]:
        return {
            _MANAGED_LABEL: "true",
            _UNIT_LABEL: request.unit_id,
            _PROVIDER_LABEL: provider_label(self.provider_ref),
            _GENERATION_LABEL: str(request.generation),
            _RELEASE_LABEL: self.images_by_location[request.offer.region].recipe_sha256[:63],
        }

    @staticmethod
    def _selector(request: ProviderUnitRequest) -> str:
        return f"{_MANAGED_LABEL}=true,{_UNIT_LABEL}={request.unit_id}"

    def _snapshot(
        self, request: ProviderUnitRequest, servers: list[Server]
    ) -> ProviderUnitSnapshot:
        instances = [
            ProviderUnitInstance(
                provider_instance_id=str(server.id),
                status=_status(server.status),
                address=server.public_net.ipv4.ip if server.public_net.ipv4 else "",
                availability_zone=server.location.name,
                storage_volume_ids=tuple(str(value) for value in server.volumes),
                booted_template_version=server.labels.get(_RELEASE_LABEL, ""),
                billing_started_at=server.created,
                billing_minimum_seconds=3600,
                billing_quantum_seconds=3600,
            )
            for server in servers
        ]
        return ProviderUnitSnapshot(
            phase=(
                ProviderCapacityPhase.Ready
                if len(servers) == request.desired_machines
                and all(server.status == "running" for server in servers)
                else ProviderCapacityPhase.Provisioning
            ),
            resource_id=request.unit_id,
            desired_machines=request.desired_machines,
            max_machines=request.max_machines,
            observed_machines=len(servers),
            instances=instances,
            provider_state=ComputeUnitProviderState(resource_id=request.unit_id),
            current_template_version=self.images_by_location[request.offer.region].recipe_sha256[
                :63
            ],
        )


def _server_id(value: str) -> int:
    if not value.isdecimal() or int(value) <= 0:
        raise ValueError("invalid Hetzner resource ID")
    return int(value)


def _status(status: str) -> str:
    if status == "running":
        return ProviderMachineStatus.Active
    if status in {"initializing", "starting"}:
        return ProviderMachineStatus.Pending
    if status in {"deleting", "off", "stopping"}:
        return ProviderMachineStatus.Unhealthy
    return ProviderMachineStatus.Unknown


def bootstrap_script(request: ProviderUnitRequest, launch: ProviderNodeLaunchCredential) -> str:
    if urlparse(request.bootstrap.control_plane_url).scheme != "https":
        raise ValueError("Hetzner bootstrap credentials require an HTTPS control-plane origin")
    return node_bootstrap_script(
        NodeBootstrapSettings(
            control_plane_url=request.bootstrap.control_plane_url,
            enrollment_request_id=request.bootstrap.enrollment_request_id,
            agent_binary_url=request.bootstrap.agent_binary_url,
            agent_sha256=request.bootstrap.agent_sha256,
        ),
        NodeBootstrapProfile(
            provider="hetzner",
            identity_shell=_IDENTITY_SHELL,
            values={
                "__HETZNER_LOCATION__": request.offer.region,
                "__HETZNER_LAUNCH_ID__": launch.launch_id,
                "__HETZNER_BOOTSTRAP_TOKEN__": launch.bootstrap_token.get_secret_value(),
            },
        ),
    )


_IDENTITY_SHELL = r"""
HETZNER_LOCATION=__HETZNER_LOCATION__
resolve_node_identity() {
  umask 077
  if [ -f "$AGENT_STATE_DIR/provider-launch-id" ]; then
    [ "$(< "$AGENT_STATE_DIR/provider-launch-id")" = __HETZNER_LAUNCH_ID__ ]
  else
    printf '%s' __HETZNER_LAUNCH_ID__ > "$AGENT_STATE_DIR/provider-launch-id"
  fi
  if [ ! -f "$AGENT_STATE_DIR/provider-node-token" ]; then
    credential_tmp=$(mktemp "$AGENT_STATE_DIR/.provider-node-token.XXXXXX")
    head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '=\n' \
      > "$credential_tmp"
    sync -f "$credential_tmp"
    printf '%s' __HETZNER_BOOTSTRAP_TOKEN__ > "$AGENT_STATE_DIR/provider-bootstrap-token"
    if ! ln "$credential_tmp" "$AGENT_STATE_DIR/provider-node-token"; then
      [ -f "$AGENT_STATE_DIR/provider-node-token" ]
    fi
    rm -f "$credential_tmp"
    sync -f "$AGENT_STATE_DIR"
  fi
  HETZNER_SERVER_ID=$(curl --noproxy '*' -fsS --connect-timeout 2 --max-time 5 http://169.254.169.254/hetzner/v1/metadata/instance-id)
  [[ "$HETZNER_SERVER_ID" =~ ^[0-9]+$ ]]
}
report_identity_fields() {
  printf ',"provider":"hetzner","region":"%s"' "$HETZNER_LOCATION"
  printf ',"provider_instance_id":"%s"' "$HETZNER_SERVER_ID"
  printf ',"identity_proof_url":"hetzner-bootstrap"'
  printf ',"launch_id":"%s"' "$(< "$AGENT_STATE_DIR/provider-launch-id")"
  printf ',"node_agent_token":"%s"' "$(< "$AGENT_STATE_DIR/provider-node-token")"
  if [ -f "$AGENT_STATE_DIR/provider-bootstrap-token" ]; then
    printf ',"bootstrap_token":"%s"' "$(< "$AGENT_STATE_DIR/provider-bootstrap-token")"
  fi
}
node_fingerprint() { printf 'hetzner:%s' "$HETZNER_SERVER_ID"; }
node_hostname() { hostname; }
PROVIDER_INSTALL_FLAGS=(--provider hetzner --provider-instance-identity hetzner-bootstrap)
"""
