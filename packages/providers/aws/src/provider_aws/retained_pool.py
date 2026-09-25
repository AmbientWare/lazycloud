from __future__ import annotations

import logging
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import uuid4

from botocore.exceptions import BotoCoreError, ClientError
from compute.providers import (
    ProviderCapacityPhase,
    ProviderMachineStatus,
    ProviderUnitInstance,
    ProviderUnitRequest,
    ProviderUnitSnapshot,
    ProviderUnitStateCheckpoints,
)
from pydantic import BaseModel, ConfigDict, Field
from shared.capacity import CapacityFailureCode
from shared.compute_policy import ComputeUnitProviderState
from shared.timestamps import utc_now

from .block_volumes import is_disk_volume_device
from .managed_pool import (
    AWS_MANAGED_POOL_TAG,
    AWS_MANAGED_POOL_TAG_VALUE,
    AwsManagedPoolClients,
    AwsManagedPoolModel,
    AwsManagedPoolProvisioner,
    AwsManagedPoolSpec,
    _client_error,
    _client_error_code,
    _Filter,
    _host_configuration_revision,
    _LaunchTemplateVersions,
)
from .provider_control import upstream_error

LOGGER = logging.getLogger(__name__)

PROVIDER_OPERATION_DEADLINE = timedelta(minutes=10)
"""How long a launch or a hibernation may take before the pool gives up on it."""

HIBERNATE_AFTER_START = timedelta(minutes=2)
"""EC2 refuses to hibernate an instance this soon after it starts."""


class SlotPhase(StrEnum):
    Preparing = "preparing"
    Refreshing = "refreshing"
    Stopping = "stopping"
    Stopped = "stopped"
    Resuming = "resuming"
    Active = "active"
    Retiring = "retiring"


_REJECTED_LAUNCH_CODES = frozenset(
    {
        "InsufficientInstanceCapacity",
        "InstanceLimitExceeded",
        "VcpuLimitExceeded",
        "MaxSpotInstanceCountExceeded",
        "SpotMaxPriceTooLow",
        "Unsupported",
        "InvalidParameterValue",
        "InvalidParameterCombination",
        "UnauthorizedOperation",
    }
)
_HIBERNATION_UNCONFIGURED = "UnsupportedHibernationConfiguration"
"""EC2's refusal for an instance launched without hibernation, which no retry changes.

Any other refusal is retried. EC2 answers `UnsupportedOperation`, "not ready to
hibernate yet", until the guest has set up its swap, which took 116 seconds
after start on a c6a.large and takes longer with more RAM.
"""


class RetainedSlot(AwsManagedPoolModel):
    token: str
    created_at: datetime
    launch_started_at: datetime | None = None
    launch_template_id: str
    launch_template_version: int
    host_revision: str
    subnet_id: str
    serving: bool
    phase: SlotPhase
    instance_id: str = ""
    spot_request_id: str = ""
    availability_zone: str = ""
    storage_volume_ids: tuple[str, ...] = ()
    hibernate: bool = False
    """The compute side asked for this stop to keep the reserve's memory."""
    stop_requested_at: datetime | None = None
    """When EC2 last accepted a stop of either kind; kept only while that stop is pending."""
    hibernate_refused_since: datetime | None = None
    """When EC2 first refused to hibernate this stop; kept only while that stop is pending."""


class RetainedPoolState(AwsManagedPoolModel):
    namespace_id: str
    launch_template_id: str = ""
    launch_template_version: int = 0
    host_revision: str = ""
    slots: tuple[RetainedSlot, ...] = ()
    last_capacity_failure_at: datetime | None = None
    last_capacity_failure_code: CapacityFailureCode = CapacityFailureCode.ProviderLaunchFailed


