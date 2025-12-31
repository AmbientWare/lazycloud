import sentry_sdk
from loguru import logger
from prefect import serve as flow_serve
from prefect.task_worker import serve as task_worker_serve
from sentry_sdk.integrations.loguru import LoguruIntegration

from backend.config import app_config
from backend.prefect_app.deployment import (
    cleanup_stale_pending_deployment,
    deploy_compose_task,
    destroy_compose_task,
    reconcile_rollback_states_deployment,
    rollback_compose_task,
)
from backend.prefect_app.instances import delete_instance_task
from backend.prefect_app.services import (
    restart_all_services_task,
    restart_service_task,
)
from backend.prefect_app.subscription_monitor import (
    monitor_subscription_states_deployment,
)
from backend.prefect_app.usage_collector import (
    alert_stuck_records_deployment,
    catch_up_missing_intervals_deployment,
    finalize_and_bill_deployment,
    spawn_usage_collection_deployment,
)
from backend.prefect_app.utils import get_task_result
from backend.prefect_app.workspace_cleanup import (
    cleanup_orphaned_deployments_deployment,
)

# Initialize Sentry for background worker error tracking
if app_config.SENTRY_DSN:
    sentry_sdk.init(
        dsn=app_config.SENTRY_DSN,
        environment=app_config.ENV.value,
        integrations=[LoguruIntegration()],
    )
    logger.info("Sentry initialized for Prefect workers")


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
        finalize_and_bill_deployment,
        catch_up_missing_intervals_deployment,
        alert_stuck_records_deployment,
        monitor_subscription_states_deployment,
        reconcile_rollback_states_deployment,
        cleanup_orphaned_deployments_deployment,
        cleanup_stale_pending_deployment,
    )


__all__ = [
    "serve_background_tasks",
    "serve_crons",
    "get_task_result",
]
