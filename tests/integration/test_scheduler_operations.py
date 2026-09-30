from datetime import timedelta

from api.fastapi_app import create_app
from api.server.services import ApiServices
from database.repositories.cron_jobs import CronJobRepository
from fastapi.testclient import TestClient
from shared.deployment_records import DeploymentSpec
from shared.deployments import DeploymentKind
from shared.http.operations import CronJobRunListResponse
from shared.timestamps import utc_now
from tests.workspaces import administrator_credential


def test_manual_cron_tick_enqueues_and_exposes_durable_run(
    isolated_services: ApiServices,
) -> None:
    deployment = isolated_services.deployments.deploy(
        DeploymentSpec(
            name="manual-cron",
            kind=DeploymentKind.Function,
            handler="module:scheduled",
            cron="every 1m",
        )
    )
    job = isolated_services.cron_jobs.list()[0]
    job.next_run_at = utc_now() - timedelta(seconds=1)
    with isolated_services.context.database.session() as session:
        CronJobRepository(session).upsert(job, workspace_id=job.workspace_id)
    token, _ = administrator_credential(isolated_services.context)
    with TestClient(create_app(isolated_services)) as client:
        client.headers["Authorization"] = f"Bearer {token}"
        response = client.post("/api/v1/scheduler/tick")
        assert response.status_code == 201, response.text
        runs = CronJobRunListResponse.model_validate(response.json()).data
        assert len(runs) == 1
        assert runs[0].enqueued, runs[0].reason
        assert runs[0].task_id is not None
        task = isolated_services.tasks.get(runs[0].task_id)
        assert task.stub_id == deployment.stub_id
        history = client.get("/api/v1/cron-job-runs", params={"workspace_id": job.workspace_id})
        assert history.status_code == 200, history.text
        saved = CronJobRunListResponse.model_validate(history.json()).data
        assert [item.id for item in saved] == [runs[0].id]
        assert saved[0].task_id == task.id
