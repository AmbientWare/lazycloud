from datetime import date, datetime, timedelta, timezone

import yaml
from loguru import logger
from prefect import flow, task

from lazycloud_api.database import db
from lazycloud_api.services import (
    get_metrics_service,
    get_polar_service,
)
from lazycloud_api.services.k8s import create_ns_name, create_release_name
from shared.models.billing import (
    UsageCollectionConfig,
    UsageRecordStatus,
    UsageRecordType,
)


def sanitize_volume_name(name: str) -> str:
    return name.lower().replace("_", "-").replace(".", "-")[:63].rstrip("-")


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


async def _get_deployment_map(workspace_id: str) -> dict[str, str]:
    """Get mapping of release_name -> deployment_id for active deployments in workspace."""
    deployment_name_map = (
        await db.compose_deployments.aget_active_deployments_for_workspace(workspace_id)
    )

    release_name_map = {}
    for deployment_name, deployment_id in deployment_name_map.items():
        release_name = create_release_name(workspace_id, deployment_name)
        release_name_map[release_name] = deployment_id

    return release_name_map


async def _get_pvc_deployment_map(workspace_id: str) -> dict[str, str]:
    """Get mapping of sanitized PVC name -> deployment_id for active deployments."""
    deployments = await db.compose_deployments.afind({"workspace_id": workspace_id})

    pvc_map = {}
    for deployment in deployments:
        if not deployment.compose_yaml:
            continue

        volumes = _parse_deployment_volumes(deployment.compose_yaml, deployment.id)
        for volume_name in volumes:
            pvc_map[volume_name] = deployment.id

    return pvc_map


