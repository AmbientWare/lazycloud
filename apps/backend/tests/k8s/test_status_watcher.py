"""Tests for status watcher deployment-level status aggregation."""

from datetime import UTC, datetime

from models.k8s import WorkloadType
from models.statuses import PodStatus, ServiceStatus, StatusPhase

from backend.services.k8s.status_watcher import StatusWatcher


def _make_service(
    status: StatusPhase,
    *,
    pods: list[PodStatus] | None = None,
    replicas: int = 1,
    ready_replicas: int = 1,
) -> ServiceStatus:
    return ServiceStatus(
        name="web",
        image="nginx:latest",
        workload_type=WorkloadType.DEPLOYMENT,
        status=status,
        replicas=replicas,
        ready_replicas=ready_replicas,
        pods=pods,
        last_checked=datetime.now(UTC),
    )


def _make_watcher() -> StatusWatcher:
    return StatusWatcher(
        deployment_id="dep-1",
        namespace="ns-1",
        helm_values=None,  # type: ignore[arg-type]
        cluster_id="cluster-1",
        deployment_name="app-1",
        deployed_at=None,
    )


class TestStatusWatcherDeploymentStatus:
    def test_uses_service_status_when_pods_are_skipped(self):
        """Fast status path (no pods) should keep service.status as source of truth."""
        watcher = _make_watcher()
        services = [_make_service(StatusPhase.RUNNING, pods=[])]

        status = watcher._determine_deployment_status(services)

        assert status == StatusPhase.RUNNING

    def test_pod_phase_still_takes_precedence_when_pods_exist(self):
        """When pods are present, deployment status should still derive from pod phases."""
        watcher = _make_watcher()
        services = [
            _make_service(
                StatusPhase.RUNNING,
                pods=[
                    PodStatus(
                        name="web-abc",
                        phase=StatusPhase.HEALTH_CHECK,
                        ready_containers=0,
                        total_containers=1,
                        restart_count=0,
                        age="1m",
                        node="node-1",
                    )
                ],
                ready_replicas=0,
            )
        ]

        status = watcher._determine_deployment_status(services)

        assert status == StatusPhase.HEALTH_CHECK
