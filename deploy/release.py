"""Publish a connected-AWS release from the working tree, in one source state.

Every artifact a managed node consumes embeds this repository's source: the
container images, the agent executable, and the worker image the node pulls.
The managed runtime compares what the control plane expects against what the
worker resolves, so a release assembled from two source states is rejected at
container start as a package-digest mismatch — an error that names a digest and
not the stale artifact behind it.

`deploy/RUNBOOK.md` has said to build them together since the first live run,
and they were still assembled apart, because a sequence written down is a
sequence someone performs from memory at the point they are least able to. This
command performs it instead, and refuses the states where "one source state" is
not a fact: a dirty tree has no single revision to name, and a subset rebuild
has no way to prove the rest came from the same place.

It orchestrates the existing owners rather than reimplementing them —
`deploy/agent-binary/build.py` builds the executable and
`deploy/aws-release-assets/release.py` stages, publishes, and verifies.

Publishing is not arriving. A managed pool rolls its launch template forward and
leaves every running node on the version it booted with, so a release that
reached nothing used to exit exactly like one that reached everything, and the
divergence surfaced an hour later as that same digest mismatch. The command now
ends by naming, per node, which release is actually running there, and fails
when that is not this one.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
_AGENT_BUILD = "deploy/agent-binary/build.py"
_RELEASE = "deploy/aws-release-assets/release.py"

# The two processes that each compose a pool's launch-template bootstrap.
_BOOTSTRAP_PROCESSES = ("control-plane", "scheduler")
# Authored by the deployment rather than published by a release, and the one
# bootstrap input both processes read that a release therefore cannot align.
_GATEWAY_ORIGIN_VARIABLE = "LAZYCLOUD_GATEWAY_PUBLIC_HTTP_URL"
# The pooled reconcile runs on a 60s timer
# (`scheduler.service.MANAGED_COMPUTE_RECONCILE_INTERVAL_SECONDS`), so a reading
# taken sooner than that after a restart is the previous process's snapshot.
_REACH_POLL_SECONDS = 5.0
_REACH_BUDGET_SECONDS = 180.0


class ManifestInspection(BaseModel):
    """The one field of `docker manifest inspect --verbose` this needs."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    reference: str = Field(alias="Ref")


_MANIFEST_INSPECTION: TypeAdapter[ManifestInspection | list[ManifestInspection]] = TypeAdapter(
    ManifestInspection | list[ManifestInspection]
)


class ReleaseError(RuntimeError):
    """A release step failed, or its preconditions were not met."""


def _run(
    argv: Sequence[str],
    *,
    capture: bool = False,
    environment: dict[str, str] | None = None,
    stdin: str | None = None,
) -> str:
    result = subprocess.run(
        list(argv),
        cwd=_REPOSITORY_ROOT,
        capture_output=capture,
        text=True,
        check=False,
        input=stdin,
        env={**os.environ, **(environment or {})},
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-600:] if capture else ""
        raise ReleaseError(f"{' '.join(argv[:3])} failed: {detail}")
    return (result.stdout or "").strip()


def _in_service(service: str, argv: Sequence[str], *, stdin: str | None = None) -> str:
    return _run(["docker", "compose", "exec", "-T", service, *argv], capture=True, stdin=stdin)


def source_revision(*, allow_dirty: bool) -> str:
    """The one revision every artifact in this release is built from."""
    dirty = _run(["git", "status", "--porcelain"], capture=True)
    if dirty and not allow_dirty:
        raise ReleaseError(
            "the working tree has uncommitted changes, so no single revision names what "
            "these artifacts contain. Commit them, or pass --allow-dirty to publish a "
            "release whose version label is knowingly approximate."
        )
    return f"branch-{_run(['git', 'rev-parse', '--short', 'HEAD'], capture=True)}"


def build_images() -> None:
    """Build every source-bearing image, never a subset.

    The profile wildcard is the point: without it Compose builds only the
    default profile, which is a subset rebuild wearing the command that is
    supposed to prevent one.
    """
    _run(["docker", "compose", "build"], environment={"COMPOSE_PROFILES": "*"})


def build_agent(version: str, architectures: Sequence[str]) -> Path:
    argv = ["uv", "run", "--no-project", "python", _AGENT_BUILD, "build", "--version", version]
    for architecture in architectures:
        argv += ["--arch", architecture]
    _run([*argv, "--output", "dist/agent-binarys"])
    return _REPOSITORY_ROOT / "dist" / "agent-binarys" / version


