def pod_keep_warm_lock_key(workspace_id: str, stub_id: str, container_id: str) -> str:
    return f"pod:{workspace_id}:{stub_id}:keep_warm_lock:{container_id}"


def pod_container_connections_key(workspace_id: str, stub_id: str, container_id: str) -> str:
    return f"pod:{workspace_id}:{stub_id}:container_connections:{container_id}"


def pod_total_connections_key(workspace_id: str, stub_id: str) -> str:
    return f"pod:{workspace_id}:{stub_id}:total_connections"
