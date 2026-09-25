from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from compute.offers import ComputeOffer
from compute.providers import (
    ProviderUnitRequest,
    ProviderUnitSnapshot,
    ProviderUnitStateCheckpoints,
)

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
        for offer in super(AwsPlatformCapacityProvider, self).list_offers(
            root_volume_gib=root_volume_gib
        ):
            yield offer.model_copy(update={"capability_key": f"{offer.capability_key}:retained"})

    def list_reserve_offers(self, *, root_volume_gib: int) -> Iterable[ComputeOffer]:
        return self.list_offers(root_volume_gib=root_volume_gib)

    @staticmethod
    def _retained(request: ProviderUnitRequest) -> bool:
        return request.offer.capability_key.endswith(":retained")

    def _spec(self, request: ProviderUnitRequest) -> AwsManagedPoolSpec:
        spec = super(AwsPlatformCapacityProvider, self)._spec(request)
        return (
            spec.model_copy(update={"spot_request_type": "persistent"})
            if self._retained(request) and request.offer.preemptible
            else spec
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
        self, request: ProviderUnitRequest, provider_instance_id: str
    ) -> None:
        if self._retained(request):
            self._pool(request).complete_preparation(provider_instance_id)
        else:
            super(AwsPlatformCapacityProvider, self).complete_machine_preparation(
                request, provider_instance_id
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
