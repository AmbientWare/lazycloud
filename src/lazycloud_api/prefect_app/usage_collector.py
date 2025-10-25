import uuid
from datetime import date, datetime, timedelta, timezone

from loguru import logger
from prefect import flow, task

from lazycloud_api.database import db
from lazycloud_api.database.usage import UsageRecordStatus, UsageRecordType
from lazycloud_api.services import metrics_service, polar_service
from lazycloud_api.services.k8s import create_ns_name


@task(retries=2, retry_delay_seconds=60)
async def collect_workspace_usage_for_hour(
    workspace_id: str, start_time: datetime, end_time: datetime
) -> dict:
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
        # Aggregate from hourly records instead of re-querying Prometheus
        hourly_records = await db.usage.get_workspace_usage(
            workspace_id=uuid.UUID(workspace_id),
            start_date=day_start,
            end_date=day_end,
            record_type=UsageRecordType.HOURLY,
        )

        if not hourly_records:
            logger.warning(
                f"No hourly records found for {workspace_id} on {day_start.date()}"
            )
            return {
                "workspace_id": workspace_id,
                "date": str(day_start.date()),
                "success": False,
                "error": "No hourly records available",
            }

        # Aggregate totals from hourly records
        total_cpu = sum(r.cpu_core_seconds for r in hourly_records)
        total_memory = sum(r.memory_gb_seconds for r in hourly_records)
        total_storage = sum(r.storage_gb_hours for r in hourly_records)

        # Aggregate service breakdowns from all hourly records
        service_aggregates: dict[str, dict[str, float | int]] = {}
        for record in hourly_records:
            for breakdown in record.breakdowns:
                if breakdown.service_name not in service_aggregates:
                    service_aggregates[breakdown.service_name] = {
                        "cpu_core_seconds": 0.0,
                        "memory_gb_seconds": 0.0,
                        "pod_count": 0,
                    }
                service_aggregates[breakdown.service_name]["cpu_core_seconds"] += (
                    breakdown.cpu_core_seconds
                )
                service_aggregates[breakdown.service_name]["memory_gb_seconds"] += (
                    breakdown.memory_gb_seconds
                )
                # Use max pod count seen across all hourly records
                service_aggregates[breakdown.service_name]["pod_count"] = max(
                    service_aggregates[breakdown.service_name]["pod_count"],
                    breakdown.pod_count,
                )

        # Create DAILY usage record with FINALIZED status
        usage_record = await db.usage.upsert_usage_record(
            workspace_id=uuid.UUID(workspace_id),
            collection_start=day_start,
            collection_end=day_end,
            cpu_core_seconds=total_cpu,
            memory_gb_seconds=total_memory,
            storage_gb_hours=total_storage,
            record_type=UsageRecordType.DAILY,
            status=UsageRecordStatus.FINALIZED,
        )

        # Upsert service breakdowns for daily record
        for service_name, aggregates in service_aggregates.items():
            await db.usage.upsert_usage_breakdown(
                usage_record_id=uuid.UUID(usage_record.id),
                service_name=service_name,
                cpu_core_seconds=aggregates["cpu_core_seconds"],
                memory_gb_seconds=aggregates["memory_gb_seconds"],
                pod_count=aggregates["pod_count"],
            )

        logger.info(
            f"Collected DAILY for {workspace_id} [{day_start.date()}]: "
            f"CPU={total_cpu:.2f}s, "
            f"Mem={total_memory:.2f}GB-s, "
            f"Storage={total_storage:.2f}GB-h "
            f"(aggregated from {len(hourly_records)} hourly records)"
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
async def collect_hourly_usage(workspace_id: str) -> dict:
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
            "hourly_collected": 0,
            "hourly_failed": 0,
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

    # Backfill only the hours the workspace existed
    hourly_results = []
    for hour in range(start_hour, end_hour + 1):
        start_time = datetime.combine(target_date, datetime.min.time()).replace(
            hour=hour, tzinfo=timezone.utc
        )
        end_time = start_time + timedelta(hours=1)

        result = await collect_workspace_usage_for_hour(
            workspace_id, start_time, end_time
        )
        hourly_results.append(result)

    hourly_collected = sum(1 for r in hourly_results if r["success"])
    hourly_failed = sum(1 for r in hourly_results if not r["success"])
    expected_hours = end_hour - start_hour + 1

    # Only aggregate and mark as FINALIZED if we got ALL hourly records
    if hourly_collected == expected_hours:
        daily_result = await collect_workspace_daily_usage(
            workspace_id, day_start, day_end
        )

        if daily_result["success"]:
            logger.info(
                f"Backfill complete for {workspace_id}: "
                f"{hourly_collected}/{expected_hours} hourly records collected, "
                f"daily record FINALIZED"
            )
            return {
                "workspace_id": workspace_id,
                "date": str(target_date),
                "hourly_collected": hourly_collected,
                "hourly_failed": hourly_failed,
                "daily_success": True,
                "daily_record_id": daily_result["usage_record_id"],
                "status": "finalized",
            }

    # If we're here, either didn't collect all hours OR daily aggregation failed
    # INCOMPLETE record remains in DB for retry
    logger.warning(
        f"Backfill incomplete for {workspace_id}: "
        f"collected {hourly_collected}/{expected_hours} hourly records, "
        f"daily record remains INCOMPLETE"
    )

    return {
        "workspace_id": workspace_id,
        "date": str(target_date),
        "hourly_collected": hourly_collected,
        "hourly_failed": hourly_failed,
        "daily_success": False,
        "status": "incomplete",
    }


@flow(log_prints=True)
async def forward_for_billing():
    """Forward finalized usage data to billing service"""
    finalized_usage = await db.usage.get_finalized_usage()

    if not finalized_usage:
        logger.info("No finalized usage records to forward")
        return {"forwarded": 0, "failed": 0}

    forwarded = 0
    failed = 0

    for usage in finalized_usage:
        try:
            logger.info(
                f"Forwarding usage to billing: workspace={usage.workspace_id}, "
                f"period={usage.collection_start} to {usage.collection_end}, "
                f"CPU={usage.cpu_core_seconds:.0f}s, "
                f"Memory={usage.memory_gb_seconds:.0f}GB-s"
            )

            should_mark_reported = False

            # Get workspace owner's polar_id
            polar_customer_id = await polar_service.get_workspace_owner_polar_id(
                str(usage.workspace_id)
            )

            if not polar_service.enabled:
                # Polar disabled - mark as reported (graceful degradation)
                logger.debug(
                    f"Polar disabled, marking usage {usage.id} as reported without sending"
                )
                should_mark_reported = True

            elif not polar_customer_id:
                # No polar_id - mark as reported (user not configured yet)
                logger.warning(
                    f"Skipping Polar billing for workspace {usage.workspace_id}: "
                    f"owner has no polar_id set. Marking as reported."
                )
                should_mark_reported = True

            else:
                # Send usage data to Polar
                results = await polar_service.send_workspace_usage(
                    usage, polar_customer_id
                )

                # Only mark as reported if ALL events succeeded
                if all(results.values()):
                    should_mark_reported = True
                    logger.info(f"All events sent successfully for usage {usage.id}")
                else:
                    logger.error(
                        f"Failed to send all events for usage {usage.id}: {results}. "
                        f"Will retry on next run."
                    )

            # Mark as reported only if delivery confirmed or gracefully skipped
            if should_mark_reported:
                await db.usage.mark_as_reported(uuid.UUID(usage.id))
                forwarded += 1
                logger.info(f"Marked usage as reported: {usage.id}")
            else:
                failed += 1

        except Exception as e:
            failed += 1
            logger.error(f"Failed to forward usage {usage.id}: {e}")

    logger.info(f"Billing forward complete: {forwarded} forwarded, {failed} failed")
    return {"forwarded": forwarded, "failed": failed}


@flow(log_prints=True)
async def spawn_usage_collection():
    """Spawn usage collection flows for active workspaces"""
    # Get only active workspaces (skip deleted ones)
    active_workspaces = await db.workspaces.aget_active_workspaces()

    logger.info(
        f"Collecting hourly usage for {len(active_workspaces)} active workspaces"
    )

    # Spawn collect_hourly_usage for all active workspaces
    results = []
    for workspace in active_workspaces:
        result = await collect_hourly_usage(str(workspace.id))
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
            workspace_id=uuid.UUID(workspace.id),
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
                workspace_id=uuid.UUID(workspace.id),
                collection_start=yesterday_start,
                collection_end=yesterday_end,
                cpu_core_seconds=0.0,
                memory_gb_seconds=0.0,
                storage_gb_hours=0.0,
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
collect_hourly_usage_deployment = collect_hourly_usage.to_deployment(
    name="collect-hourly-usage",
)

backfill_daily_usage_deployment = backfill_daily_usage.to_deployment(
    name="backfill-daily-usage",
)

# define deployments for the flows that can be called by other flows

spawn_usage_collection_deployment = spawn_usage_collection.to_deployment(
    name="spawn-usage-collection",
    cron="15 * * * *",  # Every hour at 15 minutes past the hour
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
