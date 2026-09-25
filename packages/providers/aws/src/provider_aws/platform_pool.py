from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from compute.offers import ComputeOffer
from compute.providers import (
    ProviderUnitRequest,
    ProviderUnitSnapshot,
    ProviderUnitStateCheckpoints,
)

from .instance_catalog import aws_instance_catalog_entry
from .managed_pool import AwsManagedPoolSpec
from .pooled_provider import AwsPooledCapacityProvider
from .retained_pool import AwsRetainedPool, RetainedPoolState


@dataclass(frozen=True, slots=True)
class AwsPlatformCapacityProvider(AwsPooledCapacityProvider):
    checkpoints: ProviderUnitStateCheckpoints = field(kw_only=True)

    def list_offers(self, *, root_volume_gib: int) -> Iterable[ComputeOffer]:
        # Every platform offer is retained, so CPU and GPU machines alike can stop
        # as reserves. Units recorded under the plain capability key are Auto
        # Scaling groups bought before; they keep that owner through cleanup.
        # Storage stays the usable root disk, and the price covers the swap a
        # hibernating type launches with.
        for offer in super(AwsPlatformCapacityProvider, self).list_offers(
            root_volume_gib=root_volume_gib
        ):
            update: dict[str, object] = {"capability_key": f"{offer.capability_key}:retained"}
            swap_gib = aws_instance_catalog_entry(offer.instance_type).hibernation_swap_gib
            prices = self.regional_prices.get(offer.region)
            if swap_gib and prices is not None:
                update["cost_terms"] = offer.cost_terms.model_copy(
                    update={
                        "root_disk_hourly_micros": prices.root_disk_hourly_micros(
                            root_volume_gib + swap_gib
                        )
                    }
                )
            yield offer.model_copy(update=update)

    def list_reserve_offers(self, *, root_volume_gib: int) -> Iterable[ComputeOffer]:
        return self.list_offers(root_volume_gib=root_volume_gib)

    @staticmethod
    def _retained(request: ProviderUnitRequest) -> bool:
        return request.offer.capability_key.endswith(":retained")

    def _spec(self, request: ProviderUnitRequest) -> AwsManagedPoolSpec:
        spec = super(AwsPlatformCapacityProvider, self)._spec(request)
        if not self._retained(request):
            return spec
        entry = aws_instance_catalog_entry(request.offer.instance_type)
        return spec.model_copy(
            update={
                "hibernation": entry.hibernates,
                "root_volume_gib": request.root_volume_gib + entry.hibernation_swap_gib,
                "spot_request_type": (
                    "persistent" if request.offer.preemptible else spec.spot_request_type
                ),
            }
        )

    @staticmethod
    def _namespace_id(request: ProviderUnitRequest) -> str:
        if AwsPlatformCapacityProvider._retained(request):
            return (
                RetainedPoolState.model_validate(request.provider_state.attributes).namespace_id
                if request.provider_state.attributes
                else request.workspace_id
            )
        return AwsPooledCapacityProvider._namespace_id(request)

    def _pool(self, request: ProviderUnitRequest) -> AwsRetainedPool:
        return AwsRetainedPool(
            request,
            self._spec(request),
            self.client_provider.assume(self._target(request.offer.region)),
            self.checkpoints,
        )

    def ensure_unit(self, request: ProviderUnitRequest) -> ProviderUnitSnapshot:
        if self._retained(request):
            return self._pool(request).ensure()
        return super(AwsPlatformCapacityProvider, self).ensure_unit(request)

    def describe_unit(self, request: ProviderUnitRequest) -> ProviderUnitSnapshot:
        if self._retained(request):
            return self._pool(request).describe()
        return super(AwsPlatformCapacityProvider, self).describe_unit(request)

    def set_unit_capacity(
        self, request: ProviderUnitRequest, *, desired_machines: int, max_machines: int
    ) -> ProviderUnitSnapshot:
        if self._retained(request):
            return self._pool(
                request.model_copy(
                    update={"desired_machines": desired_machines, "max_machines": max_machines}
                )
            ).ensure()
        return super(AwsPlatformCapacityProvider, self).set_unit_capacity(
            request, desired_machines=desired_machines, max_machines=max_machines
        )

    def complete_machine_preparation(
        self, request: ProviderUnitRequest, provider_instance_id: str, *, hibernate: bool
    ) -> ProviderUnitSnapshot:
        if self._retained(request):
            return self._pool(request).complete_preparation(
                provider_instance_id, hibernate=hibernate
            )
        return super(AwsPlatformCapacityProvider, self).complete_machine_preparation(
            request, provider_instance_id, hibernate=hibernate
        )

    def refresh_machine(
        self, request: ProviderUnitRequest, provider_instance_id: str
    ) -> ProviderUnitSnapshot:
        if self._retained(request):
            return self._pool(request).refresh(provider_instance_id)
        return super(AwsPlatformCapacityProvider, self).refresh_machine(
            request, provider_instance_id
        )

    def stop_machine(
        self, request: ProviderUnitRequest, provider_instance_id: str
    ) -> ProviderUnitSnapshot:
        if self._retained(request):
            return self._pool(request).stop(provider_instance_id)
        return super(AwsPlatformCapacityProvider, self).stop_machine(request, provider_instance_id)

    def release_machine(
        self, request: ProviderUnitRequest, provider_instance_id: str
    ) -> ProviderUnitSnapshot:
        if self._retained(request):
            return self._pool(request).release(provider_instance_id)
        return super(AwsPlatformCapacityProvider, self).release_machine(
            request, provider_instance_id
        )

    def delete_unit(self, request: ProviderUnitRequest) -> ProviderUnitSnapshot:
        if self._retained(request):
            return self._pool(request).delete()
        return super(AwsPlatformCapacityProvider, self).delete_unit(request)
