from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from api.server.services import ApiServices
from database.records.apps import StubRecord
from database.repositories.cron_jobs import CronJobRepository
from database.repositories.execution import CronJobRunRepository
from execution.functions.service import FunctionAdmission, FunctionControlService
from scheduler.cron import CronScheduler
from shared.cron import CronJobRun
from shared.deployment_records import DeploymentSpec
from shared.errors import UpstreamUnavailableError
from shared.http.functions import FunctionInvokeBody
from shared.tasks import Task
from shared.timestamps import utc_now
from shared.usage import UsageRecord


def test_concurrent_ticks_admit_one_task_for_the_same_occurrence(
    isolated_services: ApiServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = isolated_services
    services.deployments.deploy(
        DeploymentSpec(name="concurrent", handler="pkg:run", cron="every 1m")
    )
    job = services.cron_jobs.list()[0]
    assert job.next_run_at is not None
    barrier = Barrier(2)
    prepare = FunctionControlService.prepare_admission

    def concurrent_prepare(
        self: FunctionControlService, requests: Sequence[FunctionInvokeBody], *, stub: StubRecord
    ) -> FunctionAdmission:
        admission = prepare(self, requests, stub=stub)
        barrier.wait(timeout=10)
        return admission

    monkeypatch.setattr(FunctionControlService, "prepare_admission", concurrent_prepare)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                CronScheduler(services.cron_jobs, FunctionControlService(services)).tick,
                now=job.next_run_at,
            )
            for _ in range(2)
        ]
        runs = [run for future in futures for run in future.result(timeout=15)]
    assert len(runs) == 1
    assert runs[0].enqueued
    assert runs[0].scheduled_at == job.next_run_at
    assert runs[0].schedule_revision == job.revision
    assert [task.id for task in services.tasks.list()] == [runs[0].task_id]
    assert services.cron_jobs.list_cron_job_runs(workspace_id=job.workspace_id).data == tuple(runs)


@pytest.mark.parametrize("after_commit", [False, True])
def test_cron_recovery_across_commit_keeps_one_task_and_run(
    isolated_services: ApiServices,
    monkeypatch: pytest.MonkeyPatch,
    after_commit: bool,
) -> None:
    services = isolated_services
    services.deployments.deploy(DeploymentSpec(name="recover", handler="pkg:run", cron="every 1m"))
    job = services.cron_jobs.list()[0]
    assert job.next_run_at is not None
    scheduler = CronScheduler(services.cron_jobs, FunctionControlService(services))
    append = CronJobRunRepository.append

    def interrupt_transaction(self: CronJobRunRepository, run: CronJobRun) -> CronJobRun:
        append(self, run)
        raise RuntimeError("interrupted before commit")

    def interrupt_notification(
        self: FunctionControlService, tasks: Sequence[Task], usage: Sequence[UsageRecord]
    ) -> None:
        raise RuntimeError("interrupted after commit")

    with monkeypatch.context() as fault:
        if after_commit:
            fault.setattr(FunctionControlService, "publish_admission", interrupt_notification)
        else:
            fault.setattr(CronJobRunRepository, "append", interrupt_transaction)
        with pytest.raises(RuntimeError, match="interrupted"):
            scheduler.tick(now=job.next_run_at)
    assert len(services.tasks.list()) == int(after_commit)
    recovered = scheduler.tick(now=job.next_run_at)
    assert len(recovered) == int(not after_commit)
    history = services.cron_jobs.list_cron_job_runs(workspace_id=job.workspace_id).data
    assert len(history) == 1
    assert history[0].scheduled_at == job.next_run_at
    assert history[0].enqueued
    assert [task.id for task in services.tasks.list()] == [history[0].task_id]
    next_run_at = services.cron_jobs.list()[0].next_run_at
    assert next_run_at is not None and next_run_at > job.next_run_at


