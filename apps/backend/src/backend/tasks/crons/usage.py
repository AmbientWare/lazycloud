"""Usage collection and billing cron jobs."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import yaml
from loguru import logger
from models.billing import (
    UsageCollectionConfig,
    UsageUnits,
)
from models.deployments import DeploymentStates
from models.metrics import StorageUsage
from tenacity import (
    RetryError,
    retry,
    stop_after_attempt,
    wait_exponential,
)

from backend.database import get_db_context
from backend.database.compose import ComposeDeploymentPydantic
from backend.database.usage import (
    BreakdownType,
    DailyUsageRecordPydantic,
    DailyUsageStatus,
)
from backend.services import (
    get_depot_service,
    get_metrics_service,
    get_polar_service,
)
from backend.services.k8s import create_ns_name, create_release_name
from backend.services.k8s.client import (
    close_async_api_client,
    get_namespace_pvcs_with_details,
)
from backend.services.polar import PolarService

MAX_BILLING_ATTEMPTS = 5


def sanitize_volume_name(name: str) -> str:
    return name.lower().replace("_", "-").replace(".", "-")[:63].rstrip("-")


async def collect_storage_usage(
    namespace: str, interval_hours: float
) -> list[StorageUsage]:
    """Collect storage usage from K8s PVC API (works with Longhorn and any CSI driver)."""
    pvcs = await get_namespace_pvcs_with_details(namespace)

    storage_list = []
    for pvc in pvcs:
        size_gb = pvc.requested_size_gb
        gb_hours = size_gb * interval_hours
        storage_list.append(
            StorageUsage(
                pvc_name=pvc.name,
                storage_class=pvc.storage_class,
                gb_hours=gb_hours,
                storage_size_gb=size_gb,
            )
        )

    return storage_list


def _parse_deployment_volumes(compose_yaml: str, deployment_id: str) -> set[str]:
    """Parse volume names from Docker Compose YAML."""
    deployment_volumes: set[str] = set()

    try:
        compose_data = yaml.safe_load(compose_yaml)
        if compose_data and "volumes" in compose_data:
            for vol_name in compose_data["volumes"].keys():
                sanitized = sanitize_volume_name(vol_name)
                deployment_volumes.add(sanitized)
    except Exception as e:
        logger.warning(
            f"Failed to parse compose YAML for deployment {deployment_id}: {e}"
        )

    return deployment_volumes


@dataclass
class ServiceEndpoint:
    deployment_id: str
    service_name: str


@dataclass
class WorkspaceDeploymentContext:
    deployments: list[ComposeDeploymentPydantic]
    deployment_map: dict[str, str]
    pvc_map: dict[str, str]
    active_endpoints: list[ServiceEndpoint]


async def _build_deployment_context(workspace_id: str) -> WorkspaceDeploymentContext:
    """Build deployment context with a single query."""
    async with get_db_context() as db:
        deployments = await db.compose_deployments.find({"workspace_id": workspace_id})

    deployment_map: dict[str, str] = {}
    pvc_map: dict[str, str] = {}
    active_endpoints: list[ServiceEndpoint] = []

    for deployment in deployments:
        if deployment.name:
            release_name = create_release_name(workspace_id, deployment.name)
            deployment_map[release_name] = deployment.id

        if deployment.compose_yaml:
            volumes = _parse_deployment_volumes(deployment.compose_yaml, deployment.id)
            for volume_name in volumes:
                pvc_map[volume_name] = deployment.id

        if deployment.state == DeploymentStates.DEPLOYED and deployment.helm_values:
            for service in deployment.helm_values.services:
                if service.ingress and service.ingress.enabled:
                    active_endpoints.append(
                        ServiceEndpoint(
                            deployment_id=deployment.id,
                            service_name=service.name,
                        )
                    )

    return WorkspaceDeploymentContext(
        deployments=deployments,
        deployment_map=deployment_map,
        pvc_map=pvc_map,
        active_endpoints=active_endpoints,
    )


async def collect_workspace_interval(
    workspace_id: str,
    interval_start: datetime,
    interval_end: datetime,
) -> dict:
    """Collect usage for one interval - atomic increments, append-only events."""
    metrics_service = get_metrics_service()

    if not await metrics_service.health_check():
        raise RuntimeError(f"Prometheus unhealthy for {workspace_id} collection")

    async with get_db_context() as db:
        # Check idempotency - skip if already collected
        if await db.usage.is_interval_collected(workspace_id, interval_start):
            logger.debug(
                f"Interval {interval_start} already collected for {workspace_id}"
            )
            return {"status": "already_collected", "workspace_id": workspace_id}

        # Get or create daily record
        usage_date = interval_start.date()
        daily_record = await db.usage.get_or_create_daily_record(
            workspace_id=workspace_id,
            usage_date=usage_date,
            expected_intervals=UsageCollectionConfig.INTERVALS_PER_DAY,
        )

        # Skip if already billed
        if daily_record.status == DailyUsageStatus.BILLED:
            logger.warning(
                f"Day {usage_date} already billed for {workspace_id}, skipping"
            )
            return {"status": "already_billed", "workspace_id": workspace_id}

        # Collect metrics from external services
        namespace = create_ns_name(workspace_id)
        ctx = await _build_deployment_context(workspace_id)

        breakdown = await metrics_service.get_namespace_breakdown(
            namespace, interval_start, interval_end
        )

        interval_hours = UsageUnits.seconds_to_hours(
            (interval_end - interval_start).total_seconds()
        )
        storage_list = await collect_storage_usage(namespace, interval_hours)

        # Collect build minutes from Depot
        build_minutes_by_deployment: dict[str, float] = {}
        total_build_minutes = 0.0
        depot_service = get_depot_service()
        if depot_service.is_configured:
            for deployment in ctx.deployments:
                if deployment.id:
                    mins = await depot_service.get_deployment_build_minutes(
                        deployment_id=deployment.id,
                        start_at=interval_start,
                        end_at=interval_end,
                    )
                    if mins > 0:
                        build_minutes_by_deployment[deployment.id] = mins
                        total_build_minutes += mins

        # Aggregate storage and convert to GB-months
        total_storage_gb_hours = sum(s.gb_hours for s in storage_list)
        storage_gb_months = UsageUnits.gb_hours_to_gb_months(total_storage_gb_hours)

        # Atomic increment of daily record totals (all metered resources)
        await db.usage.atomic_increment_usage(
            record_id=daily_record.id,
            cpu_core_seconds=breakdown.totals.cpu_core_seconds,
            memory_gb_seconds=breakdown.totals.memory_gb_seconds,
            build_minutes=total_build_minutes,
            storage_gb_months=storage_gb_months,
        )

        # Mark interval as collected (idempotency)
        await db.usage.mark_interval_collected(workspace_id, interval_start)

        # Append breakdown events for dashboards
        for pod in breakdown.by_pod:
            deployment_id = (
                ctx.deployment_map.get(pod.release_name) if pod.release_name else None
            )
            await db.usage.add_breakdown_event(
                workspace_id=workspace_id,
                interval_start=interval_start,
                interval_end=interval_end,
                breakdown_type=BreakdownType.COMPUTE,
                resource_name=pod.pod,
                deployment_id=deployment_id,
                service_name=pod.service,
                cpu_core_seconds=pod.cpu_core_seconds,
                memory_gb_seconds=pod.memory_gb_seconds,
            )

        for storage in storage_list:
            deployment_id = ctx.pvc_map.get(storage.pvc_name)
            await db.usage.add_breakdown_event(
                workspace_id=workspace_id,
                interval_start=interval_start,
                interval_end=interval_end,
                breakdown_type=BreakdownType.STORAGE,
                resource_name=storage.pvc_name,
                deployment_id=deployment_id,
                storage_class=storage.storage_class,
                gb_hours=storage.gb_hours,
            )

        for endpoint in ctx.active_endpoints:
            await db.usage.add_breakdown_event(
                workspace_id=workspace_id,
                interval_start=interval_start,
                interval_end=interval_end,
                breakdown_type=BreakdownType.NETWORK,
                resource_name=endpoint.service_name,
                deployment_id=endpoint.deployment_id,
                endpoint_hours=interval_hours,
            )

        for deployment_id, mins in build_minutes_by_deployment.items():
            await db.usage.add_breakdown_event(
                workspace_id=workspace_id,
                interval_start=interval_start,
                interval_end=interval_end,
                breakdown_type=BreakdownType.BUILD,
                resource_name=deployment_id,
                deployment_id=deployment_id,
                build_minutes=mins,
            )

        # All changes committed together on context exit

    logger.debug(
        f"Collected {workspace_id} [{interval_start.strftime('%H:%M')}]: "
        f"CPU={breakdown.totals.cpu_core_seconds:.2f}s, "
        f"Mem={breakdown.totals.memory_gb_seconds:.2f}GB-s"
    )

    return {
        "status": "collected",
        "workspace_id": workspace_id,
        "interval": interval_start.isoformat(),
    }


async def collect_interval_usage(workspace_id: str) -> dict:
    """Collect usage for previous interval for a single workspace."""
    now = datetime.now(timezone.utc)
    end_time = UsageCollectionConfig.round_time_to_interval(now)
    start_time = end_time - UsageCollectionConfig.COLLECTION_INTERVAL_TIMEDELTA

    logger.info(
        f"Collecting usage for {workspace_id}: "
        f"{start_time.strftime('%H:%M')}-{end_time.strftime('%H:%M')}"
    )

    try:
        result = await collect_workspace_interval(workspace_id, start_time, end_time)
        return {**result, "success": True}
    except Exception as e:
        logger.error(f"Failed to collect usage for {workspace_id}: {e}")
        return {
            "workspace_id": workspace_id,
            "success": False,
            "error": str(e),
        }


async def spawn_usage_collection_job(ctx: dict[str, Any]) -> dict[str, Any]:
    """Spawn usage collection for all active workspaces - parallel execution.

    Args:
        ctx: SAQ job context

    Returns:
        Result dict with total, failed, and success counts
    """
    async with get_db_context() as db:
        active_workspaces = await db.workspaces.get_active_workspaces()

    logger.info(
        f"Collecting usage for {len(active_workspaces)} active workspaces "
        f"(interval: {UsageCollectionConfig.COLLECTION_INTERVAL.value} minutes)"
    )

    # Parallel collection with concurrency limit
    semaphore = asyncio.Semaphore(20)

    async def collect_with_limit(workspace):
        async with semaphore:
            return await collect_interval_usage(str(workspace.id))

    try:
        results = await asyncio.gather(
            *[collect_with_limit(ws) for ws in active_workspaces],
            return_exceptions=True,
        )

        # Count failures
        failed_count = sum(
            1
            for r in results
            if isinstance(r, Exception)
            or (isinstance(r, dict) and not r.get("success", False))
        )
        total_count = len(results)

        if total_count > 0 and failed_count / total_count > 0.3:
            logger.critical(
                f"HIGH FAILURE RATE in usage collection: {failed_count}/{total_count} "
                f"({failed_count / total_count * 100:.1f}%) workspaces failed"
            )
        elif failed_count > 0:
            logger.error(f"Usage collection had {failed_count}/{total_count} failures")

        return {
            "total": total_count,
            "failed": failed_count,
            "success": total_count - failed_count,
        }
    finally:
        # Clean up aiohttp connections from kubernetes-asyncio and depot service
        await close_async_api_client()
        await get_depot_service().close()


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=4, max=30),
    reraise=True,
)
async def _send_to_polar_with_retry(
    polar_service: PolarService,
    record: DailyUsageRecordPydantic,
    external_customer_id: str,
    idempotency_key: str,
) -> bool:
    """Send usage to Polar with exponential backoff retry."""
    success = await polar_service.usage.send_daily_usage(
        record=record,
        external_customer_id=external_customer_id,
        idempotency_key=idempotency_key,
    )
    if not success:
        raise RuntimeError(f"Polar rejected usage for workspace {record.workspace_id}")
    return success


async def finalize_and_bill_job(ctx: dict[str, Any]) -> dict[str, Any]:
    """Finalize yesterday's usage and send to Polar for billing.

    Args:
        ctx: SAQ job context

    Returns:
        Result dict with billed, failed, and skipped counts
    """
    logger.info("Starting daily billing process")
    start_time = datetime.now(timezone.utc)

    yesterday = (start_time - timedelta(days=1)).date()

    async with get_db_context() as db:
        unbilled_records = await db.usage.get_unbilled_for_date(yesterday)

    logger.info(f"Found {len(unbilled_records)} unbilled records for {yesterday}")

    if not unbilled_records:
        return {"billed": 0, "failed": 0, "skipped": 0}

    polar_service = get_polar_service()
    billed = 0
    failed = 0
    skipped = 0

    for record in unbilled_records:
        usage_date_str = str(record.usage_date)

        # Skip records that have exceeded max attempts
        if record.billing_attempts >= MAX_BILLING_ATTEMPTS:
            logger.warning(
                f"Skipping workspace {record.workspace_id} - exceeded max billing attempts "
                f"({record.billing_attempts}/{MAX_BILLING_ATTEMPTS})"
            )
            async with get_db_context() as db:
                await db.billing_audit.log_billing_skipped(
                    workspace_id=record.workspace_id,
                    record_id=record.id,
                    reason=f"Exceeded max billing attempts ({MAX_BILLING_ATTEMPTS})",
                    usage_date=usage_date_str,
                )
            skipped += 1
            continue

        # Require 100% collection before billing
        if record.intervals_collected < record.expected_intervals:
            logger.error(
                f"INCOMPLETE USAGE - REQUIRES INVESTIGATION: "
                f"Workspace {record.workspace_id} has {record.intervals_collected}/{record.expected_intervals} "
                f"intervals for {yesterday}. Skipping billing until resolved."
            )
            async with get_db_context() as db:
                await db.billing_audit.log_billing_skipped(
                    workspace_id=record.workspace_id,
                    record_id=record.id,
                    reason=f"Incomplete intervals: {record.intervals_collected}/{record.expected_intervals}",
                    usage_date=usage_date_str,
                )
            skipped += 1
            continue

        # Get workspace owner
        async with get_db_context() as db:
            workspace_owner = await db.workspaces.get_owner_user(record.workspace_id)

        if not workspace_owner:
            logger.error(f"No owner found for workspace {record.workspace_id}")
            async with get_db_context() as db:
                await db.billing_audit.log_billing_skipped(
                    workspace_id=record.workspace_id,
                    record_id=record.id,
                    reason="No workspace owner found",
                    usage_date=usage_date_str,
                )
            skipped += 1
            continue

        # Use record ID as idempotency key - Polar will dedupe
        idempotency_key = str(record.id)
        current_attempt = record.billing_attempts + 1

        # Log billing started
        async with get_db_context() as db:
            await db.billing_audit.log_billing_started(
                workspace_id=record.workspace_id,
                record_id=record.id,
                usage_date=usage_date_str,
                attempt=current_attempt,
            )

        try:
            if not polar_service.usage.enabled:
                logger.error(
                    f"BILLING SKIPPED - Polar disabled: workspace {record.workspace_id} "
                    f"for {yesterday}. Set POLAR_ACCESS_TOKEN to enable billing."
                )
                async with get_db_context() as db:
                    await db.billing_audit.log_billing_skipped(
                        workspace_id=record.workspace_id,
                        record_id=record.id,
                        reason="Polar service disabled (POLAR_ACCESS_TOKEN not configured)",
                        usage_date=usage_date_str,
                    )
                skipped += 1
                continue

            # Increment attempt counter before trying
            async with get_db_context() as db:
                await db.usage.increment_billing_attempt(record.id)

            await _send_to_polar_with_retry(
                polar_service=polar_service,
                record=record,
                external_customer_id=workspace_owner.workos_id,
                idempotency_key=idempotency_key,
            )
            billing_id = idempotency_key

            # Mark as billed and log success
            async with get_db_context() as db:
                await db.usage.mark_as_billed(record.id, billing_id)
                await db.billing_audit.log_billing_completed(
                    workspace_id=record.workspace_id,
                    record_id=record.id,
                    billing_id=billing_id,
                    usage_date=usage_date_str,
                )

            billed += 1
            logger.info(f"Billed workspace {record.workspace_id} for {yesterday}")

        except RetryError as e:
            error_msg = str(e.last_attempt.exception()) if e.last_attempt else str(e)
            logger.error(
                f"Failed to bill {record.workspace_id} after retries: {error_msg}"
            )
            async with get_db_context() as db:
                await db.usage.increment_billing_attempt(record.id, error=error_msg)
                await db.billing_audit.log_billing_failed(
                    workspace_id=record.workspace_id,
                    record_id=record.id,
                    error=error_msg,
                    attempt=current_attempt,
                    usage_date=usage_date_str,
                )
            failed += 1

        except Exception as e:
            error_msg = str(e)
            logger.exception(f"Exception billing {record.workspace_id}: {e}")
            async with get_db_context() as db:
                await db.usage.increment_billing_attempt(record.id, error=error_msg)
                await db.billing_audit.log_billing_failed(
                    workspace_id=record.workspace_id,
                    record_id=record.id,
                    error=error_msg,
                    attempt=current_attempt,
                    usage_date=usage_date_str,
                )
            failed += 1

    elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
    logger.info(
        f"Billing complete: {billed} billed, {failed} failed, {skipped} skipped "
        f"(took {elapsed:.2f}s)"
    )

    # Alert on high billing failure rate
    total_attempted = billed + failed
    if total_attempted > 0 and failed / total_attempted > 0.3:
        logger.critical(
            f"HIGH BILLING FAILURE RATE: {failed}/{total_attempted} "
            f"({failed / total_attempted * 100:.1f}%) - "
            f"date={yesterday}, billed={billed}, failed={failed}, skipped={skipped}"
        )

    return {"billed": billed, "failed": failed, "skipped": skipped}


# Test-friendly wrapper (without ctx parameter)
async def finalize_and_bill() -> dict[str, Any]:
    """Finalize yesterday's usage and send to Polar for billing.

    This is a wrapper for finalize_and_bill_job that can be called without
    the SAQ context, useful for testing.

    Returns:
        Result dict with billed, failed, and skipped counts
    """
    return await finalize_and_bill_job({})


async def catch_up_missing_intervals_job(ctx: dict[str, Any]) -> dict[str, Any]:
    """Catch up any missed intervals for active workspaces.

    Args:
        ctx: SAQ job context

    Returns:
        Result dict with caught_up count
    """
    now = datetime.now(timezone.utc)
    today = now.date()

    async with get_db_context() as db:
        active_workspaces = await db.workspaces.get_active_workspaces()

    logger.info(f"Checking {len(active_workspaces)} workspaces for missed intervals")

    total_caught_up = 0

    try:
        for workspace in active_workspaces:
            workspace_id = str(workspace.id)

            # Check intervals for today
            for minute in UsageCollectionConfig.get_minute_marks():
                for hour in range(now.hour + 1):
                    interval_start = datetime.combine(
                        today, datetime.min.time()
                    ).replace(hour=hour, minute=minute, tzinfo=timezone.utc)

                    # Skip future intervals
                    if interval_start >= now:
                        continue

                    interval_end = (
                        interval_start
                        + UsageCollectionConfig.COLLECTION_INTERVAL_TIMEDELTA
                    )

                    async with get_db_context() as db:
                        if not await db.usage.is_interval_collected(
                            workspace_id, interval_start
                        ):
                            try:
                                await collect_workspace_interval(
                                    workspace_id, interval_start, interval_end
                                )
                                total_caught_up += 1
                                logger.debug(
                                    f"Caught up interval {interval_start} for {workspace_id}"
                                )
                            except Exception as e:
                                logger.warning(
                                    f"Failed to catch up {interval_start} for {workspace_id}: {e}"
                                )

        logger.info(f"Caught up {total_caught_up} missed intervals")
        return {"caught_up": total_caught_up}

    finally:
        await close_async_api_client()
        await get_depot_service().close()


async def alert_stuck_records_job(ctx: dict[str, Any]) -> dict[str, Any]:
    """Alert on records stuck in collecting status for 1+ days.

    Args:
        ctx: SAQ job context

    Returns:
        Result dict with stuck_count
    """
    today = datetime.now(timezone.utc).date()

    async with get_db_context() as db:
        stuck_records = await db.usage.get_stuck_records(before_date=today)

    if stuck_records:
        for record in stuck_records:
            days_stuck = (today - record.usage_date).days
            logger.critical(
                f"STUCK USAGE RECORD - REQUIRES MANUAL INVESTIGATION: "
                f"workspace={record.workspace_id}, date={record.usage_date}, "
                f"intervals={record.intervals_collected}/{record.expected_intervals}, "
                f"days_stuck={days_stuck}"
            )

        logger.critical(
            f"STUCK BILLING RECORDS: {len(stuck_records)} require investigation - "
            f"oldest={min(r.usage_date for r in stuck_records)}"
        )

    return {"stuck_count": len(stuck_records)}
