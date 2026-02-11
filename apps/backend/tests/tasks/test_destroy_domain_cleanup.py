from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.tasks.jobs import destroy as destroy_job


class _FakeDbContext:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _make_deployment() -> SimpleNamespace:
    return SimpleNamespace(
        id="dep-1",
        workspace_id="ws-1",
        name="app",
        namespace="lc-app",
        cluster_id="ash-1",
        compose_yaml="old-compose",
        pending_compose_yaml=None,
        deleted_at=None,
        state=None,
        status_message=None,
        current_task_run_id="task-1",
    )


def _make_fake_db(deployment: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
        compose_deployments=SimpleNamespace(
            get_by_id=AsyncMock(side_effect=[deployment, deployment]),
            update=AsyncMock(return_value=deployment),
        )
    )


@pytest.mark.asyncio
async def test_destroy_job_uses_reconciliation_for_domain_cleanup(monkeypatch):
    deployment = _make_deployment()
    fake_db = _make_fake_db(deployment)
    fake_helm = SimpleNamespace(
        destroy=AsyncMock(
            side_effect=[
                SimpleNamespace(success=True, error=None),
                SimpleNamespace(success=True, error=None),
            ]
        )
    )
    reconcile_mock = AsyncMock(return_value=(["old.example.com"], []))

    monkeypatch.setattr(
        destroy_job, "get_db_context", lambda: _FakeDbContext(fake_db)
    )
    monkeypatch.setattr(destroy_job, "update_deployment_state", AsyncMock())
    monkeypatch.setattr(
        destroy_job,
        "reconcile_custom_domains_for_deployment",
        reconcile_mock,
    )
    monkeypatch.setattr(destroy_job, "HelmManager", lambda _: fake_helm)
    monkeypatch.setattr(destroy_job, "create_release_name", lambda *_: "release-name")
    monkeypatch.setattr(
        destroy_job,
        "get_depot_service",
        lambda: SimpleNamespace(delete_deployment_project=AsyncMock()),
    )

    result = await destroy_job.destroy_compose_job(
        ctx=SimpleNamespace(),
        deployment_id="dep-1",
    )

    assert result == {"status": "success", "deployment_id": "dep-1"}
    reconcile_mock.assert_awaited_once_with(
        deployment_id="dep-1",
        previous_compose_yaml="old-compose",
        current_compose_file=None,
    )
    assert fake_helm.destroy.await_count == 2


@pytest.mark.asyncio
async def test_destroy_job_domain_cleanup_failure_is_non_fatal(monkeypatch):
    deployment = _make_deployment()
    fake_db = _make_fake_db(deployment)
    fake_helm = SimpleNamespace(
        destroy=AsyncMock(
            side_effect=[
                SimpleNamespace(success=True, error=None),
                SimpleNamespace(success=True, error=None),
            ]
        )
    )
    reconcile_mock = AsyncMock(side_effect=RuntimeError("cloudflare timeout"))

    monkeypatch.setattr(
        destroy_job, "get_db_context", lambda: _FakeDbContext(fake_db)
    )
    monkeypatch.setattr(destroy_job, "update_deployment_state", AsyncMock())
    monkeypatch.setattr(
        destroy_job,
        "reconcile_custom_domains_for_deployment",
        reconcile_mock,
    )
    monkeypatch.setattr(destroy_job, "HelmManager", lambda _: fake_helm)
    monkeypatch.setattr(destroy_job, "create_release_name", lambda *_: "release-name")
    monkeypatch.setattr(
        destroy_job,
        "get_depot_service",
        lambda: SimpleNamespace(delete_deployment_project=AsyncMock()),
    )

    result = await destroy_job.destroy_compose_job(
        ctx=SimpleNamespace(),
        deployment_id="dep-1",
    )

    assert result == {"status": "success", "deployment_id": "dep-1"}
    reconcile_mock.assert_awaited_once()
    assert fake_helm.destroy.await_count == 2
