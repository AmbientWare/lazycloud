"""Publish local release artifacts and select independent worker and host pins.

Control-plane images and the worker image are built from this source revision.
The deployment keeps the explicitly selected host agent and AMI release.
Host template inventory is reported separately from worker rollout acceptance.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
_AGENT_BUILD = "deploy/agent-binary/build.py"
_RELEASE = "deploy/aws-release-assets/release.py"

# The two processes that each compose a pool's launch-template bootstrap.
_BOOTSTRAP_PROCESSES = ("control-plane", "scheduler")
# Authored by the deployment rather than published by a release, and the one
# bootstrap input both processes read that a release therefore cannot align.
_GATEWAY_ORIGIN_VARIABLE = "LAZYCLOUD_GATEWAY_PUBLIC_HTTP_URL"
_MANIFEST_URL_VARIABLE = "LAZYCLOUD_RELEASE_MANIFEST_URL"
_WORKER_MANIFEST_URL_VARIABLE = "LAZYCLOUD_RELEASE_WORKER_MANIFEST_URL"
_HOST_MANIFEST_URL_VARIABLE = "LAZYCLOUD_RELEASE_HOST_MANIFEST_URL"
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


def _parse_region_amis(entries: Sequence[str], *, flag: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for entry in entries:
        region, separator, ami_id = entry.partition("=")
        if not separator or not region.strip() or not ami_id.strip():
            raise ReleaseError(f"{flag} takes REGION=AMI, not {entry!r}")
        parsed[region.strip()] = ami_id.strip()
    return parsed


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
    cpu_ami_ids: dict[str, str],
    gpu_ami_ids: dict[str, str],
) -> str:
    """Stage, validate, publish, and verify; return the URL the manifest is served at."""
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
            "--cpu-ami-ids",
            json.dumps(cpu_ami_ids, sort_keys=True, separators=(",", ":")),
            "--gpu-ami-ids",
            json.dumps(gpu_ami_ids, sort_keys=True, separators=(",", ":")),
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
    published_url = str(manifest["manifest_public_url"])
    if not published_url:
        raise ReleaseError(f"release {version} does not say where its manifest is served")
    return published_url


def _https_manifest_url(value: str) -> str:
    url = value.strip()
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise ReleaseError("host manifest must be a valid HTTPS URL") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or any(character.isspace() for character in url)
    ):
        raise ReleaseError("host manifest must be an HTTPS URL without credentials or a fragment")
    return url


def repoint_deployment(manifest_url: str, *, host_manifest_url: str) -> None:
    """Update release pins in the local .env without changing other settings."""
    path = _REPOSITORY_ROOT / ".env"
    lines = path.read_text().splitlines(keepends=True) if path.exists() else []
    pins = {
        _MANIFEST_URL_VARIABLE: manifest_url,
        _WORKER_MANIFEST_URL_VARIABLE: manifest_url,
        _HOST_MANIFEST_URL_VARIABLE: host_manifest_url,
    }
    for name, value in pins.items():
        entry = f"{name}={json.dumps(value)}\n"
        matches = [
            index for index, line in enumerate(lines) if line.split("=", 1)[0].strip() == name
        ]
        if matches:
            for index in matches:
                lines[index] = entry
        else:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.append(entry)
    path.write_text("".join(lines))


def restart_stack() -> None:
    _run(["docker", "compose", "up", "-d"])


def process_environment(service: str) -> dict[str, str]:
    """The environment a running service holds, which is the one it was created with."""
    entries = _in_service(service, ["env", "-0"]).split("\0")
    return dict(entry.split("=", 1) for entry in entries if "=" in entry)


def guard_bootstrap_agreement(manifest_url: str, *, host_manifest_url: str) -> None:
    """Verify both bootstrap composers loaded the selected release pins."""
    expected: dict[str, str | None] = {
        _MANIFEST_URL_VARIABLE: manifest_url,
        _WORKER_MANIFEST_URL_VARIABLE: manifest_url,
        _HOST_MANIFEST_URL_VARIABLE: host_manifest_url,
        # No release publishes this, so it is checked for agreement only.
        _GATEWAY_ORIGIN_VARIABLE: None,
    }
    observed = {service: process_environment(service) for service in _BOOTSTRAP_PROCESSES}
    divergent: list[str] = []
    unloaded: list[str] = []
    for name, published in expected.items():
        held = [observed[service].get(name, "") for service in _BOOTSTRAP_PROCESSES]
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
            unloaded.append(f"{name}: both hold {value}, selected pin is {published}")
            print(f"  {name}: both hold {value}, NOT the selected pin", flush=True)
            continue
        origin = "selected pin" if published is not None else "the deployment"
        print(f"  {name}: both hold {value} ({origin})", flush=True)
    if divergent:
        raise ReleaseError(
            "the API and the scheduler compose different pool bootstraps, so the launch "
            "template alternates between versions on every reconcile and no node settles "
            "on either: " + "; ".join(divergent)
        )
    if unloaded:
        raise ReleaseError(
            "processes have not loaded the selected release pins: " + "; ".join(unloaded)
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
      from compute_units p
      where p.phase <> 'deleted'
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

    A pool holding no instances settles immediately. Its own reading is only
    ever used to supply the version its nodes are compared against, and a pool
    at zero capacity is not reconciled at all -- waiting for a rewrite that the
    control plane has no reason to perform would spend the whole budget and then
    report a pool with nothing to check as unreachable.
    """
    return all(
        (not pool.instances)
        or (
            pool.observed_at >= since
            and all(instance.observed_at >= since for instance in pool.instances)
        )
        for pool in reading.pools
    )


