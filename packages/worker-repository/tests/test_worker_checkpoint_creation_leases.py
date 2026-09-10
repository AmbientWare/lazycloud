from __future__ import annotations

from control.service import ControlPlaneService
from database.context import ServiceContext
from tests.real_redis import RealRedisActors
from tests.workspaces import owned_workspace
from worker.checkpoints import (
    CheckpointStateOperation,
    CheckpointStatePayload,
    WorkerCheckpointStatus,
)
from worker_repository.checkpoint_records import (
    AUTOMATIC_CHECKPOINT_LEASE_NAMESPACE,
    AutomaticCheckpointCreationLeaseService,
    CheckpointService,
)


def test_automatic_checkpoint_creation_lease_serializes_first_creator(
    service_context: ServiceContext,
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    service = AutomaticCheckpointCreationLeaseService(service_context, redis)
    control = ControlPlaneService(service_context)
    workspace = owned_workspace(control, "checkpoint-owner")
    stub = control.create_stub("checkpoint-lease", workspace=workspace.id)

    first = service.acquire(
        workspace_id=workspace.id,
        stub_id=stub.id,
        owner_token="container-1",
        ttl_seconds=2400,
    )
    second = service.acquire(
        workspace_id=workspace.id,
        stub_id=stub.id,
        owner_token="container-2",
        ttl_seconds=2400,
    )
    key = redis.key(AUTOMATIC_CHECKPOINT_LEASE_NAMESPACE, workspace.id, stub.id)

    assert first.acquired
    assert not second.acquired
    assert 0 < redis.ttl(key) <= 2400
    assert not service.release(
        workspace_id=workspace.id,
        stub_id=stub.id,
        owner_token="container-2",
    )
    assert service.release(
        workspace_id=workspace.id,
        stub_id=stub.id,
        owner_token="container-1",
    )
    assert redis.get(key) is None


def test_automatic_checkpoint_creation_lease_rechecks_available_artifact_after_lock(
    service_context: ServiceContext,
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    control = ControlPlaneService(service_context)
    workspace = owned_workspace(control, "default")
    stub = control.create_stub("checkpoint-lease", workspace=workspace.id)
    CheckpointService(service_context).save_state(
        CheckpointStatePayload(
            operation=CheckpointStateOperation.Create,
            checkpoint_id="checkpoint-available",
            status=WorkerCheckpointStatus.Available,
            workspace_id=workspace.id,
            stub_id=stub.id,
        )
    )
    service = AutomaticCheckpointCreationLeaseService(service_context, redis)

    decision = service.acquire(
        workspace_id=workspace.id,
        stub_id=stub.id,
        owner_token="container-1",
        ttl_seconds=2400,
    )

    assert not decision.acquired
    assert decision.available_checkpoint_id == "checkpoint-available"
    assert redis.get(redis.key(AUTOMATIC_CHECKPOINT_LEASE_NAMESPACE, workspace.id, stub.id)) is None
