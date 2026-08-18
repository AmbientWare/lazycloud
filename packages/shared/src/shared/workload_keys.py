def pod_keep_warm_lock_key(workspace_id: str, stub_id: str, container_id: str) -> str:
    return f"pod:{workspace_id}:{stub_id}:keep_warm_lock:{container_id}"


def pod_container_connections_key(workspace_id: str, stub_id: str, container_id: str) -> str:
    return f"pod:{workspace_id}:{stub_id}:container_connections:{container_id}"


def pod_total_connections_key(workspace_id: str, stub_id: str) -> str:
    return f"pod:{workspace_id}:{stub_id}:total_connections"


def container_readiness_key(container_id: str, *, port: int, path: str = "") -> str:
    """Name one probe, not one container.

    A container serving several proxied ports is asked a different question on
    each, and a declared health path asks a different question again, so the
    verdicts cannot share a key without one answering for another.
    """

    probe = f"{port}" if not path else f"{port}:{path}"
    return f"container:readiness:{container_id}:{probe}"