def _reach_faults(reading: ReachReading, *, since: datetime) -> list[str]:
    faults: list[str] = []
    for pool in reading.pools:
        if not pool.instances:
            # Nothing booted from this pool, so there is nothing this release
            # could have failed to reach.
            continue
        if pool.observed_at < since:
            faults.append(f"{pool.name} has not been reconciled since the restart")
        elif not pool.expected_template_version:
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
    """Report host launch-template inventory; this does not inspect worker images."""
    deadline = time.monotonic() + _REACH_BUDGET_SECONDS
    cycle = 0
    while True:
        cycle += 1
        reading = read_reach()
        print(f"host template inventory, cycle {cycle}:", flush=True)
        if not reading.pools:
            print("  no managed host instances recorded; worker rollout is not evaluated")
            return 0
        _print_reach(reading, since=since)
        if _reach_settled(reading, since=since) or time.monotonic() >= deadline:
            break
        time.sleep(_REACH_POLL_SECONDS)
    faults = _reach_faults(reading, since=since)
    nodes = sum(len(pool.instances) for pool in reading.pools)
    print(f"read {len(reading.pools)} pool(s), {nodes} node(s), {len(faults)} fault(s)", flush=True)
    if faults:
        raise ReleaseError(
            "host template inventory has unresolved differences: " + "; ".join(faults)
        )
    return nodes


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--worker-repository", required=True)
    parser.add_argument(
        "--host-manifest-url",
        required=True,
        help="HTTPS release manifest selecting the host agent and AMIs",
    )
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--aws-cli", default="aws")
    parser.add_argument("--arch", action="append", dest="architectures")
    parser.add_argument(
        "--gpu-ami",
        action="append",
        dest="gpu_amis",
        metavar="REGION=AMI",
        help="baked GPU node AMI for a region; omit for a deployment that runs no GPUs",
    )
    parser.add_argument(
        "--cpu-ami",
        action="append",
        dest="cpu_amis",
        metavar="REGION=AMI",
        help="CPU host AMI advertised by this manifest; this stack uses --host-manifest-url",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="publish from an uncommitted tree, whose version label cannot name its contents",
    )
    parser.add_argument(
        "--skip-restart",
        action="store_true",
        help="publish and write pins without restarting or checking the running stack",
    )
    args = parser.parse_args(argv)

    try:
        host_manifest_url = _https_manifest_url(args.host_manifest_url)
        cpu_ami_ids = _parse_region_amis(args.cpu_amis or [], flag="--cpu-ami")
        gpu_ami_ids = _parse_region_amis(args.gpu_amis or [], flag="--gpu-ami")
        version = source_revision(allow_dirty=args.allow_dirty)
        print(f"source revision: {version}", flush=True)

        build_images()
        print("built every source-bearing image", flush=True)

        agent_dir = build_agent(version, args.architectures or ["amd64"])
        print(f"built the agent executable: {agent_dir}", flush=True)

        worker_image = push_worker_image(args.worker_repository, version)
        print(f"published the worker image: {worker_image}", flush=True)

        manifest_url = publish_release(
            version=version,
            agent_dir=agent_dir,
            worker_image=worker_image,
            bucket=args.bucket,
            region=args.region,
            aws_cli=args.aws_cli,
            cpu_ami_ids=cpu_ami_ids,
            gpu_ami_ids=gpu_ami_ids,
        )
        print("published and verified the release", flush=True)

        repoint_deployment(manifest_url, host_manifest_url=host_manifest_url)
        print(
            f"selected worker release {manifest_url}; host release {host_manifest_url}",
            flush=True,
        )

        if args.skip_restart:
            print(
                f"release {version} is published; this stack was not restarted, so it "
                "still runs the previous release and no node was checked"
            )
            return 0

        restart_stack()
        print("restarted the stack", flush=True)

        # Both processes must agree before any claim about a node: while they
        # disagree the launch template alternates and every version a node
        # reports is meaningless.
        print("pool bootstrap inputs:", flush=True)
        guard_bootstrap_agreement(manifest_url, host_manifest_url=host_manifest_url)

        nodes = report_release_reach(since=read_reach().clock)
    except ReleaseError as error:
        print(f"release failed: {error}", file=sys.stderr)
        return 1

    print(
        f"release {version} published; stack pins loaded and {nodes} host(s) inventoried. "
        "Worker rollout has not been verified."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
