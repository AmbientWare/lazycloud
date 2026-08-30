"""Render the chart's values from what the infrastructure already knows.

Nothing here is authored. Image references come from what the deploy pushed,
and runtime values and secret paths from the platform module's outputs. The chart
then decides nothing about a value the infrastructure has already decided, which
is what makes a variable impossible to supply in one place and forget in another.

No role ARNs: which AWS identity a service account holds is a Pod Identity
association declared beside the cluster, so it never travels through here.

One image tag, naming the commit that produced the images, and it is safe to pin
by tag for a reason worth stating: every repository is created
`image_tag_mutability = "IMMUTABLE"`, so a tag cannot be repointed once pushed.
The rule against tags is about names that can move; this one cannot, and it says
which commit is running in a way a digest does not.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from pydantic import TypeAdapter, ValidationError

_STRING_MAP = TypeAdapter(dict[str, str])

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
    "LAZYCLOUD_STRIPE_ACCOUNT_ID": (
        "the payment-provider account the catalog is published into; the "
        "publisher checks the credential against it, and without it no plan or "
        "price exists for a sign-in to put anyone on"
    ),
    "LAZYCLOUD_WORKSPACE_STORAGE_ISSUER": (
        "which object store cuts a workspace's storage credential; without it "
        "the control plane refuses to start rather than guessing, because the "
        "two stores are reached in different ways"
    ),
    "LAZYCLOUD_WORKSPACE_STORAGE_ROLE_ARN": (
        "the role a workspace's bucket-scoped credential is cut from; without it "
        "no container can mount its workspace storage"
    ),
    "LAZYCLOUD_REDIS_URL": (
        "the coordination Redis; without it the scheduler holds no lease and the "
        "control plane publishes no origin for a worker to dial"
    ),
    "LAZYCLOUD_PANGOLIN_API_URL": "the Pangolin Integration API the provider reconciles",
    "LAZYCLOUD_PANGOLIN_ENDPOINT": "the Pangolin endpoint Newt and machine clients connect to",
    "LAZYCLOUD_PANGOLIN_ORGANIZATION_ID": "the Pangolin organization LazyCloud owns",
}


class ValuesError(RuntimeError):
    """The values could not be rendered."""


def _string_map(source: Path, label: str) -> dict[str, str]:
    try:
        return _STRING_MAP.validate_json(source.read_bytes())
    except ValidationError as error:
        raise ValuesError(f"{label} file must be a JSON object of strings: {error}") from error


def _json_value(source: Path, label: str) -> dict[str, object]:
    try:
        return json.loads(source.read_bytes())
    except (OSError, ValueError) as error:
        raise ValuesError(f"{label} file could not be read: {error}") from error


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


def _credential_count(values: dict[str, str], prefix: str, label: str) -> int:
    pattern = re.compile(rf"^{re.escape(prefix)}_(\d+)_(ID|SECRET)$")
    fields_by_ordinal: dict[int, set[str]] = {}
    for name in values:
        match = pattern.fullmatch(name)
        if match is None:
            raise ValuesError(f"unrecognized {label} credential key: {name}")
        fields_by_ordinal.setdefault(int(match.group(1)), set()).add(match.group(2))

    ordinals = sorted(fields_by_ordinal)
    if ordinals != list(range(len(ordinals))):
        raise ValuesError(f"{label} credential ordinals must be contiguous from zero")
    incomplete = [
        ordinal for ordinal, fields in fields_by_ordinal.items() if fields != {"ID", "SECRET"}
    ]
    if incomplete:
        raise ValuesError(f"{label} credentials require ID and SECRET for ordinals {incomplete}")
    if len(ordinals) < 2:
        raise ValuesError(f"{label} credentials require at least two replicas")
    return len(ordinals)


def _pangolin_runtime_maps(
    values: dict[str, str],
) -> tuple[dict[str, dict[str, str]], int, int]:
    provider_names = {
        "LAZYCLOUD_PANGOLIN_PLATFORM_SITE_IDS",
        "LAZYCLOUD_PANGOLIN_PLATFORM_CLIENT_RECORD_IDS",
    }
    groups: dict[str, dict[str, str]] = {
        "provider": {},
        "sites": {},
        "clients": {},
    }
    for name, secret in values.items():
        if name in provider_names:
            groups["provider"][name] = secret
        elif name.startswith("LAZYCLOUD_PANGOLIN_PLATFORM_CONNECTOR_"):
            groups["sites"][name] = secret
        elif name.startswith("LAZYCLOUD_PANGOLIN_PLATFORM_CLIENT_"):
            groups["clients"][name] = secret
        else:
            raise ValuesError(f"unrecognized Pangolin runtime secret key: {name}")
    missing = sorted(provider_names - groups["provider"].keys())
    if missing:
        raise ValuesError(
            "Pangolin runtime secrets require aggregate provider IDs: " + ", ".join(missing)
        )
    site_count = _credential_count(
        groups["sites"],
        "LAZYCLOUD_PANGOLIN_PLATFORM_CONNECTOR",
        "Pangolin site",
    )
    client_count = _credential_count(
        groups["clients"],
        "LAZYCLOUD_PANGOLIN_PLATFORM_CLIENT",
        "Pangolin client",
    )
    return groups, site_count, client_count


def render(args: argparse.Namespace) -> None:
    runtime = _checked_runtime(_string_map(Path(args.runtime), "runtime"))
    if args.release_manifest_url:
        # Added only when there is one. Writing the variable empty is not the
        # same as leaving it out: the pods would then carry a blank URL, and a
        # release that cannot be fetched is a different failure from a deployment
        # that names no release.
        runtime["LAZYCLOUD_RELEASE_MANIFEST_URL"] = args.release_manifest_url

    public_hostname = urlsplit(runtime["LAZYCLOUD_GATEWAY_PUBLIC_HTTP_URL"]).hostname
    if public_hostname is None:
        raise ValuesError("LAZYCLOUD_GATEWAY_PUBLIC_HTTP_URL has no hostname")

    pangolin_runtime, site_count, client_count = _pangolin_runtime_maps(
        _string_map(Path(args.scoped_secrets), "Pangolin runtime secrets")
    )
    values: dict[str, object] = {
        "image": {
            "registry": args.registry,
            "repositoryPrefix": args.deployment,
            "tag": args.tag,
        },
        "runtime": runtime,
        "fleet": _json_value(Path(args.fleet), "fleet connection"),
        "secrets": {
            "map": _string_map(Path(args.secret_map), "secret map"),
            "pangolinControl": {
                "map": _string_map(
                    Path(args.pangolin_control_secrets),
                    "Pangolin control secrets",
                ),
            },
            "pangolinRuntime": {
                "secretId": args.pangolin_runtime_secret,
                **{name: {"map": values} for name, values in pangolin_runtime.items()},
            },
        },
        "pangolin": {
            "publicHostname": public_hostname,
            "site": {"replicas": site_count},
        },
        "controlPlane": {"replicas": client_count},
    }
    Path(args.output).write_text(yaml.safe_dump(values, sort_keys=True))
    print(json.dumps({"output": args.output, "tag": args.tag}, indent=2))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", required=True, help="ECR registry host.")
    parser.add_argument("--deployment", required=True, help="Deployment prefix repositories carry.")
    parser.add_argument(
        "--tag",
        required=True,
        help="Tag every image carries, which is the commit that produced them.",
    )
    parser.add_argument(
        "--runtime",
        required=True,
        help="JSON file of non-secret runtime values. Required: a chart without them "
        "produces a control plane that starts and is wrong.",
    )
    parser.add_argument("--secret-map", required=True, help="JSON file of variable to secret name.")
    parser.add_argument(
        "--pangolin-control-secrets",
        required=True,
        help="JSON file of operator Pangolin variable to secret name.",
    )
    parser.add_argument(
        "--pangolin-runtime-secret",
        required=True,
        help="Secrets Manager entry the Pangolin bootstrap owns.",
    )
    parser.add_argument(
        "--fleet",
        required=True,
        help="JSON file of the platform's own account, network and connection role.",
    )
    parser.add_argument(
        "--scoped-secrets",
        required=True,
        help="JSON file of variable to secret name, for entries exposed only to named containers.",
    )
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
