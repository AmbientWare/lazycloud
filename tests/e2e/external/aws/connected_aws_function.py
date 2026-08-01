"""Run one paid public Function on the connected-AWS warm baseline.

Invoking this explicitly named scenario authorizes its one bounded paid
Function. The stage mutates only through the public SDK: it deploys the
example app, submits one Function call carrying a unique run marker plus the
value ``21``, and proves the public task completed with ``42``, retained the
marker in public logs, exited its container successfully, and ran on the
exact warm-baseline machine of the connected AWS account. Success, task
failure, timeout, and interruption all enter the same cleanup path: cancel
live marker work, delete the exact app, and prove the public pool, cost, and
tag-scoped AWS inventory returned to the pre-submit warm baseline. Reusing
the same ``--run-id`` recovers the one matching durable task instead of
submitting a duplicate; ``--cleanup-only`` restores the baseline without
submitting work. Tailnet device corroboration remains owned by the tailnet
stages.

Run from the repository root:

``uv run --env-file .env python -m tests.e2e.external.aws.connected_aws_function
--run-id <id> --app-slug <slug>``
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from examples.connected_aws_function import connected_aws_probe
from lazycloud.cli.control import compute_client, control_config, resource_client, task_client
from lazycloud.clients.compute.control import ComputeClient
from lazycloud.clients.resource.control import ResourceControlClient
from lazycloud.clients.workspace.control import WorkspaceControlClient
from lazycloud.session.task import TaskClient
from pydantic import RootModel
from shared.app_slug import validate_app_slug
from shared.aws_connections import AwsAccountConnectionPhase
from shared.compute_enrollment import MachineServiceState
from shared.http.apps import AppResponse
from shared.http.aws_connections import AwsConnectionResponse
from shared.http.compute_policy import WorkspaceComputeInstanceResponse
from shared.http.tasks import TaskResponse
from shared.tasks import is_terminal_task_status
from tests.e2e.external import _support

_RUN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
_EXAMPLES_ROOT = Path(__file__).resolve().parents[4] / "examples"


@dataclass(frozen=True, slots=True)
class WarmBaseline:
    total: int
    ready: int
    pending: int
    degraded: int
    hourly_micros: int
    instance_id: str
    machine_id: str
    region: str
    instance_type: str


def _connected_instances(
    client: ComputeClient, connection: AwsConnectionResponse
) -> list[WorkspaceComputeInstanceResponse]:
    return [item for item in client.instances().data if item.provider == f"aws:{connection.id}"]


def _warm_baseline(client: ComputeClient, connection: AwsConnectionResponse) -> WarmBaseline | None:
    summary = client.summary()
    connected = _connected_instances(client, connection)
    ready = [
        item
        for item in connected
        if item.service_state is MachineServiceState.Serving and item.machine_id is not None
    ]
    if (
        summary.instances.total != 1
        or summary.instances.ready != 1
        or summary.instances.pending != 0
        or summary.instances.degraded != 0
        or summary.cost.hourly_micros <= 0
        or len(connected) != 1
        or len(ready) != 1
    ):
        return None
    machine = ready[0]
    if machine.machine_id is None or machine.instance_type is None:
        return None
    return WarmBaseline(
        total=summary.instances.total,
        ready=summary.instances.ready,
        pending=summary.instances.pending,
        degraded=summary.instances.degraded,
        hourly_micros=summary.cost.hourly_micros,
        instance_id=machine.id,
        machine_id=machine.machine_id,
        region=machine.region,
        instance_type=machine.instance_type,
    )


def _owned_app(resources: ResourceControlClient, app_slug: str) -> AppResponse | None:
    matches = [app for app in resources.list_apps(active=True).data if app.name == app_slug]
    if len(matches) > 1:
        raise RuntimeError("the exact Function app resolved more than once")
    return matches[0] if matches else None


def _recover_task(tasks: TaskClient, app_id: str, marker: str) -> TaskResponse | None:
    matches = [task for task in tasks.list(app_id=app_id, limit=100) if task.args[:1] == [marker]]
    if len(matches) > 1:
        raise RuntimeError("the run ID resolved more than one durable task; refusing ambiguity")
    return matches[0] if matches else None


def _corroborate_warm_aws(baseline: WarmBaseline, workspace_id: str) -> None:
    """Read-only, tag-scoped proof that AWS holds exactly the baseline unit."""
    filters = [
        "Name=tag:cloud-pool:managed-by,Values=control-plane",
        f"Name=tag:cloud-pool:workspace,Values={workspace_id}",
    ]
    instance_ids = _support.run_aws(
        [
            "ec2",
            "describe-instances",
            "--region",
            baseline.region,
            "--filters",
            *filters,
            "Name=instance-state-name,Values=pending,running,stopping,stopped",
            "--query",
            "Reservations[].Instances[].InstanceId",
            "--output",
            "json",
        ]
    )
    desired = _support.run_aws(
        [
            "autoscaling",
            "describe-auto-scaling-groups",
            "--region",
            baseline.region,
            "--query",
            (
                "AutoScalingGroups[?"
                "Tags[?Key=='cloud-pool:managed-by' && Value=='control-plane'] && "
                f"Tags[?Key=='cloud-pool:workspace' && Value=='{workspace_id}']"
                "].DesiredCapacity"
            ),
            "--output",
            "json",
        ]
    )
    observed_ids = RootModel[list[str]].model_validate(instance_ids or []).root
    if observed_ids != [baseline.instance_id]:
        raise RuntimeError(
            f"AWS reports instances {observed_ids} instead of the exact warm baseline "
            f"{[baseline.instance_id]}"
        )
    observed_desired = RootModel[list[int]].model_validate(desired or []).root
    if sum(observed_desired) != 1:
        raise RuntimeError("AWS Auto Scaling desired capacity is not the exact warm baseline")


def _cleanup(
    resources: ResourceControlClient,
    tasks: TaskClient,
    compute: ComputeClient,
    connection: AwsConnectionResponse,
    app_slug: str,
    marker: str,
    baseline: WarmBaseline | None,
    workspace_id: str,
    deadline: _support.Deadline,
) -> WarmBaseline:
    app = _owned_app(resources, app_slug)
    if app is not None:
        for task in tasks.list(app_id=app.id, limit=100):
            if task.args[:1] == [marker] and not is_terminal_task_status(task.status):
                tasks.cancel(task.id)
        resources.delete_app(app.id)
        _support.poll_until(
            deadline,
            "the exact Function app to leave the public listing",
            lambda: True if _owned_app(resources, app_slug) is None else None,
            interval_seconds=2.0,
        )

    def restored() -> WarmBaseline | None:
        observed = _warm_baseline(compute, connection)
        if observed is None:
            return None
        if baseline is not None and observed.machine_id != baseline.machine_id:
            raise RuntimeError(
                "the warm baseline machine changed during the run: "
                f"expected {baseline.machine_id}, observed {observed.machine_id}"
            )
        return observed

    restored_baseline = _support.poll_until(
        deadline, "the pre-submit warm baseline to be restored", restored
    )
    _corroborate_warm_aws(restored_baseline, workspace_id)
    return restored_baseline


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--app-slug", required=True)
    parser.add_argument("--cleanup-only", action="store_true")
    parser.add_argument("--timeout", type=float, default=900)
    args = parser.parse_args(argv)
    if _RUN_ID.fullmatch(args.run_id) is None:
        parser.error("--run-id must be lowercase letters, digits, or dashes")
    try:
        validate_app_slug(args.app_slug)
    except ValueError as exc:
        parser.error(f"--app-slug is invalid: {exc}")
    if not 0 < args.timeout <= 900:
        parser.error("--timeout must be at most the supported 900 seconds")
    deadline = _support.Deadline(args.timeout)
    marker = f"connected-aws:{args.run_id}"

    compute = compute_client(timeout_seconds=30)
    resources = resource_client(timeout_seconds=30)
    tasks = task_client(timeout_seconds=30)
    try:
        _support.ambient_aws_account_id()
        connection = _support.first_public_call(compute.current_connection)
    except _support.MissingPrerequisite as exc:
        return _support.skip("connected-AWS Function", exc)
    if connection is None or connection.phase is not AwsAccountConnectionPhase.Ready:
        raise RuntimeError("the public AWS connection is not ready")
    config = control_config(timeout_seconds=30)
    workspace_id = (
        WorkspaceControlClient.from_endpoint(
            config.endpoint,
            token=config.token,
            timeout_seconds=30,
            workspace=config.workspace,
        )
        .current()
        .id
    )

    baseline = _warm_baseline(compute, connection)
    if args.cleanup_only:
        restored = _cleanup(
            resources,
            tasks,
            compute,
            connection,
            args.app_slug,
            marker,
            baseline,
            workspace_id,
            deadline,
        )
        _support.emit_evidence(
            {
                "app_slug": args.app_slug,
                "capability": "function.connected_aws.cleanup",
                "machine_id": restored.machine_id,
                "run_id": args.run_id,
            }
        )
        return 0
    if baseline is None:
        raise RuntimeError(
            "the one-machine AWS warm baseline is not ready; refusing to submit paid work"
        )

    app = _owned_app(resources, args.app_slug)
    recovered = _recover_task(tasks, app.id, marker) if app is not None else None
    task_id = recovered.id if recovered is not None else ""
    try:
        if recovered is None:
            probe_app = connected_aws_probe(args.app_slug)
            probe_app.app.deploy(source_root=_EXAMPLES_ROOT)
            call = probe_app.probe.spawn(marker, 21)
            task_id = call.task_id
        _support.emit_evidence(
            {
                "app_slug": args.app_slug,
                "capability": "function.connected_aws.submitted",
                "recovered": recovered is not None,
                "run_id": args.run_id,
                "task_id": task_id,
            }
        )

        result = tasks.handle(task_id).result(
            wait=True,
            timeout_seconds=deadline.remaining_seconds(),
            poll_interval_seconds=2.0,
        )
        if not result.ok:
            raise RuntimeError(
                f"the public task did not complete successfully: status={result.status}, "
                f"exit_code={result.exit_code}, error={result.error or 'none'}"
            )
        expected: dict[str, str | int] = {"marker": marker, "doubled": 42, "status": "complete"}
        if result.value != expected:
            raise RuntimeError(f"the public Function returned {result.value!r}, not {expected!r}")

        _support.poll_until(
            deadline,
            "the public task logs to retain the Function marker",
            lambda: True if marker in tasks.output(task_id, limit=250) else None,
            interval_seconds=2.0,
        )

        detail = tasks.detail(task_id)
        container = detail.container
        if container is None or container.exit_code not in {None, 0}:
            raise RuntimeError(
                "the Function container did not exit successfully: "
                f"{container.exit_code if container is not None else 'no container projection'}"
            )
        if container.machine_id != baseline.machine_id:
            raise RuntimeError(
                f"the Function ran on machine {container.machine_id}, not the warm baseline "
                f"machine {baseline.machine_id}"
            )
    except BaseException:
        if task_id:
            print(
                f"recovery: rerun with --run-id {args.run_id} --app-slug {args.app_slug} "
                f"(task {task_id}); --cleanup-only restores the baseline without resubmitting",
                file=sys.stderr,
            )
        raise
    finally:
        _cleanup(
            resources,
            tasks,
            compute,
            connection,
            args.app_slug,
            marker,
            baseline,
            workspace_id,
            deadline,
        )

    _support.emit_evidence(
        {
            "account_id": connection.account_id,
            "app_slug": args.app_slug,
            "capability": "function.connected_aws",
            "hourly_micros": baseline.hourly_micros,
            "instance_id": baseline.instance_id,
            "instance_type": baseline.instance_type,
            "machine_id": baseline.machine_id,
            "marker": marker,
            "region": baseline.region,
            "result": 42,
            "run_id": args.run_id,
            "task_id": task_id,
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
