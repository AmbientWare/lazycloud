"""Render the connection role policy for consumers that are not CloudFormation.

`provider_aws.connection_policy` owns the permission set. The customer template
renders it directly at request time; the platform's own Terraform cannot, because
Terraform does not run Python. So the rendering is committed as a file, and
`--check` proves the committed file is still what the owner produces.

That check is the whole point. A committed artifact and its generator drift
silently, and this one drifts into a role that is missing an action the control
plane started calling — which surfaces as a launch denial naming an API call
rather than the policy behind it.

Without `--check`, writes the file. With it, exits non-zero when the file differs
and prints what changed.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path

from provider_aws.connection_policy import ConcreteArns, connection_role_policy

_RENDERED = Path(__file__).resolve().parent / "platform-deployment" / "connection-role-policy.json"

# Rendered against wildcards rather than one account's identity. The platform's
# own connection role is the only consumer, its node identity names are derived
# per connection, and pinning them here would make the artifact specific to a
# deployment that has not been created yet.
_SCOPE = ConcreteArns(
    partition="aws",
    region="*",
    account_id="*",
    node_role_name="compute-node-*",
    node_instance_profile_name="compute-node-*",
)


def _rendered() -> str:
    # No authorization stack: the platform's own account is connected by an
    # existing role, so there is no stack for the connection to describe or
    # delete and the statement would grant nothing.
    policy = connection_role_policy(_SCOPE, authorization_stack=None)
    return json.dumps(policy, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail if the file is stale.")
    args = parser.parse_args(argv)

    rendered = _rendered()
    if not args.check:
        _RENDERED.parent.mkdir(parents=True, exist_ok=True)
        _RENDERED.write_text(rendered)
        print(f"wrote {_RENDERED}")
        return 0

    current = _RENDERED.read_text() if _RENDERED.exists() else ""
    if current == rendered:
        return 0
    diff = difflib.unified_diff(
        current.splitlines(keepends=True),
        rendered.splitlines(keepends=True),
        fromfile=f"{_RENDERED.name} (committed)",
        tofile=f"{_RENDERED.name} (generated)",
    )
    sys.stdout.writelines(diff)
    print(
        f"\n{_RENDERED} is stale. Run: uv run --group workspace python "
        "deploy/render_connection_policy.py",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