@pytest.mark.parametrize("mutation", ["replace", "remove", "stop", "pause", "delete"])
def test_lifecycle_change_invalidates_a_selected_occurrence(
    isolated_services: ApiServices,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    services = isolated_services
    app = services.apps.create("schedule_owner")
    spec = DeploymentSpec(
        name="changing", handler="pkg:run", cron="every 1m", metadata={"app_id": app.id}
    )
    deployment = services.deployments.deploy(spec)
    job = services.cron_jobs.list()[0]
    assert job.next_run_at is not None
    prepare = FunctionControlService.prepare_admission

    def prepare_then_change(
        self: FunctionControlService, requests: Sequence[FunctionInvokeBody], *, stub: StubRecord
    ) -> FunctionAdmission:
        admission = prepare(self, requests, stub=stub)
        if mutation == "replace":
            services.deployments.deploy(spec.model_copy(update={"handler": "pkg:replacement"}))
        elif mutation == "remove":
            services.cron_jobs.delete(job.name)
        elif mutation == "stop":
            services.deployments.set_deployment_active("default", deployment.id, active=False)
        elif mutation == "pause":
            services.apps.pause(app.id)
        else:
            services.apps.delete(app.id)
        return admission

    with monkeypatch.context() as change:
        change.setattr(FunctionControlService, "prepare_admission", prepare_then_change)
        assert (
            CronScheduler(services.cron_jobs, FunctionControlService(services)).tick(
                now=job.next_run_at
            )
            == []
        )
    assert services.tasks.list() == []
    assert services.cron_jobs.list_cron_job_runs(workspace_id=job.workspace_id).data == ()


def test_transient_admission_failure_retains_due_work_without_blocking_other_jobs(
    isolated_services: ApiServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = isolated_services
    for name in ("unavailable", "available"):
        services.deployments.deploy(DeploymentSpec(name=name, handler="pkg:run", cron="every 1m"))
    jobs = services.cron_jobs.list()
    now = utc_now() + timedelta(minutes=1)
    prepare = FunctionControlService.prepare_admission

    def prepare_with_outage(
        self: FunctionControlService, requests: Sequence[FunctionInvokeBody], *, stub: StubRecord
    ) -> FunctionAdmission:
        if stub.name == "unavailable":
            raise UpstreamUnavailableError("admission dependency unavailable")
        return prepare(self, requests, stub=stub)

    scheduler = CronScheduler(services.cron_jobs, FunctionControlService(services))
    with monkeypatch.context() as fault:
        fault.setattr(FunctionControlService, "prepare_admission", prepare_with_outage)
        with pytest.raises(UpstreamUnavailableError):
            scheduler.tick(now=now)
    assert len(services.tasks.list()) == 1
    assert len(scheduler.tick(now=now)) == 1
    assert len(services.tasks.list()) == 2
    assert len(services.cron_jobs.list_cron_job_runs(workspace_id=jobs[0].workspace_id).data) == 2


def test_app_resume_moves_schedule_forward_and_does_not_catch_up_paused_time(
    isolated_services: ApiServices,
) -> None:
    services = isolated_services
    app = services.apps.create("paused_schedule")
    services.deployments.deploy(
        DeploymentSpec(
            name="resume", handler="pkg:run", cron="every 1m", metadata={"app_id": app.id}
        )
    )
    services.apps.pause(app.id)
    job = services.cron_jobs.list()[0]
    job.next_run_at = utc_now() - timedelta(days=2)
    with services.database.session() as session:
        CronJobRepository(session).upsert(job, workspace_id=job.workspace_id)
    resumed_at = utc_now()
    services.apps.resume(app.id)
    resumed = services.cron_jobs.list()[0]
    assert resumed.enabled
    assert resumed.next_run_at is not None and resumed.next_run_at > resumed_at
    assert (
        CronScheduler(services.cron_jobs, FunctionControlService(services)).tick(now=resumed_at)
        == []
    )
