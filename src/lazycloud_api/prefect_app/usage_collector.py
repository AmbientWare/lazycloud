import uuid
from datetime import datetime, timedelta, timezone

from loguru import logger
from prefect import flow, task

from lazycloud_api.database import db
from lazycloud_api.database.usage import UsageRecordType
from lazycloud_api.services import metrics_service
from lazycloud_api.services.k8s import create_ns_name


@task(retries=2, retry_delay_seconds=60)
async def collect_workspace_usage_for_hour(
    workspace_id: str, start_time: datetime, end_time: datetime
) -> dict:
    try:
        namespace = create_ns_name(workspace_id)

        # Get detailed breakdown from Prometheus
        breakdown = await metrics_service.get_namespace_breakdown(
            namespace, start_time, end_time
        )

        # Upsert usage record (update if exists, insert if not)
        usage_record = await db.usage.upsert_usage_record(
            workspace_id=uuid.UUID(workspace_id),
            collection_start=start_time,
            collection_end=end_time,
            cpu_core_seconds=breakdown.totals.cpu_core_seconds,
            memory_gb_seconds=breakdown.totals.memory_gb_seconds,
            storage_gb_hours=breakdown.totals.storage_gb_hours,
            record_type=UsageRecordType.HOURLY,
        )

        # Upsert service breakdowns
        for service_name, service_usage in breakdown.by_service.items():
            await db.usage.upsert_usage_breakdown(
                usage_record_id=uuid.UUID(usage_record.id),
                service_name=service_name,
                cpu_core_seconds=service_usage.cpu_core_seconds,
                memory_gb_seconds=service_usage.memory_gb_seconds,
                pod_count=service_usage.pod_count,
            )

        logger.debug(
            f"Collected {workspace_id} [{start_time.hour:02d}:00]: "
            f"CPU={breakdown.totals.cpu_core_seconds:.2f}s, "
            f"Mem={breakdown.totals.memory_gb_seconds:.2f}GB-s"
        )

        return {
            "workspace_id": workspace_id,
            "hour": start_time.hour,
            "success": True,
            "usage_record_id": usage_record.id,
        }

    except Exception as e:
        logger.error(
            f"Error collecting usage for {workspace_id} [{start_time.hour:02d}:00]: {e}"
        )
        return {
            "workspace_id": workspace_id,
            "hour": start_time.hour,
            "success": False,
            "error": str(e),
        }


@task(retries=2, retry_delay_seconds=60)
async def collect_workspace_daily_usage(
    workspace_id: str, day_start: datetime, day_end: datetime
) -> dict:
    try:
        namespace = create_ns_name(workspace_id)

        # Query Prometheus for the full day
        breakdown = await metrics_service.get_namespace_breakdown(
            namespace, day_start, day_end
        )

        # Create DAILY usage record
        usage_record = await db.usage.upsert_usage_record(
            workspace_id=uuid.UUID(workspace_id),
            collection_start=day_start,
            collection_end=day_end,
            cpu_core_seconds=breakdown.totals.cpu_core_seconds,
            memory_gb_seconds=breakdown.totals.memory_gb_seconds,
            storage_gb_hours=breakdown.totals.storage_gb_hours,
            record_type=UsageRecordType.DAILY,
        )

        # Upsert service breakdowns for daily record
        for service_name, service_usage in breakdown.by_service.items():
            await db.usage.upsert_usage_breakdown(
                usage_record_id=uuid.UUID(usage_record.id),
                service_name=service_name,
                cpu_core_seconds=service_usage.cpu_core_seconds,
                memory_gb_seconds=service_usage.memory_gb_seconds,
                pod_count=service_usage.pod_count,
            )

        logger.info(
            f"Collected DAILY for {workspace_id} [{day_start.date()}]: "
            f"CPU={breakdown.totals.cpu_core_seconds:.2f}s, "
            f"Mem={breakdown.totals.memory_gb_seconds:.2f}GB-s, "
            f"Storage={breakdown.totals.storage_gb_hours:.2f}GB-h"
        )

        return {
            "workspace_id": workspace_id,
            "date": str(day_start.date()),
            "success": True,
            "usage_record_id": usage_record.id,
        }

    except Exception as e:
        logger.error(f"Error collecting daily usage for {workspace_id}: {e}")
        return {
            "workspace_id": workspace_id,
            "date": str(day_start.date()),
            "success": False,
            "error": str(e),
        }


