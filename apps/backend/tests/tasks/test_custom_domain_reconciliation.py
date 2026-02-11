from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.services.compose.parser import ComposeParser
from backend.tasks.core import tasks as core_tasks


def _compose_yaml_with_domains(domains: list[str]) -> str:
    lines = ["version: '3.8'", "services:"]
    for idx, domain in enumerate(domains):
        lines.extend(
            [
                f"  svc{idx}:",
                "    image: nginx:latest",
                "    labels:",
                f"      lazycloud.domain: {domain}",
            ]
        )
    return "\n".join(lines)


class _FakeDbContext:
    def __init__(self, deployments: list[SimpleNamespace]):
        self._db = SimpleNamespace(
            compose_deployments=SimpleNamespace(
                find=AsyncMock(return_value=deployments),
            )
        )

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_reconcile_custom_domains_normalizes_and_deduplicates(monkeypatch):
    compose_yaml = _compose_yaml_with_domains(["API.EXAMPLE.COM.", "api.example.com"])
    compose_file = ComposeParser.parse_dict(
        {
            "version": "3.8",
            "services": {
                "svc0": {
                    "image": "nginx:latest",
                    "labels": {"lazycloud.domain": "new.example.com"},
                }
            },
        }
    )
    mock_unregister = AsyncMock()
    monkeypatch.setattr(core_tasks, "unregister_custom_domains", mock_unregister)
    monkeypatch.setattr(
        core_tasks,
        "_find_referenced_custom_domains",
        AsyncMock(return_value=set()),
    )

    removed, skipped = await core_tasks.reconcile_custom_domains_for_deployment(
        deployment_id="dep-1",
        previous_compose_yaml=compose_yaml,
        current_compose_file=compose_file,
    )

    mock_unregister.assert_awaited_once_with(["api.example.com"])
    assert removed == ["api.example.com"]
    assert skipped == []


@pytest.mark.asyncio
async def test_reconcile_custom_domains_checks_compose_and_pending_references(monkeypatch):
    deployments = [
        SimpleNamespace(
            id="dep-a",
            compose_yaml=_compose_yaml_with_domains(["old.example.com"]),
            pending_compose_yaml=None,
        ),
        SimpleNamespace(
            id="dep-b",
            compose_yaml=_compose_yaml_with_domains(["another.example.com"]),
            pending_compose_yaml=_compose_yaml_with_domains(["pending.example.com"]),
        ),
    ]

    monkeypatch.setattr(
        core_tasks,
        "get_db_context",
        lambda: _FakeDbContext(deployments),
    )
    mock_unregister = AsyncMock()
    monkeypatch.setattr(core_tasks, "unregister_custom_domains", mock_unregister)

    current_compose_file = ComposeParser.parse_dict(
        {
            "version": "3.8",
            "services": {
                "svc": {
                    "image": "nginx:latest",
                    "labels": {"lazycloud.domain": "new.example.com"},
                }
            },
        }
    )
    removed, skipped = await core_tasks.reconcile_custom_domains_for_deployment(
        deployment_id="dep-current",
        previous_compose_yaml=_compose_yaml_with_domains(
            ["old.example.com", "pending.example.com", "unused.example.com"]
        ),
        current_compose_file=current_compose_file,
    )

    mock_unregister.assert_awaited_once_with(["unused.example.com"])
    assert removed == ["unused.example.com"]
    assert skipped == ["old.example.com", "pending.example.com"]


@pytest.mark.asyncio
async def test_reconcile_custom_domains_no_stale_domains_is_noop(monkeypatch):
    mock_unregister = AsyncMock()
    monkeypatch.setattr(
        core_tasks,
        "_find_referenced_custom_domains",
        AsyncMock(),
    )
    monkeypatch.setattr(core_tasks, "unregister_custom_domains", mock_unregister)

    removed, skipped = await core_tasks.reconcile_custom_domains_for_deployment(
        deployment_id="dep-1",
        previous_compose_yaml=_compose_yaml_with_domains(["same.example.com"]),
        current_compose_file=ComposeParser.parse_dict(
            {
                "version": "3.8",
                "services": {
                    "svc": {
                        "image": "nginx:latest",
                        "labels": {"lazycloud.domain": "same.example.com"},
                    }
                },
            }
        ),
    )

    core_tasks._find_referenced_custom_domains.assert_not_awaited()
    mock_unregister.assert_not_awaited()
    assert removed == []
    assert skipped == []


@pytest.mark.asyncio
async def test_reconcile_custom_domains_parse_failure_is_fail_safe(monkeypatch):
    deployments = [
        SimpleNamespace(
            id="dep-a",
            compose_yaml="not: [valid",
            pending_compose_yaml=None,
        ),
    ]
    monkeypatch.setattr(
        core_tasks,
        "get_db_context",
        lambda: _FakeDbContext(deployments),
    )
    mock_unregister = AsyncMock()
    monkeypatch.setattr(core_tasks, "unregister_custom_domains", mock_unregister)

    current_compose_file = ComposeParser.parse_dict(
        {
            "version": "3.8",
            "services": {
                "svc": {
                    "image": "nginx:latest",
                    "labels": {"lazycloud.domain": "new.example.com"},
                }
            },
        }
    )
    removed, skipped = await core_tasks.reconcile_custom_domains_for_deployment(
        deployment_id="dep-current",
        previous_compose_yaml=_compose_yaml_with_domains(["old.example.com"]),
        current_compose_file=current_compose_file,
    )

    mock_unregister.assert_not_awaited()
    assert removed == []
    assert skipped == ["old.example.com"]
