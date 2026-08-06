from __future__ import annotations

DEFAULT_ENDPOINT_SERVE_TIMEOUT_SECONDS = 600


def endpoint_instance_lock_key(workspace_id: str, stub_id: str) -> str:
    return f"endpoint:{workspace_id}:{stub_id}:instance_lock"


def endpoint_serve_lock_key(workspace_id: str, stub_id: str) -> str:
    return f"scheduler:serve:lock:{workspace_id}:{stub_id}"
