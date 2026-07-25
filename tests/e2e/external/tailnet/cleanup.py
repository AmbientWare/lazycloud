"""Delete one exact uniquely named Tailnet E2E app through its public owner."""

from __future__ import annotations

import argparse
import json

from lazycloud.clients.resource.control import ResourceControlClient
from shared.http.errors import HttpApiError
from tests.e2e.external import _support

OWNED_PREFIX = "e2e_tailnet_"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--expected-app-name", required=True)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not args.live:
        return _support.skip("Tailnet cleanup", "--live is required")
    if not args.expected_app_name.startswith(OWNED_PREFIX):
        raise RuntimeError(f"refusing to delete an app outside {OWNED_PREFIX!r} ownership")
    try:
        endpoint, token, workspace = _support.prepared_gateway()
    except _support.MissingPrerequisite as exc:
        return _support.skip("Tailnet cleanup", exc)
    client = ResourceControlClient.from_endpoint(
        endpoint,
        token=token,
        workspace=workspace,
    )
    try:
        app = client.app(args.app_id)
    except HttpApiError as exc:
        if exc.status_code != 404:
            raise
        app = None
    if app is not None:
        if app.name != args.expected_app_name:
            raise RuntimeError("app ID/name ownership guard did not match")
        client.delete_app(app.id)
    if any(item.id == args.app_id for item in client.list_apps().data):
        raise RuntimeError("owned Tailnet app remains after public deletion")
    print(
        json.dumps(
            {
                "accepted": True,
                "deleted_app_id": args.app_id,
                "deleted_app_name": args.expected_app_name,
                "already_absent": app is None,
                "prepared_tailnet_deployment_retained": True,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
