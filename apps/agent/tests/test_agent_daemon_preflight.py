from __future__ import annotations

from agent.service_manager import AgentPreflightProbeSet, PreflightCheckName
from agent_app.daemon import detect_agent_resource_plan


def test_resource_detection_reports_required_host_preflight() -> None:
    plan = detect_agent_resource_plan(
        probes=AgentPreflightProbeSet(
            os_name="linux",
            effective_uid=0,
            docker_command=True,
            docker_daemon=True,
            docker_host_network=True,
            network_namespace=True,
            netns_run_dir_writable=True,
            ip_forward_enabled_or_writable=True,
            iptables_nat=True,
            fuse_device=True,
        )
    )

    names = {check.name for check in plan.checks}
    assert PreflightCheckName.ContainerRuntime in names
    assert PreflightCheckName.DockerDaemon in names
    assert PreflightCheckName.NetworkNamespace in names
    assert PreflightCheckName.Fuse in names
    assert plan.schedulable is True


def test_resource_detection_blocks_failed_required_preflight() -> None:
    plan = detect_agent_resource_plan(
        probes=AgentPreflightProbeSet(
            os_name="linux",
            effective_uid=0,
            docker_command=True,
            docker_daemon=False,
            docker_host_network=True,
            network_namespace=True,
            netns_run_dir_writable=True,
            ip_forward_enabled_or_writable=True,
            iptables_nat=True,
            fuse_device=True,
        )
    )

    docker_daemon = next(
        check for check in plan.checks if check.name is PreflightCheckName.DockerDaemon
    )
    assert docker_daemon.ok is False
    assert plan.schedulable is False
