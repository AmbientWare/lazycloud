from __future__ import annotations

import importlib

release_plan = importlib.import_module("deploy.aws-release-assets.plan")

_INPUTS = release_plan.ArtifactInputs(
    worker=("docker/Dockerfile.worker", "packages/shared/", "packages/worker/", "uv.lock"),
    agent=("packages/agent/", "packages/shared/", "packages/scheduler/", "uv.lock"),
)

_PREVIOUS = release_plan.PreviousRelease(
    version="0.0.5",
    worker_image="public.ecr.aws/x/lazycloud/container-worker@sha256:" + "a" * 64,
    agent_sha256="b" * 64,
    agent_url="https://example.invalid/agents/0.0.5/x/lazycloud-agent-linux-amd64",
    agent_size_bytes=48273808,
    cpu_ami_ids={"us-east-1": "ami-0123456789abcdef0"},
    gpu_ami_ids={"us-east-1": "ami-0123456789abcdef1"},
)


def test_a_control_plane_only_change_reuses_every_artifact() -> None:
    plan = release_plan.plan_release(["deploy/RUNBOOK.md", "apps/api/x.py"], _PREVIOUS, _INPUTS)
    assert (plan.rebuild_worker, plan.rebuild_agent, plan.rebuild_images) == (False, False, False)
    outputs = plan.as_outputs()
    assert outputs["previous_worker_image"] == _PREVIOUS.worker_image
    assert outputs["previous_cpu_ami_ids"] == '{"us-east-1": "ami-0123456789abcdef0"}'
    assert '"node_images": "0.0.5"' in outputs["reused_from"]


def test_a_worker_change_rebuilds_the_worker_and_the_images_but_not_the_agent() -> None:
    plan = release_plan.plan_release(["docker/Dockerfile.worker"], _PREVIOUS, _INPUTS)
    assert (plan.rebuild_worker, plan.rebuild_agent, plan.rebuild_images) == (True, False, True)
    assert "worker" in plan.reasons["images"]


def test_an_agent_only_change_rebuilds_the_agent_and_reuses_the_images() -> None:
    """A node fetches the agent by hash at boot; the image only caches it."""
    plan = release_plan.plan_release(
        ["packages/scheduler/src/scheduler/fleet.py"], _PREVIOUS, _INPUTS
    )
    assert (plan.rebuild_worker, plan.rebuild_agent, plan.rebuild_images) == (False, True, False)


def test_tests_and_docs_beside_shipped_code_are_not_inputs() -> None:
    plan = release_plan.plan_release(
        ["packages/scheduler/tests/test_fleet.py", "packages/shared/AGENTS.md"], _PREVIOUS, _INPUTS
    )
    assert (plan.rebuild_worker, plan.rebuild_agent, plan.rebuild_images) == (False, False, False)


def test_a_shared_package_change_rebuilds_both_because_both_ship_it() -> None:
    plan = release_plan.plan_release(["packages/shared/src/shared/routing.py"], _PREVIOUS, _INPUTS)
    assert (plan.rebuild_worker, plan.rebuild_agent, plan.rebuild_images) == (True, True, True)


def test_no_previous_release_builds_everything() -> None:
    plan = release_plan.plan_release([], None, _INPUTS)
    assert (plan.rebuild_worker, plan.rebuild_agent, plan.rebuild_images) == (True, True, True)
    assert plan.as_outputs()["reused_from"] == "{}"


def test_inputs_come_from_the_workspace_dependency_graph() -> None:
    """The agent bundles agent-app's workspace closure; the worker ships
    container-worker-app's plus the managed runtime packages. A dependency added
    to either pyproject counts without anyone editing a list."""
    inputs = release_plan.artifact_inputs()
    assert {"apps/agent/", "packages/agent/", "packages/scheduler/", "packages/shared/"} <= set(
        inputs.agent
    )
    assert {
        "apps/container-worker/",
        "packages/worker/",
        "packages/runner/",
        "packages/lazycloud/",
    } <= set(inputs.worker)
    assert "docker/Dockerfile.worker" in inputs.worker
    assert "uv.lock" in inputs.agent
    assert "apps/api/" not in inputs.worker