def push_worker_image(repository: str, version: str) -> str:
    """Publish the worker image and return the digest the node will pull.

    The reference comes from `docker manifest inspect`, not from `RepoDigests`.
    Both name the same image, but only one of them qualifies the registry host,
    and the release verifier compares the pinned reference against what
    `docker manifest inspect` reports -- so pinning the other spelling publishes
    a release that cannot verify itself.
    """
    tag = f"{repository}:{version}"
    _run(["docker", "tag", "container-worker:local", tag])
    _run(["docker", "push", tag])
    local_digest = _run(
        ["docker", "inspect", tag, "--format", "{{index .RepoDigests 0}}"],
        capture=True,
    )
    if "@sha256:" not in local_digest:
        raise ReleaseError(f"worker image {tag} has no published digest to pin")
    inspected = _MANIFEST_INSPECTION.validate_json(
        _run(["docker", "manifest", "inspect", "--verbose", local_digest], capture=True)
    )
    inspections = inspected if isinstance(inspected, list) else [inspected]
    references = {inspection.reference for inspection in inspections}
    if len(references) != 1:
        raise ReleaseError(f"worker image {tag} resolved to more than one reference: {references}")
    return references.pop()


def publish_release(
    *,
    version: str,
    agent_dir: Path,
    worker_image: str,
    bucket: str,
    region: str,
    aws_cli: str,
) -> dict[str, str]:
    """Stage, validate, publish, and verify; return the deployment environment."""
    bundle = _REPOSITORY_ROOT / "dist" / "connected-aws"
    _run(
        [
            "uv",
            "run",
            "python",
            _RELEASE,
            "stage",
            "--version",
            version,
            "--agent-version-dir",
            str(agent_dir),
            "--worker-image",
            worker_image,
            "--bucket",
            bucket,
            "--region",
            region,
            "--output",
            str(bundle),
        ]
    )
    manifest_path = bundle / version / "manifest.json"
    for command in ("validate-local", "publish", "verify"):
        argv = ["uv", "run", "python", _RELEASE, command, "--manifest", str(manifest_path)]
        if command != "validate-local":
            argv += ["--aws-cli", aws_cli]
        _run(argv)
    manifest = json.loads(manifest_path.read_text())
    return dict(manifest["deployment_environment"])


def repoint_deployment(environment: dict[str, str]) -> list[str]:
    """Write the manifest's own values into `.env`, never a transcription of them."""
    path = _REPOSITORY_ROOT / ".env"
    lines = path.read_text().splitlines(keepends=True)
    written: set[str] = set()
    for index, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key in environment:
            lines[index] = f"{key}='{environment[key]}'\n"
            written.add(key)
    lines += [f"{key}='{value}'\n" for key, value in environment.items() if key not in written]
    path.write_text("".join(lines))
    return sorted(environment)


def restart_stack() -> None:
    """Restart, and put the sidecars back in the namespace they lost."""
    _run(["docker", "compose", "up", "-d"])
    # A recreated control plane takes both sidecars with it, and a plain `up -d`
    # leaves them attached to a namespace that no longer exists -- healthy, and
    # serving nothing.
    _run(["docker", "compose", "up", "-d", "--force-recreate", "tailnet-gateway", "public-ingress"])


def process_environment(service: str) -> dict[str, str]:
    """The environment a running service holds, which is the one it was created with."""
    entries = _in_service(service, ["env", "-0"]).split("\0")
    return dict(entry.split("=", 1) for entry in entries if "=" in entry)


def guard_bootstrap_agreement(release_environment: Mapping[str, str]) -> None:
    """Prove both bootstrap composers read this release, and read the same one.

    `apps/api` and `apps/scheduler` each build a pool's launch-template bootstrap
    from their own process settings, and `gateway.pool_bootstrap` says what a
    disagreement costs: the template alternates between two versions on every
    reconcile and no node is ever stable. A staleness number taken then measures
    nothing, so this is a precondition rather than another line of the report.

    The comparison is each running container's own environment, keyed by the
    variables this release publishes plus the origin the two call sites both warn
    about. A container keeps the environment it was created with, so one process
    left on the previous release shows up here rather than an hour later as a
    digest mismatch. What this cannot see: values that differ before settings
    normalization and agree after it, and any configuration layer beneath the
    environment.
    """
    names = sorted({*release_environment, _GATEWAY_ORIGIN_VARIABLE})
    observed = {service: process_environment(service) for service in _BOOTSTRAP_PROCESSES}
    divergent: list[str] = []
    unloaded: list[str] = []
    for name in names:
        held = [observed[service].get(name, "") for service in _BOOTSTRAP_PROCESSES]
        published = release_environment.get(name)
        if len(set(held)) != 1:
            detail = " ".join(
                f"{service}={value or '(unset)'}"
                for service, value in zip(_BOOTSTRAP_PROCESSES, held, strict=True)
            )
            divergent.append(f"{name}: {detail}")
            print(f"  {name}: DISAGREE {detail}", flush=True)
            continue
        value = held[0] or "(unset)"
        if published is not None and held[0] != published:
            unloaded.append(f"{name}: both hold {value}, this release published {published}")
            print(f"  {name}: both hold {value}, NOT this release", flush=True)
            continue
        origin = "this release" if published is not None else "the deployment"
        print(f"  {name}: both hold {value} ({origin})", flush=True)
    if divergent:
        raise ReleaseError(
            "the API and the scheduler compose different pool bootstraps, so the launch "
            "template alternates between versions on every reconcile and no node settles "
            "on either: " + "; ".join(divergent)
        )
    if unloaded:
        raise ReleaseError(
            "both processes agree, on a release that is not this one, so no node could "
            "have received it: " + "; ".join(unloaded)
        )


