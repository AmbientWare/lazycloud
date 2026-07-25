def endpoint_keep_warm_lock_key(workspace_name: str, stub_id: str, container_id: str) -> str:
    return f"endpoint:{workspace_name}:{stub_id}:keep_warm_lock:{container_id}"


def pod_keep_warm_lock_key(workspace_name: str, stub_id: str, container_id: str) -> str:
    return f"pod:{workspace_name}:{stub_id}:keep_warm_lock:{container_id}"


def pod_container_connections_key(workspace_name: str, stub_id: str, container_id: str) -> str:
    return f"pod:{workspace_name}:{stub_id}:container_connections:{container_id}"


def pod_total_connections_key(workspace_name: str, stub_id: str) -> str:
    return f"pod:{workspace_name}:{stub_id}:total_connections"


def task_queue_running_lock_index_key(workspace_name: str, stub_id: str, container_id: str) -> str:
    return f"taskqueue:{workspace_name}:{stub_id}:task_running:{container_id}:index"


def task_queue_running_lock_key(
    workspace_name: str,
    stub_id: str,
    container_id: str,
    task_id: str,
) -> str:
    return f"taskqueue:{workspace_name}:{stub_id}:task_running:{container_id}:{task_id}"


def task_queue_processing_lock_key(workspace_name: str, stub_id: str, container_id: str) -> str:
    return f"taskqueue:{workspace_name}:{stub_id}:processing_lock:{container_id}"


def task_queue_keep_warm_lock_key(workspace_name: str, stub_id: str, container_id: str) -> str:
    return f"taskqueue:{workspace_name}:{stub_id}:keep_warm_lock:{container_id}"
