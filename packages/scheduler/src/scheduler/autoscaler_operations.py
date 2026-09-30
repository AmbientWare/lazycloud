from __future__ import annotations

from dataclasses import dataclass

from database.records.apps import StubKind, StubRecord
from database.records.autoscaling import AutoscalerStatus
from database.repositories.orchestration import AutoscalerStateRepository
from pydantic import Field, JsonValue
from shared.autoscaler_state import AutoscalerTargetKind
from shared.contracts import ContractModel
from shared.events import Event, EventLevel
from shared.worker_events import AUTOSCALER_SCALE_DECISION_ACTIONS

from scheduler.autoscaling import (
    ENDPOINT_AUTOSCALER_SOURCE,
    FUNCTION_AUTOSCALER_SOURCE,
    POD_AUTOSCALER_SOURCE,
    AutoscalingDriver,
    SchedulerServices,
)


class AutoscalerStatusResponse(ContractModel):
    items: list[AutoscalerStatus] = Field(default_factory=list)


class AutoscalerHistoryResponse(ContractModel):
    events: list[Event] = Field(default_factory=list)


class AutoscalerControlResponse(ContractModel):
    stub: StubRecord
    target_kind: AutoscalerTargetKind
    autoscaling_enabled: bool


class AutoscalerReconcileResponse(ContractModel):
    results: list[dict[str, JsonValue]] = Field(default_factory=list)


@dataclass(slots=True)
class AutoscalerOperationsService:
    services: SchedulerServices
    function_autoscaler: AutoscalingDriver
    endpoint_autoscaler: AutoscalingDriver
    pod_autoscaler: AutoscalingDriver

    def status(
        self,
        *,
        workspace: str | None = None,
        target_kind: AutoscalerTargetKind | None = None,
        target_id: str | None = None,
    ) -> AutoscalerStatusResponse:
        workspace_id = self._workspace_id(workspace)
        source = _source_for_kind(target_kind) if target_kind is not None else None
        with self.services.context.database.session() as session:
            items = AutoscalerStateRepository(session).status(
                workspace_id=workspace_id, source=source, target_id=target_id
            )
        return AutoscalerStatusResponse(items=items)

    def history(
        self,
        *,
        workspace: str | None = None,
        target_id: str | None = None,
        limit: int = 100,
    ) -> AutoscalerHistoryResponse:
        workspace_id = self._workspace_id(workspace)
        events = self.services.events.list(
            workspace_id=workspace_id,
            resource_id=target_id,
            actions=tuple(AUTOSCALER_SCALE_DECISION_ACTIONS),
            limit=max(limit, 0),
        )
        return AutoscalerHistoryResponse(events=events)

    def pause(
        self,
        stub_id_or_name: str,
        *,
        workspace: str = "default",
    ) -> AutoscalerControlResponse:
        return self._set_enabled(stub_id_or_name, workspace=workspace, enabled=False)

    def resume(
        self,
        stub_id_or_name: str,
        *,
        workspace: str = "default",
    ) -> AutoscalerControlResponse:
        return self._set_enabled(stub_id_or_name, workspace=workspace, enabled=True)

    def reconcile(
        self,
        *,
        target_kind: AutoscalerTargetKind | None = None,
        stub_id_or_name: str | None = None,
        workspace: str = "default",
    ) -> AutoscalerReconcileResponse:
        if stub_id_or_name is not None:
            stub = self.services.control_plane_service.get_stub(
                stub_id_or_name,
                workspace=workspace,
            )
            kind = target_kind or _target_kind_for_stub(stub)
            records = self.services.control_plane_service.list_autoscaling_stubs([stub.id])
            return AutoscalerReconcileResponse(
                results=[
                    _dump_result(result)
                    for result in self._service_for_kind(kind).reconcile(records)
                ]
            )
        if target_kind is not None:
            kind_results = self._service_for_kind(target_kind).reconcile()
            return AutoscalerReconcileResponse(
                results=[_dump_result(result) for result in kind_results]
            )
        results: list[ContractModel] = []
        for kind in AutoscalerTargetKind:
            results.extend(self._service_for_kind(kind).reconcile())
        return AutoscalerReconcileResponse(results=[_dump_result(result) for result in results])

    def _set_enabled(
        self,
        stub_id_or_name: str,
        *,
        workspace: str,
        enabled: bool,
    ) -> AutoscalerControlResponse:
        stub = self.services.control_plane_service.update_stub_config(
            stub_id_or_name,
            workspace=workspace,
            fields={"metadata.autoscaling_enabled": enabled},
        ).stub
        target_kind = _target_kind_for_stub(stub)
        self.services.events.emit(
            "autoscaler.resumed" if enabled else "autoscaler.paused",
            resource_type="stub",
            resource_id=stub.id,
            message=(
                f"resumed autoscaler for {stub.name}"
                if enabled
                else f"paused autoscaler for {stub.name}"
            ),
            level=EventLevel.Info,
            data={
                "target_kind": target_kind.value,
                "autoscaling_enabled": enabled,
            },
            workspace_id=stub.workspace_id,
        )
        return AutoscalerControlResponse(
            stub=stub,
            target_kind=target_kind,
            autoscaling_enabled=enabled,
        )

    def _service_for_kind(self, target_kind: AutoscalerTargetKind) -> AutoscalingDriver:
        if target_kind is AutoscalerTargetKind.Function:
            return self.function_autoscaler
        if target_kind is AutoscalerTargetKind.Endpoint:
            return self.endpoint_autoscaler
        return self.pod_autoscaler

    def _workspace_id(self, workspace: str | None) -> str | None:
        if workspace is None:
            return None
        return self.services.control_plane_service.get_workspace(workspace).id


def _source_for_kind(target_kind: AutoscalerTargetKind) -> str:
    if target_kind is AutoscalerTargetKind.Function:
        return FUNCTION_AUTOSCALER_SOURCE
    if target_kind is AutoscalerTargetKind.Endpoint:
        return ENDPOINT_AUTOSCALER_SOURCE
    return POD_AUTOSCALER_SOURCE


def _target_kind_for_stub(stub: StubRecord) -> AutoscalerTargetKind:
    if stub.kind is StubKind.Function:
        return AutoscalerTargetKind.Function
    if stub.kind in {StubKind.Endpoint, StubKind.Asgi}:
        return AutoscalerTargetKind.Endpoint
    # A sandbox is scaled by the pod workload, so it has a kind to name.
    if stub.kind in {StubKind.Pod, StubKind.Sandbox}:
        return AutoscalerTargetKind.Pod
    msg = f"stub is not autoscaled: {stub.id}"
    raise ValueError(msg)


def _dump_result(result: ContractModel) -> dict[str, JsonValue]:
    payload = result.model_dump(mode="json")
    return payload if isinstance(payload, dict) else {}
