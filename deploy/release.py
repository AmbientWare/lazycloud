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
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
_AGENT_BUILD = "deploy/agent-binary/build.py"
_RELEASE = "deploy/aws-release-assets/release.py"


class ReleaseError(RuntimeError):
    """A release step failed, or its preconditions were not met."""


def _run(
    argv: Sequence[str],
    *,
    capture: bool = False,
    environment: dict[str, str] | None = None,
) -> str:
    result = subprocess.run(
        list(argv),
        cwd=_REPOSITORY_ROOT,
        capture_output=capture,
        text=True,
        check=False,
        env={**os.environ, **(environment or {})},
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-600:] if capture else ""
        raise ReleaseError(f"{' '.join(argv[:3])} failed: {detail}")
    return (result.stdout or "").strip()


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
    """Publish the worker image and return the digest the node will pull."""
    tag = f"{repository}:{version}"
    _run(["docker", "tag", "container-worker:local", tag])
    _run(["docker", "push", tag])
    digest = _run(
        ["docker", "inspect", tag, "--format", "{{index .RepoDigests 0}}"],
        capture=True,
    )
    if "@sha256:" not in digest:
        raise ReleaseError(f"worker image {tag} has no published digest to pin")
    return digest


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
    parser.add_argument("--skip-restart", action="store_true")
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

        if not args.skip_restart:
            restart_stack()
            print("restarted the stack and restored the sidecars", flush=True)
    except ReleaseError as error:
        print(f"release failed: {error}", file=sys.stderr)
        return 1

    print(f"release {version} is live; every artifact came from that revision")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