@task(retries=2, retry_delay_seconds=60)
async def collect_workspace_usage_for_interval(
    workspace_id: str,
    start_time: datetime,
    end_time: datetime,
    status: UsageRecordStatus = UsageRecordStatus.DRAFT,
) -> dict:
    metrics_service = get_metrics_service()

    try:
        # Check Prometheus health before attempting collection
        if not await metrics_service.health_check():
            logger.error("Prometheus is not healthy, skipping usage collection")
            return {
                "workspace_id": workspace_id,
                "hour": start_time.hour,
                "success": False,
                "error": "Prometheus unhealthy",
            }

        namespace = create_ns_name(workspace_id)

        # Get detailed breakdown from Prometheus
        breakdown = await metrics_service.get_namespace_breakdown(
            namespace, start_time, end_time
        )

        # Upsert usage record (update if exists, insert if not)
        usage_record = await db.usage.upsert_usage_record(
            workspace_id=workspace_id,
            collection_start=start_time,
            collection_end=end_time,
            cpu_core_seconds=breakdown.totals.cpu_core_seconds,
            memory_gb_seconds=breakdown.totals.memory_gb_seconds,
            storage_gb_hours=breakdown.totals.storage_gb_hours,
            s3_gb_hours=breakdown.totals.s3_gb_hours,
            efs_gb_hours=breakdown.totals.efs_gb_hours,
            record_type=UsageCollectionConfig.get_record_type(),
            status=status,
        )

        # Get deployment mappings
        deployment_map = await _get_deployment_map(workspace_id)
        pvc_deployment_map = await _get_pvc_deployment_map(workspace_id)

        # Upsert compute breakdowns (per pod)
        for pod_usage in breakdown.by_pod:
            deployment_id = None
            if pod_usage.release_name:
                deployment_id = deployment_map.get(pod_usage.release_name)

            await db.usage.upsert_compute_breakdown(
                usage_record_id=usage_record.id,
                pod_name=pod_usage.pod,
                cpu_core_seconds=pod_usage.cpu_core_seconds,
                memory_gb_seconds=pod_usage.memory_gb_seconds,
                deployment_id=deployment_id,
                service_name=pod_usage.service,
            )

        # Upsert storage breakdowns (per PVC)
        for storage_usage in breakdown.by_pvc:
            deployment_id = pvc_deployment_map.get(storage_usage.pvc_name)

            await db.usage.upsert_storage_breakdown(
                usage_record_id=usage_record.id,
                pvc_name=storage_usage.pvc_name,
                storage_class=storage_usage.storage_class,
                gb_hours=storage_usage.gb_hours,
                deployment_id=deployment_id,
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
        # Aggregate from interval records instead of re-querying Prometheus
        interval_records = await db.usage.get_workspace_usage(
            workspace_id=workspace_id,
            start_date=day_start,
            end_date=day_end,
            record_type=UsageCollectionConfig.get_record_type(),
        )

        if not interval_records:
            logger.warning(
                f"No interval records found for {workspace_id} on {day_start.date()}"
            )
            return {
                "workspace_id": workspace_id,
                "date": str(day_start.date()),
                "success": False,
                "error": "No interval records available",
            }

        # Aggregate totals from interval records
        total_cpu = sum(r.cpu_core_seconds for r in interval_records)
        total_memory = sum(r.memory_gb_seconds for r in interval_records)
        total_storage = sum(r.storage_gb_hours for r in interval_records)
        total_s3 = sum(r.s3_gb_hours for r in interval_records)
        total_efs = sum(r.efs_gb_hours for r in interval_records)

        # Aggregate compute breakdowns from all interval records
        compute_aggregates: dict[str, dict] = {}
        for record in interval_records:
            for breakdown in record.compute_breakdowns:
                if breakdown.pod_name not in compute_aggregates:
                    compute_aggregates[breakdown.pod_name] = {
                        "cpu_core_seconds": 0.0,
                        "memory_gb_seconds": 0.0,
                        "deployment_id": breakdown.deployment_id,
                        "service_name": breakdown.service_name,
                    }
                compute_aggregates[breakdown.pod_name]["cpu_core_seconds"] += (
                    breakdown.cpu_core_seconds
                )
                compute_aggregates[breakdown.pod_name]["memory_gb_seconds"] += (
                    breakdown.memory_gb_seconds
                )

        # Aggregate storage breakdowns from all interval records
        storage_aggregates: dict[tuple[str, str], dict] = {}
        for record in interval_records:
            for breakdown in record.storage_breakdowns:
                key = (breakdown.pvc_name, breakdown.storage_class)
                if key not in storage_aggregates:
                    storage_aggregates[key] = {
                        "gb_hours": 0.0,
                        "deployment_id": breakdown.deployment_id,
                    }
                storage_aggregates[key]["gb_hours"] += breakdown.gb_hours

        # Create DAILY usage record with FINALIZED status
        usage_record = await db.usage.upsert_usage_record(
            workspace_id=workspace_id,
            collection_start=day_start,
            collection_end=day_end,
            cpu_core_seconds=total_cpu,
            memory_gb_seconds=total_memory,
            storage_gb_hours=total_storage,
            s3_gb_hours=total_s3,
            efs_gb_hours=total_efs,
            record_type=UsageRecordType.DAILY,
            status=UsageRecordStatus.FINALIZED,
        )

        # Upsert compute breakdowns for daily record
        for pod_name, aggregates in compute_aggregates.items():
            await db.usage.upsert_compute_breakdown(
                usage_record_id=usage_record.id,
                pod_name=pod_name,
                cpu_core_seconds=aggregates["cpu_core_seconds"],
                memory_gb_seconds=aggregates["memory_gb_seconds"],
                deployment_id=aggregates["deployment_id"],
                service_name=aggregates["service_name"],
            )

        # Upsert storage breakdowns for daily record
        for (pvc_name, storage_class), aggregates in storage_aggregates.items():
            await db.usage.upsert_storage_breakdown(
                usage_record_id=usage_record.id,
                pvc_name=pvc_name,
                storage_class=storage_class,
                gb_hours=aggregates["gb_hours"],
                deployment_id=aggregates["deployment_id"],
            )

        logger.info(
            f"Collected DAILY for {workspace_id} [{day_start.date()}]: "
            f"CPU={total_cpu:.2f}s, "
            f"Mem={total_memory:.2f}GB-s, "
            f"Storage={total_storage:.2f}GB-h "
            f"(aggregated from {len(interval_records)} interval records)"
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
async def collect_interval_usage(workspace_id: str) -> dict:
    """Collect usage for previous interval for a single workspace"""
    now = datetime.now(timezone.utc)

    # Round down to previous interval mark
    end_time = UsageCollectionConfig.round_time_to_interval(now)
    start_time = end_time - UsageCollectionConfig.COLLECTION_INTERVAL_TIMEDELTA

    logger.info(
        f"Collecting usage for {workspace_id}: "
        f"{start_time.strftime('%H:%M')}-{end_time.strftime('%H:%M')}"
    )

    result = await collect_workspace_usage_for_interval(
        workspace_id, start_time, end_time
    )

    return result


@flow(log_prints=True)
async def backfill_daily_usage(
    workspace_id: str, collection_date: date | None = None
) -> dict:
    """Process an INCOMPLETE daily usage record for a workspace"""
    now = datetime.now(timezone.utc)
    target_date = collection_date if collection_date else now.date() - timedelta(days=1)

    logger.info(f"Processing daily usage backfill for {workspace_id}: {target_date}")

    # Define day boundaries
    day_start = datetime.combine(target_date, datetime.min.time()).replace(
        tzinfo=timezone.utc
    )
    day_end = datetime.combine(target_date, datetime.max.time()).replace(
        second=59, microsecond=999999, tzinfo=timezone.utc
    )

    # Get workspace to check created_at and deleted_at
    workspace = await db.workspaces.aget_by_id(workspace_id)
    if not workspace:
        logger.error(f"Workspace {workspace_id} not found")
        return {
            "workspace_id": workspace_id,
            "date": str(target_date),
            "intervals_collected": 0,
            "intervals_failed": 0,
            "daily_success": False,
            "error": "Workspace not found",
        }

    # If workspace was created on target date, start from creation hour
    start_hour = 0
    if workspace.created_at.date() == target_date:
        start_hour = workspace.created_at.hour
        logger.info(
            f"Workspace {workspace_id} created at {workspace.created_at}, "
            f"starting backfill from hour {start_hour}"
        )

    # If workspace was deleted on target date, end at deletion hour
    end_hour = 23
    if workspace.deleted_at and workspace.deleted_at.date() == target_date:
        end_hour = workspace.deleted_at.hour
        logger.info(
            f"Workspace {workspace_id} deleted at {workspace.deleted_at}, "
            f"ending backfill at hour {end_hour}"
        )

    # Backfill only the intervals the workspace existed
    interval_results = []
    for hour in range(start_hour, end_hour + 1):
        for minute in UsageCollectionConfig.get_minute_marks():
            start_time = datetime.combine(target_date, datetime.min.time()).replace(
                hour=hour, minute=minute, tzinfo=timezone.utc
            )
            end_time = start_time + UsageCollectionConfig.COLLECTION_INTERVAL_TIMEDELTA

            result = await collect_workspace_usage_for_interval(
                workspace_id, start_time, end_time, status=UsageRecordStatus.FINALIZED
            )
            interval_results.append(result)

    intervals_collected = sum(1 for r in interval_results if r["success"])
    intervals_failed = sum(1 for r in interval_results if not r["success"])
    expected_intervals = (
        end_hour - start_hour + 1
    ) * UsageCollectionConfig.INTERVALS_PER_HOUR

    # Only aggregate and mark as FINALIZED if we got ALL interval records
    if intervals_collected == expected_intervals:
        daily_result = await collect_workspace_daily_usage(
            workspace_id, day_start, day_end
        )

        if daily_result["success"]:
            logger.info(
                f"Backfill complete for {workspace_id}: "
                f"{intervals_collected}/{expected_intervals} interval records collected, "
                f"daily record FINALIZED"
            )
            return {
                "workspace_id": workspace_id,
                "date": str(target_date),
                "intervals_collected": intervals_collected,
                "intervals_failed": intervals_failed,
                "daily_success": True,
                "daily_record_id": daily_result["usage_record_id"],
                "status": "finalized",
            }

    # If we're here, either didn't collect all intervals OR daily aggregation failed
    # INCOMPLETE record remains in DB for retry
    logger.warning(
        f"Backfill incomplete for {workspace_id}: "
        f"collected {intervals_collected}/{expected_intervals} interval records, "
        f"daily record remains INCOMPLETE"
    )

    return {
        "workspace_id": workspace_id,
        "date": str(target_date),
        "intervals_collected": intervals_collected,
        "intervals_failed": intervals_failed,
        "daily_success": False,
        "status": "incomplete",
    }


@flow(log_prints=True)
async def forward_for_billing():
    """Forward finalized usage data to billing service"""
    logger.info("Starting billing forward process")
    start_time = datetime.now(timezone.utc)

    finalized_usage = await db.usage.get_finalized_usage()
    logger.info(
        f"Found {len(finalized_usage) if finalized_usage else 0} finalized usage records to forward"
    )

    polar_service = get_polar_service()

    if not finalized_usage:
        logger.info("No finalized usage records to forward")
        return {"forwarded": 0, "failed": 0}

    forwarded = 0
    failed = 0

    logger.info(f"Processing {len(finalized_usage)} usage records")
    for usage in finalized_usage:
        # bill usage to the workspace owner
        workspace_owner = await db.workspaces.aget_owner_user(usage.workspace_id)
        if not workspace_owner:
            logger.error(
                f"No owner found for workspace {usage.workspace_id}, skipping usage {usage.id}"
            )
            failed += 1
            continue

        try:
            logger.info(
                f"Forwarding usage to billing: workspace={usage.workspace_id}, "
                f"period={usage.collection_start} to {usage.collection_end}, "
                f"CPU={usage.cpu_core_seconds:.0f}s, "
                f"Memory={usage.memory_gb_seconds:.0f}GB-s, "
                f"S3={usage.s3_gb_hours:.2f}GB-h, "
                f"EFS={usage.efs_gb_hours:.2f}GB-h"
            )

            should_mark_reported = False

            if not polar_service.usage.enabled:
                # Polar disabled - mark as reported (graceful degradation)
                logger.debug(
                    f"Polar disabled, marking usage {usage.id} as reported without sending"
                )
                should_mark_reported = True

            else:
                # Send usage data to Polar
                success = await polar_service.usage.send_workspace_usage(
                    usage_record=usage, external_customer_id=workspace_owner.clerk_id
                )

                # Only mark as reported if event succeeded
                if success:
                    should_mark_reported = True
                    logger.info(
                        f"Usage event sent successfully for usage {usage.id} "
                        f"(workspace={usage.workspace_id})"
                    )
                else:
                    logger.error(
                        f"Failed to send usage event for usage {usage.id} "
                        f"(workspace={usage.workspace_id}). Will retry on next run."
                    )

            # Mark as reported only if delivery confirmed or gracefully skipped
            if should_mark_reported:
                await db.usage.mark_as_reported(usage.id)
                forwarded += 1
                logger.info(f"Marked usage as reported: {usage.id}")
            else:
                failed += 1

        except Exception as e:
            failed += 1
            logger.exception(
                f"Exception while forwarding usage {usage.id} "
                f"(workspace={usage.workspace_id}): {e}"
            )

    elapsed_time = (datetime.now(timezone.utc) - start_time).total_seconds()
    logger.info(
        f"Billing forward complete: {forwarded} forwarded, {failed} failed "
        f"(took {elapsed_time:.2f}s)"
    )
    return {"forwarded": forwarded, "failed": failed}


@flow(log_prints=True)
async def spawn_usage_collection():
    """Spawn usage collection flows for active workspaces"""
    # Get only active workspaces (skip deleted ones)
    active_workspaces = await db.workspaces.aget_active_workspaces()

    logger.info(
        f"Collecting usage for {len(active_workspaces)} active workspaces "
        f"(interval: {UsageCollectionConfig.COLLECTION_INTERVAL.value} minutes)"
    )

    # Spawn collect_interval_usage for all active workspaces
    results = []
    for workspace in active_workspaces:
        result = await collect_interval_usage(str(workspace.id))
        results.append(result)

    # Monitor collection failures
    failed_count = sum(1 for r in results if not r.get("success", False))
    total_count = len(results)

    if failed_count > 0:
        logger.warning(
            f"Usage collection completed with {failed_count}/{total_count} failures"
        )

    # Alert if failure rate is high
    if total_count > 0 and failed_count / total_count > 0.3:
        logger.critical(
            f"HIGH FAILURE RATE in usage collection: {failed_count}/{total_count} "
            f"({failed_count / total_count * 100:.1f}%) workspaces failed"
        )

    return {
        "total": total_count,
        "failed": failed_count,
        "success": total_count - failed_count,
    }


@flow(log_prints=True)
async def mark_workspaces_for_backfill():
    """Mark workspaces that need daily usage backfill by creating INCOMPLETE records (outbox pattern)"""
    now = datetime.now(timezone.utc)
    yesterday = now.date() - timedelta(days=1)
    yesterday_start = datetime.combine(yesterday, datetime.min.time()).replace(
        tzinfo=timezone.utc
    )
    yesterday_end = datetime.combine(yesterday, datetime.max.time()).replace(
        second=59, microsecond=999999, tzinfo=timezone.utc
    )

    # Efficiently get workspaces that existed yesterday (at database level):
    # 1. Active workspaces created before today (existed yesterday)
    # 2. Workspaces deleted yesterday (existed at some point yesterday)
    today_start = datetime.combine(now.date(), datetime.min.time()).replace(
        tzinfo=timezone.utc
    )
    active_workspaces = await db.workspaces.aget_active_workspaces_before(today_start)
    deleted_yesterday = await db.workspaces.aget_deleted_in_range(
        yesterday_start, yesterday_end
    )

    # Combine and deduplicate
    candidate_workspaces = {ws.id: ws for ws in active_workspaces + deleted_yesterday}

    logger.info(
        f"Found {len(candidate_workspaces)} candidate workspaces for backfill "
        f"({len(active_workspaces)} active, {len(deleted_yesterday)} deleted yesterday)"
    )

    # Mark workspaces that need backfilling with INCOMPLETE records
    marked = 0
    skipped_already_done = 0

    for workspace in candidate_workspaces.values():
        # Check if already has a FINALIZED or REPORTED daily record for yesterday
        existing_records = await db.usage.get_workspace_usage(
            workspace_id=workspace.id,
            start_date=yesterday_start,
            end_date=yesterday_end,
            record_type=UsageRecordType.DAILY,
        )

        # Skip if already finalized or reported
        if existing_records:
            record_status = existing_records[0].status
            if record_status in (
                UsageRecordStatus.FINALIZED,
                UsageRecordStatus.REPORTED,
            ):
                logger.debug(
                    f"Skipping {workspace.id}: already has {record_status} daily record for {yesterday}"
                )
                skipped_already_done += 1
                continue

            # Already has INCOMPLETE or DRAFT record, no need to recreate
            if record_status == UsageRecordStatus.INCOMPLETE:
                logger.debug(f"Workspace {workspace.id} already marked as INCOMPLETE")
                marked += 1
                continue

        # Create INCOMPLETE record as marker for processing
        try:
            await db.usage.upsert_usage_record(
                workspace_id=workspace.id,
                collection_start=yesterday_start,
                collection_end=yesterday_end,
                cpu_core_seconds=0.0,
                memory_gb_seconds=0.0,
                storage_gb_hours=0.0,
                s3_gb_hours=0.0,
                efs_gb_hours=0.0,
                record_type=UsageRecordType.DAILY,
                status=UsageRecordStatus.INCOMPLETE,
            )
            marked += 1
            logger.debug(f"Marked workspace {workspace.id} for backfill")
        except Exception as e:
            logger.error(f"Failed to mark workspace {workspace.id}: {e}")

    logger.info(
        f"Marked {marked} workspaces for backfill on {yesterday} "
        f"(skipped {skipped_already_done} already done)"
    )

    return {
        "date": str(yesterday),
        "marked": marked,
        "skipped_already_done": skipped_already_done,
        "total_candidates": len(candidate_workspaces),
    }


@flow(log_prints=True)
async def process_incomplete_usage():
    """Process incomplete daily usage records (outbox pattern).

    This flow handles both:
    1. Initial backfill processing (after marking)
    2. Retry processing (for previously failed attempts)
    """
    incomplete_records = await db.usage.get_incomplete_usage()

    if not incomplete_records:
        logger.info("No incomplete daily usage records to process")
        return {"total": 0, "finalized": 0, "still_incomplete": 0, "failed": 0}

    logger.info(
        f"Found {len(incomplete_records)} incomplete daily records, processing..."
    )

    finalized = 0
    still_incomplete = 0
    failed = 0

    for record in incomplete_records:
        # Process each incomplete record by running backfill flow
        # Pass the collection date from the record to ensure we process the correct date
        result = await backfill_daily_usage(
            str(record.workspace_id), record.collection_start.date()
        )

        if result.get("daily_success"):
            finalized += 1
        elif result.get("status") == "incomplete":
            still_incomplete += 1
        else:
            failed += 1

    logger.info(
        f"Incomplete usage processing complete: "
        f"{finalized} finalized, {still_incomplete} still incomplete, {failed} failed"
    )

    return {
        "total": len(incomplete_records),
        "finalized": finalized,
        "still_incomplete": still_incomplete,
        "failed": failed,
    }


# define deployments for the flows that can be called by other flows
collect_interval_usage_deployment = collect_interval_usage.to_deployment(
    name="collect-interval-usage",
)

backfill_daily_usage_deployment = backfill_daily_usage.to_deployment(
    name="backfill-daily-usage",
)

# define deployments for the flows that can be called by other flows

spawn_usage_collection_deployment = spawn_usage_collection.to_deployment(
    name="spawn-usage-collection",
    cron=UsageCollectionConfig.get_cron_expression(),
)

mark_workspaces_for_backfill_deployment = mark_workspaces_for_backfill.to_deployment(
    name="mark-workspaces-for-backfill",
    cron="5,35 0,12 * * *",  # 4x daily for redundancy: 00:05, 00:35, 12:05, 12:35
)

process_incomplete_usage_deployment = process_incomplete_usage.to_deployment(
    name="process-incomplete-usage",
    cron="10,40 */3 * * *",  # Every 3 hours at :10 and :40 past the hour
)

forward_for_billing_deployment = forward_for_billing.to_deployment(
    name="forward-for-billing",
    cron="0 2,14 * * *",  # Every day at 02:00 and 14:00
)
