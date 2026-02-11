from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.tasks.jobs import deploy as deploy_job


@pytest.mark.asyncio
async def test_deploy_job_reconciles_custom_domains_after_sync(monkeypatch):
    deployment = SimpleNamespace(
        workspace_id="ws-1",
        name="app",
        namespace="lc-app",
        cluster_id="ash-1",
        compose_yaml="old-compose",
    )
    validation_result = SimpleNamespace(
        deployment=deployment,
        helm_values=SimpleNamespace(),
        secrets=[],
        compose_file=SimpleNamespace(
            services=[SimpleNamespace(domain="new.example.com")]
        ),
    )

    sync_mock = AsyncMock()
    reconcile_mock = AsyncMock(return_value=(["old.example.com"], []))

    monkeypatch.setattr(
        deploy_job,
        "check_deployment_idempotency",
        AsyncMock(return_value=SimpleNamespace(current_helm_values=None)),
    )
    monkeypatch.setattr(
        deploy_job,
        "prepare_deployment",
        AsyncMock(return_value=validation_result),
    )
    monkeypatch.setattr(
        deploy_job,
        "prepare_namespace_config",
        AsyncMock(return_value=SimpleNamespace()),
    )
    monkeypatch.setattr(deploy_job, "deploy_namespace_resources", AsyncMock())
    monkeypatch.setattr(deploy_job, "delete_existing_jobs", AsyncMock())
    monkeypatch.setattr(deploy_job, "create_release_name", lambda *_: "release-name")
    monkeypatch.setattr(
        deploy_job,
        "deploy_application",
        AsyncMock(return_value=SimpleNamespace(revision=2)),
    )
    monkeypatch.setattr(deploy_job, "register_custom_domains", AsyncMock())
    monkeypatch.setattr(deploy_job, "sync_deployment_to_db", sync_mock)
    monkeypatch.setattr(
        deploy_job,
        "reconcile_custom_domains_for_deployment",
        reconcile_mock,
    )
    monkeypatch.setattr(deploy_job, "update_deployment_state", AsyncMock())

    result = await deploy_job.deploy_compose_job(
        ctx=SimpleNamespace(),
        deployment_id="dep-1",
    )

    assert result == {"status": "success", "deployment_id": "dep-1"}
    deploy_job.register_custom_domains.assert_awaited_once_with(["new.example.com"])
    sync_mock.assert_awaited_once()
    reconcile_mock.assert_awaited_once_with(
        deployment_id="dep-1",
        previous_compose_yaml="old-compose",
        current_compose_file=validation_result.compose_file,
    )