@flow(log_prints=True)
async def collect_hourly_usage_flow(workspace_id: str) -> dict:
    """Collect usage for previous hour for a single workspace"""
    now = datetime.now(timezone.utc)

    # Calculate previous hour
    # At 01:05 or 01:30, we want 00:00-01:00
    end_time = now.replace(minute=0, second=0, microsecond=0)
    start_time = end_time - timedelta(hours=1)

    logger.info(
        f"Collecting hourly usage for {workspace_id}: {start_time.hour:02d}:00-{end_time.hour:02d}:00"
    )

    result = await collect_workspace_usage_for_hour(workspace_id, start_time, end_time)

    return result


@flow(log_prints=True)
async def backfill_daily_usage_flow(workspace_id: str) -> dict:
    """Backfill yesterday's usage and create DAILY record for a single workspace"""
    now = datetime.now(timezone.utc)
    yesterday = now.date() - timedelta(days=1)

    logger.info(f"Backfilling daily usage for {workspace_id}: {yesterday}")

    # Backfill all 24 hours of yesterday
    hourly_results = []
    for hour in range(24):
        start_time = datetime.combine(yesterday, datetime.min.time()).replace(
            hour=hour, tzinfo=timezone.utc
        )
        end_time = start_time + timedelta(hours=1)

        result = await collect_workspace_usage_for_hour(
            workspace_id, start_time, end_time
        )
        hourly_results.append(result)

    hourly_collected = sum(1 for r in hourly_results if r["success"])
    hourly_failed = sum(1 for r in hourly_results if not r["success"])

    # Create DAILY record by querying full day
    day_start = datetime.combine(yesterday, datetime.min.time()).replace(
        tzinfo=timezone.utc
    )
    day_end = datetime.combine(yesterday, datetime.max.time()).replace(
        second=0, microsecond=0, tzinfo=timezone.utc
    )

    daily_result = await collect_workspace_daily_usage(workspace_id, day_start, day_end)

    logger.info(
        f"Backfill complete for {workspace_id}: "
        f"{hourly_collected} hourly records, "
        f"{'1 daily record' if daily_result['success'] else '0 daily records'}"
    )

    return {
        "workspace_id": workspace_id,
        "date": str(yesterday),
        "hourly_collected": hourly_collected,
        "hourly_failed": hourly_failed,
        "daily_success": daily_result["success"],
        "daily_record_id": daily_result.get("usage_record_id")
        if daily_result["success"]
        else None,
    }


@flow(log_prints=True)
async def forward_for_billing_flow():
    """Forward usage data to billing service"""
    unreported_usage = await db.usage.get_unreported_usage()

    if not unreported_usage:
        logger.info("No unreported usage records to forward")
        return {"forwarded": 0, "failed": 0}

    forwarded = 0
    failed = 0

    for usage in unreported_usage:
        try:
            # TODO: Forward to billing service (e.g., Polar.sh)
            logger.info(
                f"Forwarding usage to billing: workspace={usage.workspace_id}, period={usage.collection_start} to {usage.collection_end}"
            )

            # Mark as reported
            await db.usage.mark_as_reported(
                uuid.UUID(usage.id), datetime.now(timezone.utc)
            )
            forwarded += 1
            logger.info(f"Marked usage as reported: {usage.id}")

        except Exception as e:
            failed += 1
            logger.error(f"Failed to forward usage {usage.id}: {e}")

    logger.info(f"Billing forward complete: {forwarded} forwarded, {failed} failed")
    return {"forwarded": forwarded, "failed": failed}


@flow(log_prints=True)
async def spawn_usage_collection_flows():
    """Spawn usage collection flows"""
    # spawn collect_hourly_usage_flow for all workspaces
    for workspace in await db.workspaces.aget_all():
        await collect_hourly_usage_flow(str(workspace.id))


@flow(log_prints=True)
async def spawn_backfill_daily_usage_flows():
    """Spawn backfill daily usage flows"""
    # spawn backfill_daily_usage_flow for all workspaces
    for workspace in await db.workspaces.aget_all():
        await backfill_daily_usage_flow(str(workspace.id))


# define deployments for the flows that can be called by other flows
collect_hourly_usage_deployment = collect_hourly_usage_flow.to_deployment(
    name="collect-hourly-usage",
)

backfill_daily_usage_deployment = backfill_daily_usage_flow.to_deployment(
    name="backfill-daily-usage",
)

# define deployments for the flows that can be called by other flows

spawn_usage_collection_deployment = spawn_usage_collection_flows.to_deployment(
    name="spawn-usage-collection",
    cron="5 * * * *",
)

spawn_backfill_daily_usage_deployment = spawn_backfill_daily_usage_flows.to_deployment(
    name="spawn-backfill-daily-usage",
    cron="5 0 * * *",
)

forward_for_billing_deployment = forward_for_billing_flow.to_deployment(
    name="forward-for-billing",
    cron="0 2,14 * * *",
)
