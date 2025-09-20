from prefect.task_worker import serve

from lazycloud_api.prefect_app.compose import (
    deploy_compose_task,
    destroy_compose_task,
)
from lazycloud_api.prefect_app.instances import delete_instance_task
from lazycloud_api.prefect_app.services import (
    restart_all_services_task,
    restart_service_task,
)
from lazycloud_api.prefect_app.utils import get_task_result


def serve_prefect_tasks():
    """Serve Prefect tasks."""
    serve(
        deploy_compose_task,
        destroy_compose_task,
        delete_instance_task,
        restart_service_task,
        restart_all_services_task,
    )


__all__ = ["serve_prefect_tasks", "get_task_result"]