class _Response(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class _ResponseMetadata(_Response):
    retry_attempts: int | None = Field(default=None, alias="RetryAttempts")


class _ErrorResponse(_Response):
    metadata: _ResponseMetadata = Field(default_factory=_ResponseMetadata, alias="ResponseMetadata")


class _State(_Response):
    name: str = Field(alias="Name")


class _Tag(_Response):
    key: str = Field(alias="Key")
    value: str = Field(alias="Value")


class _Placement(_Response):
    zone: str = Field(default="", alias="AvailabilityZoneId")


class _Ebs(_Response):
    id: str = Field(alias="VolumeId")


class _BlockDevice(_Response):
    name: str = Field(default="", alias="DeviceName")
    ebs: _Ebs | None = Field(default=None, alias="Ebs")

    @property
    def machine_volume_id(self) -> str:
        """The volume id when this is the machine's own storage, not an attached disk volume."""
        if self.ebs is None or is_disk_volume_device(self.name):
            return ""
        return self.ebs.id


class _HibernationOptions(_Response):
    configured: bool = Field(default=False, alias="Configured")


class _StateReason(_Response):
    code: str = Field(default="", alias="Code")


class _Instance(_Response):
    id: str = Field(alias="InstanceId")
    token: str = Field(default="", alias="ClientToken")
    state: _State = Field(alias="State")
    state_reason: _StateReason = Field(default_factory=_StateReason, alias="StateReason")
    launch_time: datetime | None = Field(default=None, alias="LaunchTime")
    hibernation: _HibernationOptions = Field(
        default_factory=_HibernationOptions, alias="HibernationOptions"
    )
    placement: _Placement = Field(default_factory=_Placement, alias="Placement")
    tags: tuple[_Tag, ...] = Field(default=(), alias="Tags")
    spot_request_id: str = Field(default="", alias="SpotInstanceRequestId")
    devices: tuple[_BlockDevice, ...] = Field(default=(), alias="BlockDeviceMappings")


class _Reservation(_Response):
    instances: tuple[_Instance, ...] = Field(default=(), alias="Instances")


class _Instances(_Response):
    reservations: tuple[_Reservation, ...] = Field(default=(), alias="Reservations")
    next_token: str = Field(default="", alias="NextToken")

    @property
    def instances(self) -> tuple[_Instance, ...]:
        return tuple(i for reservation in self.reservations for i in reservation.instances)


class _Launched(_Response):
    instances: tuple[_Instance, ...] = Field(alias="Instances")


class _SpotRequest(_Response):
    id: str = Field(alias="SpotInstanceRequestId")
    state: str = Field(alias="State")
    instance_id: str = Field(default="", alias="InstanceId")


class _SpotRequests(_Response):
    requests: tuple[_SpotRequest, ...] = Field(alias="SpotInstanceRequests")


class AwsRetainedPool:
    """EC2 instances whose launch intent survives controller retries and restarts."""

    def __init__(
        self,
        request: ProviderUnitRequest,
        spec: AwsManagedPoolSpec,
        clients: AwsManagedPoolClients,
        checkpoints: ProviderUnitStateCheckpoints,
    ) -> None:
        self.request = request
        self.spec = spec
        self.clients = clients
        self.provisioner = AwsManagedPoolProvisioner(clients)
        self.checkpoints = checkpoints
        self.recorded = checkpoints.load(request)
        self.state = (
            RetainedPoolState.model_validate(self.recorded.attributes)
            if self.recorded.attributes
            else RetainedPoolState(namespace_id=spec.workspace_id)
        )
        if self.state.namespace_id != spec.workspace_id or self.recorded.resource_id not in {
            "",
            f"ec2-pool-{spec.resource_key}",
        }:
            raise ValueError("retained pool state does not match its namespace")

    def _save(self, state: RetainedPoolState, *, deleted: bool = False) -> None:
        updated = ComputeUnitProviderState(
            revision=self.recorded.revision,
            committed_machines=sum(
                slot.phase is not SlotPhase.Retiring
                or bool(slot.instance_id or slot.spot_request_id or slot.storage_volume_ids)
                or slot.launch_started_at is not None
                for slot in state.slots
            ),
            resource_id="" if deleted else f"ec2-pool-{self.spec.resource_key}",
            attributes=state.model_dump(mode="json"),
        )
        if updated == self.recorded:
            return
        updated = updated.model_copy(update={"revision": self.recorded.revision + 1})
        self.checkpoints.save(self.request, expected=self.recorded, state=updated)
        self.recorded = updated
        self.state = state

    def _update(self, slot: RetainedSlot) -> None:
        self._save(
            self.state.model_copy(
                update={
                    "slots": tuple(
                        slot if item.token == slot.token else item for item in self.state.slots
                    )
                }
            )
        )

    def _owned(self, instance: _Instance, slot: RetainedSlot) -> None:
        tags = {tag.key: tag.value for tag in instance.tags}
        if (
            instance.token != slot.token
            or tags.get(AWS_MANAGED_POOL_TAG) != AWS_MANAGED_POOL_TAG_VALUE
            or tags.get("cloud-pool:key") != self.spec.resource_key
            or tags.get("cloud-pool:workspace") != self.state.namespace_id
            or (slot.instance_id and slot.instance_id != instance.id)
            or (self.spec.preemptible and not instance.spot_request_id)
        ):
            raise ValueError("retained instance does not match its durable launch owner")

    def _inventory(self) -> dict[str, _Instance]:
        found: dict[str, _Instance] = {}
        slots = {slot.token: slot for slot in self.state.slots}
        if not slots:
            return found
        for instance in self._describe([{"Name": "client-token", "Values": list(slots)}]):
            slot = slots.get(instance.token)
            if slot is None:
                raise ValueError("EC2 returned an instance outside the requested launches")
            self._owned(instance, slot)
            if slot.token in found:
                raise ValueError("EC2 returned multiple instances for one launch slot")
            found[slot.token] = instance
        return found

    def _describe(self, filters: list[_Filter]) -> list[_Instance]:
        described: list[_Instance] = []
        token = ""
        while True:
            response = _Instances.model_validate(
                self.clients.ec2.describe_instances(Filters=filters, NextToken=token)
                if token
                else self.clients.ec2.describe_instances(Filters=filters)
            )
            described += response.instances
            token = response.next_token
            if not token:
                return described

    def _record_instances(self, inventory: dict[str, _Instance]) -> None:
        slots: list[RetainedSlot] = []
        for slot in self.state.slots:
            instance = inventory.get(slot.token)
            if instance is not None:
                stop_pending = slot.phase is SlotPhase.Stopping and instance.state.name != "stopped"
                slot = slot.model_copy(
                    update={
                        "instance_id": instance.id,
                        "spot_request_id": instance.spot_request_id,
                        "availability_zone": instance.placement.zone,
                        "storage_volume_ids": tuple(
                            device.machine_volume_id
                            for device in instance.devices
                            if device.machine_volume_id
                        )
                        or slot.storage_volume_ids,
                        # Stop timing belongs to the stop pending now. A slot
                        # resumed or serving again drops it, so an earlier
                        # stop's time never forces a later one.
                        "stop_requested_at": (slot.stop_requested_at if stop_pending else None),
                        "hibernate_refused_since": (
                            slot.hibernate_refused_since if stop_pending else None
                        ),
                        # Only EC2 stops a running, preparing or refreshing
                        # instance; this pool stops one only after moving it to
                        # Stopping. A reserve holds no work, so an interrupted
                        # one is replaced. A refresh starts its instance in the
                        # same call that records it, so no pass sees it unstarted.
                        "phase": (
                            SlotPhase.Retiring
                            if instance.state.name in {"terminated", "shutting-down"}
                            or (
                                slot.phase
                                in {SlotPhase.Active, SlotPhase.Preparing, SlotPhase.Refreshing}
                                and instance.state.name in {"stopping", "stopped"}
                            )
                            else SlotPhase.Stopped
                            if slot.phase is SlotPhase.Stopping and instance.state.name == "stopped"
                            else slot.phase
                        ),
                    }
                )
            elif (
                slot.instance_id
                and slot.phase is not SlotPhase.Retiring
                and slot.launch_started_at is not None
                and utc_now() - slot.launch_started_at > PROVIDER_OPERATION_DEADLINE
                and self.provisioner.machine_storage_destroyed(
                    self.spec, slot.instance_id, slot.storage_volume_ids
                )
            ):
                slot = slot.model_copy(update={"phase": SlotPhase.Retiring})
            slots.append(slot)
        if tuple(slots) != self.state.slots:
            self._save(self.state.model_copy(update={"slots": tuple(slots)}))

    def _launch(self, slot: RetainedSlot) -> bool:
        # EC2's idempotency window is finite. An unresolved launch keeps its budget
        # instead of issuing a new request after the token might have expired.
        first_attempt = slot.launch_started_at is None
        if first_attempt:
            slot = slot.model_copy(update={"launch_started_at": utc_now()})
            self._update(slot)
        assert slot.launch_started_at is not None
        if utc_now() - slot.launch_started_at > PROVIDER_OPERATION_DEADLINE:
            raise RuntimeError(
                f"EC2 launch {slot.token} has no instance evidence after ten minutes"
            )
        operation = "launch retained instance"
        try:
            launched = self.clients.ec2.run_instances(
                LaunchTemplate={
                    "LaunchTemplateId": slot.launch_template_id,
                    "Version": str(slot.launch_template_version),
                },
                SubnetId=slot.subnet_id,
                ClientToken=slot.token,
                MinCount=1,
                MaxCount=1,
            )
        except ClientError as exc:
            if (
                first_attempt
                and _client_error_code(exc) in _REJECTED_LAUNCH_CODES
                and _ErrorResponse.model_validate(exc.response).metadata.retry_attempts == 0
            ):
                # A rejection cannot resolve a previous call with an unknown outcome.
                rejected = slot.model_copy(
                    update={"phase": SlotPhase.Retiring, "launch_started_at": None}
                )
                code = _client_error_code(exc)
                failure = (
                    CapacityFailureCode.CapacityUnavailable
                    if code == "InsufficientInstanceCapacity"
                    else CapacityFailureCode.ProviderQuotaExceeded
                    if code
                    in {
                        "InstanceLimitExceeded",
                        "VcpuLimitExceeded",
                        "MaxSpotInstanceCountExceeded",
                    }
                    else CapacityFailureCode.ProviderLaunchFailed
                )
                self._save(
                    self.state.model_copy(
                        update={
                            "slots": tuple(
                                rejected if item.token == slot.token else item
                                for item in self.state.slots
                            ),
                            "last_capacity_failure_at": utc_now(),
                            "last_capacity_failure_code": failure,
                        }
                    )
                )
                return False
            raise _client_error(exc, operation=operation) from exc
        except BotoCoreError as exc:
            raise upstream_error(exc, operation=operation) from exc
        response = _Launched.model_validate(launched)
        if len(response.instances) != 1:
            raise ValueError("EC2 did not return exactly one instance for the launch slot")
        instance = response.instances[0]
        self._owned(instance, slot)
        self._update(
            slot.model_copy(
                update={"instance_id": instance.id, "spot_request_id": instance.spot_request_id}
            )
        )
        return True

    def _start(self, slot: RetainedSlot, instance: _Instance) -> None:
        operation = "start retained instance"
        # EC2 records a requested hibernation the same way whether the guest
        # hibernated or fell back to shutting down, so this names the request.
        LOGGER.info(
            "starting reserve %s, stopped by %s",
            instance.id,
            instance.state_reason.code or "an unrecorded reason",
        )
        try:
            self.clients.ec2.start_instances(InstanceIds=[instance.id])
        except BotoCoreError as exc:
            self._keep_unrefreshed(slot)
            raise upstream_error(exc, operation=operation) from exc
        except ClientError as exc:
            if _client_error_code(exc) != "InsufficientInstanceCapacity":
                self._keep_unrefreshed(slot)
                raise _client_error(exc, operation=operation) from exc
            # A stopped Spot instance starts only when its market has room again.
            # The recorded failure cools the market; a fresh launch replaces it.
            self._save(
                self.state.model_copy(
                    update={
                        "slots": tuple(
                            slot.model_copy(update={"phase": SlotPhase.Retiring})
                            if item.token == slot.token
                            else item
                            for item in self.state.slots
                        ),
                        "last_capacity_failure_at": utc_now(),
                        "last_capacity_failure_code": CapacityFailureCode.CapacityUnavailable,
                    }
                )
            )

    def _stop(self, slot: RetainedSlot, instance: _Instance) -> None:
        """Stop a reserve, hibernating it when the compute side asked for that.

        A hibernation EC2 refuses is tried again each pass and becomes a plain stop
        once refused for the operation deadline, or at once when the instance was
        launched without hibernation. A stop of either kind still pending past
        that deadline is forced, so the reserve always reaches stopped.
        """
        now = utc_now()
        if instance.state.name == "stopping":
            requested = slot.stop_requested_at
            if requested is None:
                # Stopping without a request this pool recorded; time it from now.
                self._update(slot.model_copy(update={"stop_requested_at": now}))
            elif now - requested > PROVIDER_OPERATION_DEADLINE:
                LOGGER.warning(
                    "reserve %s is still stopping after %s; forcing it to stop",
                    instance.id,
                    PROVIDER_OPERATION_DEADLINE,
                )
                if self._request_stop(instance, force=True):
                    self._update(slot.model_copy(update={"stop_requested_at": now}))
            return
        if instance.state.name != "running":
            return
        if slot.hibernate and instance.hibernation.configured:
            if (
                instance.launch_time is not None
                and now - instance.launch_time < HIBERNATE_AFTER_START
            ):
                return
            refused_since = slot.hibernate_refused_since or now
            try:
                self.clients.ec2.stop_instances(InstanceIds=[instance.id], Hibernate=True)
            except (ClientError, BotoCoreError) as exc:
                code = (
                    _client_error_code(exc) if isinstance(exc, ClientError) else type(exc).__name__
                )
                if (
                    code != _HIBERNATION_UNCONFIGURED
                    and now - refused_since < PROVIDER_OPERATION_DEADLINE
                ):
                    LOGGER.warning(
                        "EC2 refused to hibernate reserve %s (%s); retrying next pass",
                        instance.id,
                        code,
                    )
                    if slot.hibernate_refused_since is None:
                        self._update(slot.model_copy(update={"hibernate_refused_since": now}))
                    return
                LOGGER.warning(
                    "EC2 did not hibernate reserve %s (%s); stopping it instead", instance.id, code
                )
            else:
                LOGGER.info("hibernating reserve %s", instance.id)
                self._update(slot.model_copy(update={"stop_requested_at": now}))
                return
        else:
            LOGGER.info("stopping reserve %s without hibernating", instance.id)
        if self._request_stop(instance):
            self._update(slot.model_copy(update={"stop_requested_at": now}))

    def _request_stop(self, instance: _Instance, *, force: bool = False) -> bool:
        """Ask EC2 to stop `instance`; a refusal is logged and left for the next pass."""
        try:
            self.clients.ec2.stop_instances(InstanceIds=[instance.id], Force=force)
        except (ClientError, BotoCoreError) as exc:
            LOGGER.warning(
                "EC2 did not accept the stop of reserve %s (%s); retrying next pass",
                instance.id,
                _client_error_code(exc) if isinstance(exc, ClientError) else type(exc).__name__,
            )
            return False
        return True

    def _keep_unrefreshed(self, slot: RetainedSlot) -> None:
        # A reserve whose refresh could not start is still usable as it was.
        if slot.phase is SlotPhase.Refreshing:
            self._update(slot.model_copy(update={"phase": SlotPhase.Stopped}))

    def _retire(self, slot: RetainedSlot, instance: _Instance | None) -> bool:
        if not slot.instance_id:
            if slot.launch_started_at is None:
                return True
            self._launch(slot)
            return False
        if slot.spot_request_id:
            return self._retire_spot_request(slot, instance)
        if instance is not None and instance.state.name != "terminated":
            if instance.state.name != "shutting-down":
                self.clients.ec2.terminate_instances(InstanceIds=[slot.instance_id])
            return False
        return self.provisioner.machine_storage_destroyed(
            self.spec, slot.instance_id, slot.storage_volume_ids
        )

    def _retire_spot_request(self, slot: RetainedSlot, instance: _Instance | None) -> bool:
        # A persistent request relaunches an untagged instance from its original
        # launch specification whenever its instance ends, so retirement cancels
        # the request before terminating anything it launched.
        try:
            response = self.clients.ec2.describe_spot_instance_requests(
                SpotInstanceRequestIds=[slot.spot_request_id]
            )
        except ClientError as exc:
            if _client_error_code(exc) != "InvalidSpotInstanceRequestID.NotFound":
                raise
            named = ""
        else:
            requests = _SpotRequests.model_validate(response).requests
            if len(requests) != 1 or requests[0].id != slot.spot_request_id:
                raise ValueError("Spot request ownership evidence is incomplete")
            if requests[0].state not in {"cancelled", "closed", "failed"}:
                self.clients.ec2.cancel_spot_instance_requests(
                    SpotInstanceRequestIds=[slot.spot_request_id]
                )
                return False
            named = requests[0].instance_id
        launched = {
            i.id: i
            for i in self._describe(
                [{"Name": "spot-instance-request-id", "Values": [slot.spot_request_id]}]
            )
        }
        if instance is not None:
            launched.setdefault(instance.id, instance)
        if named and named not in launched and named != slot.instance_id:
            try:
                launched |= {
                    i.id: i
                    for i in _Instances.model_validate(
                        self.clients.ec2.describe_instances(InstanceIds=[named])
                    ).instances
                }
            except ClientError as exc:
                if _client_error_code(exc) != "InvalidInstanceID.NotFound":
                    raise
        if any(i.spot_request_id != slot.spot_request_id for i in launched.values()):
            raise ValueError("instance was not launched by the slot's Spot request")
        volumes = tuple(
            dict.fromkeys(
                (
                    *slot.storage_volume_ids,
                    *(
                        device.machine_volume_id
                        for i in launched.values()
                        if i.id != slot.instance_id
                        for device in i.devices
                        if device.machine_volume_id
                    ),
                )
            )
        )
        if volumes != slot.storage_volume_ids:
            slot = slot.model_copy(update={"storage_volume_ids": volumes})
            self._update(slot)
        live = [i for i in launched.values() if i.state.name != "terminated"]
        if live:
            terminate = [i.id for i in live if i.state.name != "shutting-down"]
            if terminate:
                self.clients.ec2.terminate_instances(InstanceIds=terminate)
            return False
        return self.provisioner.machine_storage_destroyed(
            self.spec, slot.instance_id, slot.storage_volume_ids
        )

    def _actions(self, inventory: dict[str, _Instance]) -> None:
        retired: set[str] = set()
        for slot in self.state.slots:
            instance = inventory.get(slot.token)
            if slot.phase is SlotPhase.Retiring:
                if self._retire(slot, instance):
                    retired.add(slot.token)
            elif not slot.instance_id and self.request.purchases_enabled:
                if not self._launch(slot):
                    break
            elif instance is not None:
                if slot.phase is SlotPhase.Stopping:
                    self._stop(slot, instance)
                elif (
                    slot.phase in {SlotPhase.Resuming, SlotPhase.Refreshing}
                    and instance.state.name == "stopped"
                    and self.request.purchases_enabled
                ):
                    self._start(slot, instance)
        if retired:
            self._save(
                self.state.model_copy(
                    update={"slots": tuple(s for s in self.state.slots if s.token not in retired)}
                )
            )

    def ensure(self) -> ProviderUnitSnapshot:
        inventory = self._inventory()
        self._record_instances(inventory)
        if self.request.purchases_enabled:
            template_id, version = self.provisioner._ensure_launch_template(
                self.spec, self.spec.security_group_id
            )
            versions = _LaunchTemplateVersions.model_validate(
                self.clients.ec2.describe_launch_template_versions(
                    LaunchTemplateId=template_id, Versions=[str(version)]
                )
            ).values
            if len(versions) != 1:
                raise ValueError("EC2 current launch template evidence is incomplete")
            self._save(
                self.state.model_copy(
                    update={
                        "launch_template_id": template_id,
                        "launch_template_version": version,
                        "host_revision": _host_configuration_revision(versions[0].data),
                    }
                )
            )
        serving = sum(s.serving and s.phase is not SlotPhase.Retiring for s in self.state.slots)
        # A reserve the request still counts stays stopped while the pool has
        # room to launch a machine instead.
        resume_until = self.request.desired_machines - self._launch_room()
        for slot in self.state.slots:
            if serving >= resume_until or not self.request.purchases_enabled:
                break
            if slot.phase is SlotPhase.Stopped:
                self._update(slot.model_copy(update={"serving": True, "phase": SlotPhase.Resuming}))
                serving += 1
        reserves = [
            s for s in self.state.slots if not s.serving and s.phase is not SlotPhase.Retiring
        ]
        for slot in reserves[self.request.stopped_machines :]:
            self._update(slot.model_copy(update={"phase": SlotPhase.Retiring}))
        if self.request.purchases_enabled:
            reserve_count = min(len(reserves), self.request.stopped_machines)
            additions = [True] * max(self.request.desired_machines - serving, 0)
            additions += [False] * max(self.request.stopped_machines - reserve_count, 0)
            room = self._launch_room()
            if additions and room:
                subnet = self.provisioner._resolve_subnets(self.spec)[0]
                slots = tuple(
                    RetainedSlot(
                        token=uuid4().hex,
                        created_at=utc_now(),
                        launch_template_id=self.state.launch_template_id,
                        launch_template_version=self.state.launch_template_version,
                        host_revision=self.state.host_revision,
                        subnet_id=subnet,
                        serving=running,
                        phase=SlotPhase.Resuming if running else SlotPhase.Preparing,
                    )
                    for running in additions[:room]
                )
                self._save(self.state.model_copy(update={"slots": (*self.state.slots, *slots)}))
        self._actions(inventory)
        return self.describe()

    def _launch_room(self) -> int:
        """Machines the pool may launch before it holds the running and stopped counts.

        A retiring slot is leaving and no longer counts, so it neither delays a
        launch nor makes a reserve resume in place of one.
        """
        held = sum(slot.phase is not SlotPhase.Retiring for slot in self.state.slots)
        return max(self.request.desired_machines + self.request.stopped_machines - held, 0)

    def describe(self, inventory: dict[str, _Instance] | None = None) -> ProviderUnitSnapshot:
        """The pool as EC2 reports it, from `inventory` when the caller already read it.

        A stop the caller just requested is not in that inventory yet, and the
        slot's own phase already reports it as stopping.
        """
        inventory = self._inventory() if inventory is None else inventory
        instances: list[ProviderUnitInstance] = []
        for slot in self.state.slots:
            instance = inventory.get(slot.token)
            if slot.phase is SlotPhase.Retiring and slot.instance_id:
                instances.append(
                    ProviderUnitInstance(
                        provider_instance_id=slot.instance_id,
                        status=ProviderMachineStatus.Pending
                        if slot.serving
                        else ProviderMachineStatus.Stopping,
                        availability_zone=slot.availability_zone,
                        storage_volume_ids=slot.storage_volume_ids,
                        booted_template_version=slot.host_revision,
                    )
                )
                continue
            if instance is None or instance.state.name == "terminated":
                continue
            if instance.state.name == "shutting-down":
                status = ProviderMachineStatus.Pending
            elif slot.phase in {SlotPhase.Stopping, SlotPhase.Stopped}:
                status = (
                    ProviderMachineStatus.Stopped
                    if instance.state.name == "stopped"
                    else ProviderMachineStatus.Stopping
                )
            elif slot.phase is SlotPhase.Active:
                status = (
                    ProviderMachineStatus.Active
                    if instance.state.name == "running"
                    else ProviderMachineStatus.Unhealthy
                )
            elif slot.phase in {SlotPhase.Preparing, SlotPhase.Refreshing}:
                status = ProviderMachineStatus.Preparing
            else:
                status = ProviderMachineStatus.Resuming
            instances.append(
                ProviderUnitInstance(
                    provider_instance_id=instance.id,
                    status=status,
                    availability_zone=instance.placement.zone,
                    storage_volume_ids=tuple(
                        d.machine_volume_id for d in instance.devices if d.machine_volume_id
                    ),
                    booted_template_version=slot.host_revision,
                    hibernates=instance.hibernation.configured,
                )
            )
        return ProviderUnitSnapshot(
            phase=ProviderCapacityPhase.Ready
            if self.state.launch_template_id
            else ProviderCapacityPhase.Deleted,
            resource_id=self.recorded.resource_id,
            desired_machines=self.request.desired_machines,
            stopped_machines=self.request.stopped_machines,
            max_machines=self.request.max_machines,
            observed_machines=sum(
                slot.serving
                and (
                    (slot.phase is SlotPhase.Retiring and bool(slot.instance_id))
                    or (
                        slot.token in inventory and inventory[slot.token].state.name != "terminated"
                    )
                )
                for slot in self.state.slots
            ),
            instances=instances,
            last_capacity_failure_at=self.state.last_capacity_failure_at,
            last_capacity_failure_code=self.state.last_capacity_failure_code,
            provider_state=self.recorded,
            current_template_version=self.state.host_revision,
        )

    def _slot(self, instance_id: str, inventory: dict[str, _Instance]) -> RetainedSlot:
        self._record_instances(inventory)
        slot = next((s for s in self.state.slots if s.instance_id == instance_id), None)
        if slot is None:
            raise ValueError("instance is not owned by this retained pool")
        return slot

    def complete_preparation(self, instance_id: str, *, hibernate: bool) -> ProviderUnitSnapshot:
        inventory = self._inventory()
        slot = self._slot(instance_id, inventory)
        if slot.phase in {SlotPhase.Preparing, SlotPhase.Refreshing}:
            self._update(
                slot.model_copy(update={"phase": SlotPhase.Stopping, "hibernate": hibernate})
            )
        elif slot.phase is SlotPhase.Resuming:
            self._update(slot.model_copy(update={"phase": SlotPhase.Active}))
        self._actions(inventory)
        return self.describe(inventory)

    def refresh(self, instance_id: str) -> ProviderUnitSnapshot:
        """Start a stopped reserve so its agent prepares it again, then stop it."""
        inventory = self._inventory()
        slot = self._slot(instance_id, inventory)
        if slot.phase is not SlotPhase.Stopped or slot.serving:
            raise ValueError("only a stopped reserve can be prepared again")
        self._update(slot.model_copy(update={"phase": SlotPhase.Refreshing}))
        self._actions(inventory)
        return self.describe()

    def stop(self, instance_id: str) -> ProviderUnitSnapshot:
        inventory = self._inventory()
        slot = self._slot(instance_id, inventory)
        if slot.phase in {SlotPhase.Stopping, SlotPhase.Stopped}:
            self._actions(inventory)
            return self.describe()
        if slot.phase is not SlotPhase.Active:
            raise ValueError("only a drained active instance can return to reserve")
        reserves = sum(
            not s.serving and s.phase is not SlotPhase.Retiring for s in self.state.slots
        )
        if reserves >= self.request.stopped_machines:
            raise ValueError("instance has no admitted stopped reserve slot")
        self._update(
            slot.model_copy(
                update={"serving": False, "phase": SlotPhase.Stopping, "hibernate": False}
            )
        )
        self._actions(inventory)
        return self.describe()

    def release(self, instance_id: str) -> ProviderUnitSnapshot:
        self._record_instances(self._inventory())
        slot = next((s for s in self.state.slots if s.instance_id == instance_id), None)
        if slot is None:
            return self.describe()
        self._update(slot.model_copy(update={"phase": SlotPhase.Retiring}))
        self._actions(self._inventory())
        return self.describe()

    def delete(self) -> ProviderUnitSnapshot:
        self._record_instances(self._inventory())
        self._save(
            self.state.model_copy(
                update={
                    "slots": tuple(
                        s.model_copy(update={"phase": SlotPhase.Retiring}) for s in self.state.slots
                    )
                }
            )
        )
        self._actions(self._inventory())
        if self.state.slots:
            return self.describe().model_copy(update={"phase": ProviderCapacityPhase.Deleting})
        if self.state.launch_template_id:
            self.provisioner._ignore_missing(
                "delete retained pool launch template",
                self.clients.ec2.delete_launch_template,
                LaunchTemplateId=self.state.launch_template_id,
            )
        self._save(RetainedPoolState(namespace_id=self.state.namespace_id), deleted=True)
        return self.describe()
