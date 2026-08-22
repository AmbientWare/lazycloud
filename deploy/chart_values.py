"""Render the chart's values from what the infrastructure already knows.

Nothing here is authored. Image references come from what the deploy pushed,
runtime values and secret paths from the platform module's outputs, and the IRSA
roles from the same. The chart then decides nothing about a value the
infrastructure has already decided, which is what makes a variable impossible to
supply in one place and forget in another.

Images are pinned by digest, never by tag. A tag is a name that can be moved, and
two replicas that pulled it an hour apart have no way to notice they differ.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml
from pydantic import TypeAdapter, ValidationError

_STRING_MAP = TypeAdapter(dict[str, str])

# Chart key per image, matching `deploy/chart/values.yaml`.
IMAGE_VALUES = {
    "api": "api",
    "scheduler": "scheduler",
    "cache-server": "cacheServer",
    "worker-bootstrap": "workerBootstrap",
    "database-bootstrap": "databaseBootstrap",
    "cli": "cli",
}

# Runtime values whose absence produces a control plane that starts, reports
# healthy, and is wrong. Each of these has done exactly that: the deployment
# converged, the workflow reported success, and the defect surfaced steps later
# as a symptom naming something else. Checked here because this is the last point
# that holds the whole set at once -- the cluster receives them already
# assembled, and the API cannot tell a value it was never given from one a
# deployment legitimately does not have.
#
# Deliberately not everything. A value with a meaningful empty -- the object
# store endpoint, which is blank to select S3 itself -- cannot be told from an
# omission by this check and does not belong in it.
REQUIRED_RUNTIME_VARIABLES = {
    "LAZYCLOUD_AWS_CONNECTION_CONTROL_PRINCIPAL_ARN": (
        "the principal a customer's account authorizes; without it every "
        "connection request is refused and no managed capacity is ever registered"
    ),
    "LAZYCLOUD_GATEWAY_PUBLIC_HTTP_URL": (
        "the origin this deployment is reached on; it defaults to localhost, so "
        "absence is silent and reaches customers in authorization templates and "
        "OAuth redirects"
    ),
    "LAZYCLOUD_GITHUB_REDIRECT_URI": (
        "where GitHub returns a person after sign-in; without it the deployment "
        "refuses every sign-in as provider_unavailable and nobody can reach the "
        "dashboard"
    ),
    "LAZYCLOUD_OBJECT_STORE_BUCKET": "the bucket every artifact, package and log is written to",
    "LAZYCLOUD_OBJECT_STORE_REGION_NAME": (
        "the region the secret store and the object store are read from"
    ),
    "LAZYCLOUD_REDIS_URL": (
        "the coordination Redis; without it the scheduler holds no lease and the "
        "control plane publishes no origin for a worker to dial"
    ),
}


class ValuesError(RuntimeError):
    """The values could not be rendered."""


def _string_map(source: Path, label: str) -> dict[str, str]:
    try:
        return _STRING_MAP.validate_json(source.read_bytes())
    except ValidationError as error:
        raise ValuesError(f"{label} file must be a JSON object of strings: {error}") from error


def _image_reference(registry: str, deployment: str, image: str, digest: str) -> str:
    if not digest.startswith("sha256:"):
        raise ValuesError(f"{image} digest must be sha256-addressed, got {digest!r}")
    return f"{registry}/{deployment}/{image}@{digest}"


def _images(registry: str, deployment: str, digests: dict[str, str]) -> dict[str, str]:
    missing = sorted(set(IMAGE_VALUES) - set(digests))
    if missing:
        raise ValuesError(f"no digest published for: {', '.join(missing)}")
    return {
        key: _image_reference(registry, deployment, image, digests[image])
        for image, key in IMAGE_VALUES.items()
    }


def _checked_runtime(runtime: dict[str, str]) -> dict[str, str]:
    missing = sorted(
        variable for variable in REQUIRED_RUNTIME_VARIABLES if not runtime.get(variable, "").strip()
    )
    if missing:
        raise ValuesError(
            "the deployment is missing runtime values a control plane cannot work "
            "without:\n"
            + "\n".join(f"  {name}: {REQUIRED_RUNTIME_VARIABLES[name]}" for name in missing)
        )
    return runtime


def render(args: argparse.Namespace) -> None:
    runtime = _checked_runtime(_string_map(Path(args.runtime), "runtime"))
    if args.release_manifest_url:
        # Added only when there is one. Writing the variable empty is not the
        # same as leaving it out: the pods would then carry a blank URL, and a
        # release that cannot be fetched is a different failure from a deployment
        # that names no release.
        runtime["LAZYCLOUD_RELEASE_MANIFEST_URL"] = args.release_manifest_url

    roles = _string_map(Path(args.roles), "roles")
    images = _images(args.registry, args.deployment, _string_map(Path(args.digests), "digests"))
    values: dict[str, object] = {
        "images": images,
        "runtime": runtime,
        "secrets": {"map": _string_map(Path(args.secret_map), "secret map")},
        "roles": {
            "controlPlane": roles["control_plane"],
            "externalSecrets": roles["external_secrets"],
        },
        "cloudflared": {
            # The zone the tunnel answers for, taken from the origin rather than
            # named twice: an ingress rule written against a different host than
            # the one the deployment publishes routes nothing.
            "apex": runtime["LAZYCLOUD_GATEWAY_PUBLIC_HTTP_URL"].removeprefix("https://"),
            "tunnelId": args.tunnel_id,
        },
    }
    Path(args.output).write_text(yaml.safe_dump(values, sort_keys=True))
    print(json.dumps({"output": args.output, "images": sorted(images)}, indent=2))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", required=True, help="ECR registry host.")
    parser.add_argument("--deployment", required=True, help="Deployment prefix repositories carry.")
    parser.add_argument(
        "--digests", required=True, help="JSON file of image name to sha256 digest."
    )
    parser.add_argument(
        "--runtime",
        required=True,
        help="JSON file of non-secret runtime values. Required: a chart without them "
        "produces a control plane that starts and is wrong.",
    )
    parser.add_argument("--secret-map", required=True, help="JSON file of variable to secret name.")
    parser.add_argument("--roles", required=True, help="JSON file of workload IRSA role ARNs.")
    parser.add_argument("--tunnel-id", default="", help="Cloudflare tunnel the ingress runs.")
    parser.add_argument("--release-manifest-url", default="", help="Release this deployment runs.")
    parser.add_argument("--output", required=True, help="Where to write the rendered values.")
    return parser


def main() -> None:
    args = _parser().parse_args()
    try:
        render(args)
    except ValuesError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
