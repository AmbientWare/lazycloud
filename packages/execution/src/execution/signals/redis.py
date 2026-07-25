from __future__ import annotations

from dataclasses import dataclass

from coordination.redis_client import RedisClient
from shared.errors import InvalidInputError, UpstreamUnavailableError
from shared.signals import (
    SignalClearPlan,
    SignalClearRequest,
    SignalClearResponse,
    SignalMonitorRequest,
    SignalMonitorSnapshot,
    SignalSetPlan,
    SignalSetRequest,
    SignalSetResponse,
    SignalStatus,
    SignalValue,
    plan_signal_clear,
    plan_signal_monitor_snapshot,
    plan_signal_read,
    plan_signal_set,
    signal_name,
    signal_response_from_clear_plan,
    signal_response_from_set_plan,
)


@dataclass(slots=True)
class RedisSignalRepository:
    redis: RedisClient

    def set(self, request: SignalSetRequest) -> SignalSetPlan:
        plan = plan_signal_set(request)
        if plan.status is SignalStatus.InvalidName:
            raise InvalidInputError(plan.error_message)
        try:
            self.redis.set(
                self._key(plan.signal_key),
                plan.value,
                ex=plan.ttl_seconds,
            )
        except Exception as exc:
            raise UpstreamUnavailableError(str(exc)) from exc
        return plan

    def clear(self, request: SignalClearRequest) -> SignalClearPlan:
        plan = plan_signal_clear(request.workspace_name, request.name)
        if plan.status is SignalStatus.InvalidName:
            raise InvalidInputError(plan.error_message)
        try:
            deleted = bool(self.redis.delete(self._key(plan.signal_key)))
        except Exception as exc:
            raise UpstreamUnavailableError(str(exc)) from exc
        return plan.model_copy(update={"deleted": deleted})

    def read(self, request: SignalMonitorRequest) -> SignalValue:
        try:
            return self.redis.get(self._key(signal_name(request.workspace_name, request.name)))
        except Exception as exc:
            raise UpstreamUnavailableError(str(exc)) from exc

    def delete_workspace(self, workspace_name: str) -> int:
        return self.redis.delete_matching(self.redis.key("signal", workspace_name, "*"))

    def _key(self, logical_key: str) -> str:
        return self.redis.key(logical_key)


@dataclass(slots=True)
class RedisSignalService:
    repository: RedisSignalRepository

    def signal_set(self, request: SignalSetRequest) -> SignalSetResponse:
        return signal_response_from_set_plan(self.repository.set(request))

    def signal_set_plan(self, request: SignalSetRequest) -> SignalSetPlan:
        return self.repository.set(request)

    def signal_clear(self, request: SignalClearRequest) -> SignalClearResponse:
        return signal_response_from_clear_plan(self.repository.clear(request))

    def signal_clear_plan(self, request: SignalClearRequest) -> SignalClearPlan:
        return self.repository.clear(request)

    def signal_monitor_once(
        self,
        request: SignalMonitorRequest,
        *,
        has_handler: bool = False,
        clear_after_interval_seconds: int | None = None,
    ) -> SignalMonitorSnapshot:
        value = self.repository.read(request)
        read = plan_signal_read(request.workspace_name, request.name, value)
        if read.status is SignalStatus.InvalidName:
            raise InvalidInputError(read.error_message)
        return plan_signal_monitor_snapshot(
            request.workspace_name,
            request.name,
            value,
            has_handler=has_handler,
            clear_after_interval_seconds=clear_after_interval_seconds,
        )

    def delete_workspace(self, workspace_name: str) -> int:
        return self.repository.delete_workspace(workspace_name)
