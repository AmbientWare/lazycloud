from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from api.server.services import ApiServices
from database.repositories.apps import DeploymentRepository
from database.repositories.deployment_plans import DeploymentPlanRepository
from execution.containers.service import PendingContainerReservation
from operations.management import ManagementService
from shared.containers import ContainerStatus
from shared.deployment_records import DeploymentSpec
from shared.deployments import DeploymentKind
from shared.errors import ConflictError
from shared.http.deployment_plans import (
    DeploymentPlanRequest,
    DeploymentPruneRequest,
    WorkloadIdentity,
)
from shared.tasks import TaskStatus


def test_prune_retires_all_omitted_versions_and_schedules_but_preserves_other_workloads(
    isolated_services: ApiServices,
) -> None:
    services = isolated_services
    app = services.apps.create("pruned")
    other = services.apps.create("other")
    stale = [
        services.deployments.deploy(
            DeploymentSpec(
                name="old",
                handler="pkg:old",
                cron="0 * * * *",
                metadata={"app_id": app.id},
            )
        )
        for _ in range(2)
    ]
    ManagementService(services).set_deployment_active("default", stale[-1].id, active=False)
    sibling = services.deployments.deploy(
        DeploymentSpec(
            name="old",
            handler="pkg:other",
            metadata={"app_id": other.id},
        )
    )
    kept = services.deployments.deploy(
        DeploymentSpec(
            name="keep",
            handler="pkg:keep",
            metadata={"app_id": app.id},
        )
    )
    queued = services.tasks.create(
        "queued",
        workspace_id=app.workspace_id,
        app_id=app.id,
        stub_id=stale[0].stub_id,
        deployment_id=stale[0].id,
    )
    reservation = PendingContainerReservation(
        name="old-container",
        workspace_id=app.workspace_id,
        app_id=app.id,
        stub_id=stale[0].stub_id,
        image="image",
        command=["python"],
    )
    with services.context.database.session() as session:
        container = services.containers.reserve_pending(session, reservation)
    manifest = DeploymentPlanRequest(
        app=app.name,
        prune=True,
        workloads=[WorkloadIdentity(kind=DeploymentKind.Function, name="keep")],
    )
    plan = services.deployment_plans.plan(manifest, workspace="default")
    current = services.deployments.deploy(kept.spec)
    request = DeploymentPruneRequest(
        **manifest.model_dump(),
        operation_id=uuid4(),
        app_id=plan.app_id,
        snapshot=plan.snapshot,
        deployment_ids=[UUID(current.id)],
    )
    result = services.deployment_plans.prune(request, workspace="default")
    assert result.complete and result.removed_versions == 2
    assert services.deployment_plans.prune(request, workspace="default") == result
    assert {item.id for item in services.deployments.list(app_id=app.id)} == {kept.id, current.id}
    assert services.deployments.get(sibling.id).active
    assert services.cron_jobs.list() == []
    assert services.apps.get(app.id).active
    assert services.containers.get(container.id).status is ContainerStatus.Stopped
    assert services.tasks.get(queued.id).status is TaskStatus.Cancelled
    with (
        services.context.database.session() as session,
        pytest.raises(ConflictError, match="stopped or deleted"),
    ):
        services.containers.reserve_pending(session, reservation)
    with services.context.database.session() as session:
        repository = DeploymentRepository(session)
        for deployment in stale:
            retired = repository.get(
                deployment.id, workspace_id=app.workspace_id, include_deleted=True
            )
            assert retired is not None and retired.deleted_at is not None and not retired.active
        with pytest.raises(ConflictError, match="cannot be restored"):
            repository.upsert(stale[0], workspace_id=app.workspace_id)


def test_changed_app_or_incomplete_submission_refuses_pruning(
    isolated_services: ApiServices,
) -> None:
    services = isolated_services
    app = services.apps.create("conflicting")
    old = services.deployments.deploy(
        DeploymentSpec(
            name="old",
            handler="pkg:old",
            metadata={"app_id": app.id},
        )
    )
    manifest = DeploymentPlanRequest(
        app=app.name,
        prune=True,
        workloads=[WorkloadIdentity(kind=DeploymentKind.Function, name="new")],
    )
    plan = services.deployment_plans.plan(manifest, workspace="default")
    request = DeploymentPruneRequest(
        **manifest.model_dump(),
        operation_id=uuid4(),
        app_id=app.id,
        snapshot=plan.snapshot,
        deployment_ids=[],
    )
    with pytest.raises(ConflictError, match="successful deployment"):
        services.deployment_plans.prune(request, workspace="default")
    new = services.deployments.deploy(
        DeploymentSpec(
            name="new",
            handler="pkg:new",
            metadata={"app_id": app.id},
        )
    )
    concurrent = services.deployments.deploy(old.spec)
    request.deployment_ids = [UUID(new.id)]
    with pytest.raises(ConflictError, match="changed since"):
        services.deployment_plans.prune(request, workspace="default")
    assert services.deployments.get(old.id).active
    assert services.deployments.get(concurrent.id).active


def test_interrupted_prune_resumes_exact_targets_without_deleting_redeployment(
    isolated_services: ApiServices,
) -> None:
    services = isolated_services
    app = services.apps.create("resumable")
    old = services.deployments.deploy(
        DeploymentSpec(
            name="old",
            handler="pkg:old",
            metadata={"app_id": app.id},
        )
    )
    manifest = DeploymentPlanRequest(app=app.name, workloads=[], prune=True)
    plan = services.deployment_plans.plan(manifest, workspace="default")
    request = DeploymentPruneRequest(
        **manifest.model_dump(),
        operation_id=uuid4(),
        app_id=app.id,
        snapshot=plan.snapshot,
        deployment_ids=[],
    )

    class UnavailableExecution:
        def delete_deployment_execution(
            self, *, workspace_id: str, deployment_ids: list[str]
        ) -> None:
            raise RuntimeError("worker shutdown unavailable")

    interrupted = replace(services.deployment_plans, execution=UnavailableExecution())
    with pytest.raises(RuntimeError, match="shutdown unavailable"):
        interrupted.prune(request, workspace="default")
    with services.context.database.session() as session:
        pending = DeploymentPlanRepository(session).operation(
            str(request.operation_id), workspace_id=app.workspace_id
        )
        assert pending is not None and not pending.complete
    replacement = services.deployments.deploy(old.spec)
    services.deployment_plans.reconcile_pending()
    with services.context.database.session() as session:
        completed = DeploymentPlanRepository(session).operation(
            str(request.operation_id), workspace_id=app.workspace_id
        )
        assert completed is not None and completed.complete
    assert services.deployments.get(replacement.id).active
    assert services.apps.get(app.id).active