class PoolInstanceReading(BaseModel):
    """One node of a pooled pool, as the control plane last recorded it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    instance_id: str
    status: str
    booted_template_version: str
    observed_at: datetime


class PoolReading(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    phase: str
    desired_machines: int
    observed_machines: int
    expected_template_version: str
    observed_at: datetime
    instances: tuple[PoolInstanceReading, ...] = ()


class ReachReading(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    clock: datetime
    pools: tuple[PoolReading, ...] = ()


# The durable record the control plane writes on every pooled reconcile. The
# pool's newest launch-template version has no HTTP surface, and both values must
# come from one instant to be comparable at all, so both are read here. Nothing
# in this statement writes, and none of it reaches the provider: re-querying EC2
# would test EC2 rather than the release.
_REACH_QUERY = """
select json_build_object(
  'clock', now(),
  'pools', coalesce((
    select json_agg(pool order by pool->>'name')
    from (
      select json_build_object(
        'name', p.name,
        'phase', p.phase,
        'desired_machines', p.desired_machines,
        'observed_machines', p.observed_machines,
        'expected_template_version',
          coalesce(p.provider_state->'attributes'->>'launch_template_latest_version', ''),
        'observed_at', p.updated_at,
        'instances', coalesce((
          select json_agg(json_build_object(
            'instance_id', coalesce(i.instance_id, ''),
            'status', i.status,
            'booted_template_version',
              coalesce(i.payload->'metadata'->>'booted_template_version', ''),
            'observed_at', i.updated_at
          ) order by i.instance_id)
          from compute_provider_instances i
          where i.pool_id = p.id and i.status not in ('deleted', 'failed')
        ), '[]'::json)
      ) as pool
      from compute_pools p
      where p.capacity_mode = 'pooled' and p.phase <> 'deleted'
    ) pools
  ), '[]'::json)
);
"""


def read_reach() -> ReachReading:
    payload = _in_service(
        "postgres",
        ["sh", "-c", 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -t -A -q -f -'],
        stdin=_REACH_QUERY,
    )
    return ReachReading.model_validate_json(payload)


def _reach_settled(reading: ReachReading, *, since: datetime) -> bool:
    """Whether a further cycle could still change what the record says.

    Only being rewritten by a post-restart reconcile can. A row that comes back
    from one still missing a version is a finding to report, not a reason to keep
    reading the same answer until a deadline.
    """
    return all(
        pool.observed_at >= since
        and all(instance.observed_at >= since for instance in pool.instances)
        for pool in reading.pools
    )


def _reach_faults(reading: ReachReading, *, since: datetime) -> list[str]:
    faults: list[str] = []
    for pool in reading.pools:
        if pool.observed_at < since:
            faults.append(f"{pool.name} has not been reconciled since the restart")
        elif not pool.expected_template_version and pool.instances:
            faults.append(f"{pool.name} has no launch-template version on record")
        for instance in pool.instances:
            node = f"{pool.name}/{instance.instance_id}"
            if instance.observed_at < since:
                faults.append(f"{node} has not been observed since the restart")
            elif not instance.booted_template_version:
                faults.append(f"{node} does not report the template version it booted with")
            elif (
                pool.expected_template_version
                and instance.booted_template_version != pool.expected_template_version
            ):
                faults.append(
                    f"{node} is running template version {instance.booted_template_version}, "
                    f"not {pool.expected_template_version}"
                )
    return faults


def _print_reach(reading: ReachReading, *, since: datetime) -> None:
    for pool in reading.pools:
        print(
            f"  pool {pool.name} phase={pool.phase} desired={pool.desired_machines} "
            f"observed={pool.observed_machines} "
            f"template={pool.expected_template_version or '(none recorded)'} "
            f"{_read_age(pool.observed_at, reading.clock, since)}",
            flush=True,
        )
        for instance in pool.instances:
            booted = instance.booted_template_version
            if not booted:
                verdict = "booted=(not reported)"
            elif booted == pool.expected_template_version:
                verdict = f"booted={booted} current"
            else:
                verdict = f"booted={booted} STALE, expected {pool.expected_template_version}"
            print(
                f"    {instance.instance_id} status={instance.status} {verdict} "
                f"{_read_age(instance.observed_at, reading.clock, since)}",
                flush=True,
            )


def _read_age(observed_at: datetime, clock: datetime, since: datetime) -> str:
    age = int((clock - observed_at).total_seconds())
    return f"read {age}s ago" + ("" if observed_at >= since else ", from before the restart")


def report_release_reach(*, since: datetime) -> int:
    """Say, per node, whether the release reached it, and refuse to guess.

    Publishing a release does not replace a running instance: an Auto Scaling
    group rolls its launch template forward and leaves every node on the version
    it started with, so a release that reached nothing succeeds exactly like one
    that reached everything. Every node's booted version is printed whether or
    not it is stale, because a report that prints only problems cannot be told
    apart from one that failed to look.

    The reading must be newer than the restart. The record is refreshed on the
    pooled reconcile's own timer, so the poll prints each cycle rather than
    waiting: a control plane that never reconciles shows as an unchanging read
    age within seconds instead of at the deadline.
    """
    deadline = time.monotonic() + _REACH_BUDGET_SECONDS
    cycle = 0
    while True:
        cycle += 1
        reading = read_reach()
        print(f"release reach, cycle {cycle}:", flush=True)
        if not reading.pools:
            print("  no pooled pool exists, so this release has no managed node to reach")
            return 0
        _print_reach(reading, since=since)
        if _reach_settled(reading, since=since) or time.monotonic() >= deadline:
            break
        time.sleep(_REACH_POLL_SECONDS)
    faults = _reach_faults(reading, since=since)
    nodes = sum(len(pool.instances) for pool in reading.pools)
    print(f"read {len(reading.pools)} pool(s), {nodes} node(s), {len(faults)} fault(s)", flush=True)
    if faults:
        raise ReleaseError("the release did not reach every running node: " + "; ".join(faults))
    return nodes


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--worker-repository", required=True)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--aws-cli", default="aws")
    parser.add_argument("--arch", action="append", dest="architectures")
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="publish from an uncommitted tree, whose version label cannot name its contents",
    )
    parser.add_argument(
        "--skip-restart",
        action="store_true",
        help=(
            "publish without loading the release into this stack, which also skips the "
            "report: nodes measured against a control plane that has not adopted the "
            "release read as stale when nothing is wrong"
        ),
    )
    args = parser.parse_args(argv)

    try:
        version = source_revision(allow_dirty=args.allow_dirty)
        print(f"source revision: {version}", flush=True)

        build_images()
        print("built every source-bearing image", flush=True)

        agent_dir = build_agent(version, args.architectures or ["amd64"])
        print(f"built the agent executable: {agent_dir}", flush=True)

        worker_image = push_worker_image(args.worker_repository, version)
        print(f"published the worker image: {worker_image}", flush=True)

        environment = publish_release(
            version=version,
            agent_dir=agent_dir,
            worker_image=worker_image,
            bucket=args.bucket,
            region=args.region,
            aws_cli=args.aws_cli,
        )
        print("published and verified the release", flush=True)

        print(f"repointed: {', '.join(repoint_deployment(environment))}", flush=True)

        if args.skip_restart:
            print(
                f"release {version} is published; this stack was not restarted, so it "
                "still runs the previous release and no node was checked"
            )
            return 0

        restart_stack()
        print("restarted the stack and restored the sidecars", flush=True)

        # Both processes must agree before any claim about a node: while they
        # disagree the launch template alternates and every version a node
        # reports is meaningless.
        print("pool bootstrap inputs:", flush=True)
        guard_bootstrap_agreement(environment)

        nodes = report_release_reach(since=read_reach().clock)
    except ReleaseError as error:
        print(f"release failed: {error}", file=sys.stderr)
        return 1

    print(f"release {version} is live; {nodes} managed node(s) are running it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
