import sentry_sdk
from loguru import logger
from prefect import serve as flow_serve
from sentry_sdk.integrations.loguru import LoguruIntegration

from backend.config import app_config
from backend.prefect_app.deployment import (
    cleanup_stale_pending_deployment,
    reconcile_rollback_states_deployment,
)
from backend.prefect_app.registry import Deployments
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
    """Serve Prefect flow deployments to the background work pool.

    These flows are triggered by the API via run_flow() and executed
    by workers polling the work pool (HTTP-based, more reliable than
    WebSocket-based task workers).
    """
    # Get all registered deployments and convert to Prefect deployment objects
    deployments = [d.to_deployment() for d in Deployments.all()]

    logger.info(
        f"Starting background flow server with {len(deployments)} deployments: "
        f"{[d.name for d in deployments]}"
    )

    flow_serve(*deployments)


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
