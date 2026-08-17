from __future__ import annotations

from dataclasses import dataclass

from database.records.apps import StubKind, StubRecord
from pydantic import Field, JsonValue
from shared.autoscaler_state import AutoscalerStateRecord, AutoscalerTargetKind
from shared.contracts import ContractModel
from shared.events import Event, EventLevel
from shared.worker_events import AUTOSCALER_SCALE_DECISION_ACTIONS

from scheduler.autoscaling import (
    ENDPOINT_AUTOSCALER_SOURCE,
    FUNCTION_AUTOSCALER_SOURCE,
    POD_AUTOSCALER_SOURCE,
    EndpointAutoscalingService,
    FunctionAutoscalingService,
    PodAutoscalingService,
    SchedulerServices,
)


class AutoscalerStatusItem(ContractModel):
    state: AutoscalerStateRecord
    stub_name: str = ""
    stub_kind: str = ""
    autoscaling_enabled: bool = True


class AutoscalerStatusResponse(ContractModel):
    items: list[AutoscalerStatusItem] = Field(default_factory=list)


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
    function_autoscaler: FunctionAutoscalingService
    endpoint_autoscaler: EndpointAutoscalingService
    pod_autoscaler: PodAutoscalingService

    def status(
        self,
        *,
        workspace: str | None = None,
        target_kind: AutoscalerTargetKind | None = None,
        target_id: str | None = None,
    ) -> AutoscalerStatusResponse:
        workspace_id = self._workspace_id(workspace)
        source = _source_for_kind(target_kind) if target_kind is not None else None
        states = self.services.autoscaler_states.list(workspace_id=workspace_id, source=source)
        if target_id:
            states = [state for state in states if state.target_id == target_id]
        stubs = {
            stub.id: stub
            for stub in self.services.scheduler_workloads.list_stubs(workspace=workspace_id)
        }
        return AutoscalerStatusResponse(
            items=[
                AutoscalerStatusItem(
                    state=state,
                    stub_name=stubs[state.target_id].name if state.target_id in stubs else "",
                    stub_kind=stubs[state.target_id].kind.value if state.target_id in stubs else "",
                    autoscaling_enabled=_autoscaling_enabled(stubs.get(state.target_id)),
                )
                for state in states
            ]
        )

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
            stub = self.services.scheduler_workloads.get_stub(
                stub_id_or_name,
                workspace=workspace,
            )
            kind = target_kind or _target_kind_for_stub(stub)
            return AutoscalerReconcileResponse(
                results=[
                    _dump_result(self._service_for_kind(kind).reconcile_stub(stub)),
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
        stub = self.services.scheduler_workloads.set_autoscaling_enabled(
            stub_id_or_name,
            workspace=workspace,
            enabled=enabled,
        )
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

    def _service_for_kind(
        self,
        target_kind: AutoscalerTargetKind,
    ) -> FunctionAutoscalingService | EndpointAutoscalingService | PodAutoscalingService:
        if target_kind is AutoscalerTargetKind.Function:
            return self.function_autoscaler
        if target_kind is AutoscalerTargetKind.Endpoint:
            return self.endpoint_autoscaler
        return self.pod_autoscaler

    def _workspace_id(self, workspace: str | None) -> str | None:
        if workspace is None:
            return None
        return self.services.scheduler_workloads.get_workspace(workspace).id


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
    if stub.kind is StubKind.Pod:
        return AutoscalerTargetKind.Pod
    msg = f"stub is not autoscaled: {stub.id}"
    raise ValueError(msg)


def _autoscaling_enabled(stub: StubRecord | None) -> bool:
    if stub is None:
        return False
    raw = stub.config.metadata.get("autoscaling_enabled", True)
    return raw is not False


def _dump_result(result: ContractModel) -> dict[str, JsonValue]:
    payload = result.model_dump(mode="json")
    return payload if isinstance(payload, dict) else {}
