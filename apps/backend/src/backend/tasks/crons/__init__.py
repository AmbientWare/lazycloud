"""SAQ cron job definitions for scheduled tasks.

These jobs run on a schedule and are executed by SAQ workers.
"""

from models.billing import UsageCollectionConfig
from saq import CronJob

from backend.config import app_config
from backend.tasks.crons.deployments import (
    cleanup_orphaned_deployments_job,
    cleanup_stale_pending_job,
    cleanup_stranded_depot_projects_job,
    reconcile_rollback_states_job,
)
from backend.tasks.crons.subscriptions import monitor_subscription_states_job
from backend.tasks.crons.usage import (
    alert_stuck_records_job,
    catch_up_missing_intervals_job,
    finalize_and_bill_job,
    spawn_usage_collection_job,
)

# All cron functions for worker registration
CRON_FUNCTIONS = [
    spawn_usage_collection_job,
    finalize_and_bill_job,
    catch_up_missing_intervals_job,
    alert_stuck_records_job,
    monitor_subscription_states_job,
    reconcile_rollback_states_job,
    cleanup_orphaned_deployments_job,
    cleanup_stale_pending_job,
    cleanup_stranded_depot_projects_job,
]

# CronJob definitions with schedules
CRON_JOBS = [
    CronJob(
        function=spawn_usage_collection_job.__call__,
        cron=UsageCollectionConfig.get_cron_expression(),  # Every 15 mins
        timeout=600,  # 10 minute timeout
        retries=0,
    ),
    CronJob(
        function=finalize_and_bill_job.__call__,
        cron="15 0 * * *",  # 00:15 UTC daily
        timeout=1800,  # 30 minute timeout
        retries=0,
    ),
    CronJob(
        function=catch_up_missing_intervals_job.__call__,
        cron="0 */2 * * *",  # Every 2 hours
        timeout=1800,
        retries=0,
    ),
    CronJob(
        function=alert_stuck_records_job.__call__,
        cron="0 1 * * *",  # 01:00 UTC daily
        timeout=300,
        retries=0,
    ),
    CronJob(
        function=monitor_subscription_states_job.__call__,
        cron="0 */4 * * *",  # Every 4 hours
        timeout=600,
        retries=0,
    ),
    CronJob(
        function=reconcile_rollback_states_job.__call__,
        cron=f"*/{app_config.ROLLBACK_RECONCILIATION_INTERVAL_MINUTES} * * * *",  # Every 10 mins
        timeout=300,
        retries=0,
    ),
    CronJob(
        function=cleanup_orphaned_deployments_job.__call__,
        cron="* * * * *",  # Every minute
        timeout=120,
        retries=0,
    ),
    CronJob(
        function=cleanup_stale_pending_job.__call__,
        cron="0 3 * * *",  # Daily 03:00 UTC
        timeout=300,
        retries=0,
    ),
    CronJob(
        function=cleanup_stranded_depot_projects_job.__call__,
        cron="0 4 * * *",  # Daily 04:00 UTC
        timeout=300,
        retries=0,
    ),
]

__all__ = [
    "CRON_FUNCTIONS",
    "CRON_JOBS",
    "spawn_usage_collection_job",
    "finalize_and_bill_job",
    "catch_up_missing_intervals_job",
    "alert_stuck_records_job",
    "monitor_subscription_states_job",
    "reconcile_rollback_states_job",
    "cleanup_orphaned_deployments_job",
    "cleanup_stale_pending_job",
    "cleanup_stranded_depot_projects_job",
]
