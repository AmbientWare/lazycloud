"""Publish the deployment bundle a control-plane host converges onto.

The bundle is what `lazycloud-deploy` reads on the host: `compose.yaml`, the
production overlay, the image digests, the non-secret runtime values, and the
list of services a deployment runs, and the script that applies all of it. It
carries no credentials — those are read
from Secrets Manager on the host, so that a bundle sitting in a versioned bucket
is not a credential that outlives every rotation.

Images are pinned by digest, never by tag. A tag is a name that can be moved, and
a host that pulled `:v1.4.0` an hour after another host pulled it has no way to
notice they differ. The digest is also what the managed runtime compares: a
release assembled from two source states is rejected at container start as a
package-digest mismatch, and that error names a digest rather than the stale
artifact behind it.

Run it after `deploy/release.py`, which is the thing that guarantees one source
state. This command refuses to invent that guarantee for itself: it takes the
digests it is given and fails if any image is missing one.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
_COMPOSE = _REPOSITORY_ROOT / "compose.yaml"
_OVERLAY = _REPOSITORY_ROOT / "deploy" / "compose.deploy.yaml"
_COLLECTOR = _REPOSITORY_ROOT / "deploy" / "telemetry" / "collector.deploy.yaml"
_CONVERGE = _REPOSITORY_ROOT / "deploy" / "host" / "converge.sh"
_STRING_MAP = TypeAdapter(dict[str, str])

# The services a deployment runs. Everything absent is served by something else:
# PlanetScale for the database, S3 for objects, the connected-AWS pool for
# capacity, and a real backend for telemetry. `deploy/compose.deploy.yaml`
# records which is which.
DEPLOYMENT_SERVICES = (
    "redis",
    "database-bootstrap",
    "administrator-bootstrap",
    "tcp-certificate",
    "control-plane",
    "scheduler",
    "cache-token",
    "cache-server",
    "public-ingress",
    "otel-collector",
)

# Compose variable per image, matching `deploy/compose.deploy.yaml`.
IMAGE_VARIABLES = {
    "api": "LAZYCLOUD_IMAGE_API",
    "scheduler": "LAZYCLOUD_IMAGE_SCHEDULER",
    "cache-server": "LAZYCLOUD_IMAGE_CACHE_SERVER",
    "worker-bootstrap": "LAZYCLOUD_IMAGE_WORKER_BOOTSTRAP",
    "database-bootstrap": "LAZYCLOUD_IMAGE_DATABASE_BOOTSTRAP",
}


class BundleError(RuntimeError):
    """The bundle could not be assembled or published."""


def _image_reference(registry: str, deployment: str, image: str, digest: str) -> str:
    if not digest.startswith("sha256:"):
        raise BundleError(f"{image} digest must be sha256-addressed, got {digest!r}")
    return f"{registry}/{deployment}/{image}@{digest}"


def _images_env(registry: str, deployment: str, digests: dict[str, str]) -> str:
    missing = sorted(set(IMAGE_VARIABLES) - set(digests))
    if missing:
        raise BundleError(f"no digest published for: {', '.join(missing)}")
    lines = [
        f"{variable}={_image_reference(registry, deployment, image, digests[image])}"
        for image, variable in sorted(IMAGE_VARIABLES.items(), key=lambda item: item[1])
    ]
    return "\n".join(lines) + "\n"


def _runtime_env(values: dict[str, str]) -> str:
    return "".join(f"{key}={value}\n" for key, value in sorted(values.items()))


def _upload(source: Path, destination: str, *, aws_cli: str) -> None:
    result = subprocess.run(
        [aws_cli, "s3", "cp", str(source), destination],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise BundleError(f"upload to {destination} failed: {result.stderr.strip()}")


def _string_map(source: Path, label: str) -> dict[str, str]:
    try:
        return _STRING_MAP.validate_json(source.read_bytes())
    except ValidationError as error:
        raise BundleError(f"{label} file must be a JSON object of strings: {error}") from error


def publish(args: argparse.Namespace) -> None:
    digests = _string_map(Path(args.digests), "digests")
    runtime = _string_map(Path(args.runtime), "runtime") if args.runtime else {}

    staged = Path(args.stage_dir)
    staged.mkdir(parents=True, exist_ok=True)
    (staged / "images.env").write_text(_images_env(args.registry, args.deployment, digests))
    (staged / "runtime.env").write_text(_runtime_env(runtime))
    (staged / "services").write_text(" ".join(DEPLOYMENT_SERVICES) + "\n")

    prefix = f"s3://{args.bucket}/current"
    for source in (_COMPOSE, _OVERLAY, _COLLECTOR, _CONVERGE):
        _upload(source, f"{prefix}/{source.name}", aws_cli=args.aws_cli)
    for name in ("images.env", "runtime.env", "services"):
        _upload(staged / name, f"{prefix}/{name}", aws_cli=args.aws_cli)

    print(json.dumps({"bucket": args.bucket, "services": list(DEPLOYMENT_SERVICES)}, indent=2))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True, help="Deploy bucket from the Terraform output.")
    parser.add_argument("--registry", required=True, help="ECR registry host.")
    parser.add_argument("--deployment", required=True, help="Deployment prefix repositories carry.")
    parser.add_argument(
        "--digests", required=True, help="JSON file of image name to sha256 digest."
    )
    parser.add_argument("--runtime", default="", help="JSON file of non-secret runtime values.")
    parser.add_argument("--stage-dir", required=True, help="Directory to assemble the bundle in.")
    parser.add_argument("--aws-cli", default="aws")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        publish(args)
    except BundleError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
