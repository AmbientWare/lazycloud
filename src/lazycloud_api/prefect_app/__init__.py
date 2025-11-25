from prefect import serve as flow_serve
from prefect.task_worker import serve as task_worker_serve

from lazycloud_api.prefect_app.deployment import (
    deploy_compose_task,
    destroy_compose_task,
    reconcile_rollback_states_deployment,
    rollback_compose_task,
)
from lazycloud_api.prefect_app.instances import delete_instance_task
from lazycloud_api.prefect_app.services import (
    restart_all_services_task,
    restart_service_task,
)
from lazycloud_api.prefect_app.subscription_monitor import (
    monitor_subscription_states_deployment,
)
from lazycloud_api.prefect_app.usage_collector import (
    forward_for_billing_deployment,
    mark_workspaces_for_backfill_deployment,
    process_incomplete_usage_deployment,
    spawn_usage_collection_deployment,
)
from lazycloud_api.prefect_app.utils import get_task_result
from lazycloud_api.prefect_app.workspace_cleanup import (
    cleanup_orphaned_deployments_deployment,
)


def serve_background_tasks():
    """Serve Prefect tasks."""
    task_worker_serve(
        deploy_compose_task,
        destroy_compose_task,
        rollback_compose_task,
        delete_instance_task,
        restart_service_task,
        restart_all_services_task,
    )


def serve_crons():
    """Serve Prefect cron tasks."""
    flow_serve(
        spawn_usage_collection_deployment,
        mark_workspaces_for_backfill_deployment,
        process_incomplete_usage_deployment,
        forward_for_billing_deployment,
        monitor_subscription_states_deployment,
        reconcile_rollback_states_deployment,
        cleanup_orphaned_deployments_deployment,
    )


__all__ = [
    "serve_background_tasks",
    "serve_crons",
    "get_task_result",
]
