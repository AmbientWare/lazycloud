from __future__ import annotations

from enum import StrEnum

from database.records.apps import AppRecord, StubKind, StubRecord
from pydantic import JsonValue
from shared.contracts import ContractModel
from shared.deployment_records import Deployment
from shared.identity import ConcurrencyLimitRecord, WorkspaceRecord


class ConcurrencyAcquireStatus(StrEnum):
    Acquired = "acquired"
    Saturated = "saturated"
    Released = "released"


class ConcurrencyAcquireResult(ContractModel):
    status: ConcurrencyAcquireStatus
    acquired: bool
    record: ConcurrencyLimitRecord
    available_before: int
    available_after: int
    reason: str


class StubUrlPlan(ContractModel):
    stub: StubRecord
    url: str
    external_url: str
    route_kind: StubKind
    deployment: Deployment | None = None


class StubConfigUpdateResult(ContractModel):
    stub: StubRecord
    updated_fields: tuple[str, ...]
    message: str


class StubCloneResult(ContractModel):
    source_stub: StubRecord
    cloned_stub: StubRecord
    app: AppRecord
    copied_config: dict[str, JsonValue]
    copied_objects: tuple[str, ...] = ()


class WorkspaceCreateResult(ContractModel):
    workspace_id: str
    token: str = ""
    """The primary credential, readable only on the call that minted it.

    Empty when the workspace already held one: a token is stored hashed, so an
    existing primary can be pointed at but never handed back.
    """

    workspace: WorkspaceRecord
