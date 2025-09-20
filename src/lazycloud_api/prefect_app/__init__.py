from prefect.task_worker import serve

from lazycloud_api.prefect_app.compose import (
    delete_instance_task,
    deploy_compose_task,
    destroy_compose_task,
)
from lazycloud_api.prefect_app.utils import get_task_result


def serve_prefect_tasks():
    """Serve Prefect tasks."""
    serve(deploy_compose_task, destroy_compose_task, delete_instance_task)


__all__ = ["serve_prefect_tasks", "get_task_result"]
