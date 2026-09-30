from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from math import ceil

from billing.meter_outbox import MeterEventDrainResult
from billing.plan_changes import PlanChangeSettleResult
from billing.reconciliation import BillingReconciliationResult
from identity.auth import AuthService
from identity.device_auth import DeviceAuthorizationService
from observability.usage import WorkerEventService
from scheduler.reconciliation import SchedulerRunResult
from shared.timestamps import utc_now
from storage.retention_settings import RetentionSettings

from scheduler_app.fleet_pass import FleetPass
from scheduler_app.fleet_services import FleetAppServices

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class FleetHousekeeping:
    services: FleetAppServices
    retention_settings: RetentionSettings
    last_reported_abandoned_meter_events: int | None = None

    def run(
        self, *, now: datetime | None = None, include_containers: bool = True, limit: int = 100
    ) -> SchedulerRunResult:
        result = SchedulerRunResult()
        run = FleetPass(self.services.redis_client)
        run.run(
            "housekeeping",
            lambda: self.reconcile(run, result, now or utc_now(), include_containers, limit),
            None,
            interval=30,
        )
        run.finish()
        return result

    def reconcile(
        self,
        run: FleetPass,
        result: SchedulerRunResult,
        now: datetime,
        include_containers: bool,
        limit: int,
    ) -> None:
        services = self.services
        run.run(
            "deployment_cleanup",
            lambda: services.deployment_plans.reconcile_pending(limit=limit),
            None,
        )
        run.run(
            "deployment_effects",
            lambda: services.deployment_effects.reconcile_pending(limit=limit),
            None,
        )
        if services.storage_access is not None:
            result.storage_access_observed = run.run(
                "storage_access", services.storage_access.reconcile, None
            )
            result.storage_access_failures = int("storage_access" in run.failures)
        run.run(
            "volume_deletion",
            lambda: services.volume_deletion.reconcile_due(now=now, limit=limit),
            None,
        )
        run.run(
            "disk_deletion",
            lambda: services.disk_deletion.reconcile_due(now=now, limit=limit),
            None,
        )
        run.run(
            "disk_volumes", lambda: services.disk_volumes.reconcile_due(now=now, limit=limit), None
        )
        metering = run.run(
            "volume_metering",
            lambda: services.volume_metering.reconcile_due(now=now, limit=limit),
            None,
        )
        result.volume_metering_count = metering.metered_count if metering is not None else 0
        result.volume_metering_failure_count = metering.failure_count if metering is not None else 1
        self.drain_meter_events(run, result, now)
        run.run("payments", lambda: services.billing_payments.maintain(now=now), None)
        plans = run.run(
            "plan_changes",
            lambda: services.plan_changes.settle_open(now=now),
            PlanChangeSettleResult(),
        )
        result.plan_changes_applied_count = plans.applied_count
        result.plan_changes_not_applied_count = plans.not_applied_count
        result.plan_changes_retried_count = plans.retried_count
        result.plan_changes_abandoned_count = plans.abandoned_count
        result.plan_changes_open_count = plans.open_count
        if plans.applied_count or plans.abandoned_count or plans.open_count:
            LOGGER.info(
                "plan changes applied=%d not_applied=%d retried=%d abandoned=%d open=%d",
                plans.applied_count,
                plans.not_applied_count,
                plans.retried_count,
                plans.abandoned_count,
                plans.open_count,
            )
        billing = run.run(
            "billing_reconciliation",
            lambda: services.billing_reconciliation.reconcile(now=now),
            BillingReconciliationResult(),
            interval=3600,
        )
        result.billing_reconcile_checked_count = billing.accounts_checked
        result.billing_reconcile_divergent_count = billing.divergent_count
        result.billing_reconcile_failure_count = billing.unreachable_count
        run.run("email", lambda: self.deliver_email(run, now), None)
        run.run(
            "custom_domains", lambda: services.custom_domains.reconcile_due(now=now), 0, interval=60
        )
        if include_containers:
            result.expired_tokens_pruned = run.run(
                "expired_tokens", lambda: self.prune_tokens(now), 0, interval=3600
            )
            result.events_pruned = run.run("events", self.prune_events, 0, interval=3600)
            result.objects_removed = run.run("retention", lambda: self.retain_artifacts(now), 0)
            result.retention_failure_count = int("retention" in run.failures)

    def drain_meter_events(self, run: FleetPass, result: SchedulerRunResult, now: datetime) -> None:
        outbox = self.services.meter_outbox
        drained = run.run("meter_events", lambda: outbox.drain(now=now), MeterEventDrainResult())
        result.meter_events_sent_count = drained.sent_count
        result.meter_events_retried_count = drained.retried_count
        result.meter_events_abandoned_count = drained.abandoned_count
        # Read the outstanding charges even when delivery failed.
        backlog = run.run("meter_backlog", outbox.abandoned_backlog, None)
        if backlog is None:
            self.last_reported_abandoned_meter_events = None
            return
        result.meter_events_abandoned_outstanding_count = backlog.count
        result.meter_events_abandoned_outstanding_nanos = backlog.value_nanos
        moved = (drained.sent_count, drained.retried_count, drained.abandoned_count)
        if any(moved) or backlog.count != self.last_reported_abandoned_meter_events:
            LOGGER.log(
                logging.WARNING if backlog.count else logging.INFO,
                "meter events sent=%d retried=%d abandoned=%d; "
                "%d rows metering %d nanodollars never reached the provider",
                *moved,
                backlog.count,
                backlog.value_nanos,
            )
            self.last_reported_abandoned_meter_events = backlog.count

    def deliver_email(self, run: FleetPass, now: datetime) -> None:
        outbox = self.services.email_outbox
        drained = outbox.drain(now=now)
        if drained.abandoned_count:
            LOGGER.error("abandoned %d email messages this sweep", drained.abandoned_count)
        backlog = outbox.abandoned_backlog()
        if backlog:
            LOGGER.error("%d email messages have been given up on", backlog)
        run.run("email_redaction", lambda: outbox.redact(now=now), 0, interval=3600)

    def prune_tokens(self, now: datetime) -> int:
        context = self.services.context
        return AuthService(context).prune_expired_system_tokens(
            now=now
        ) + DeviceAuthorizationService(context).prune_expired(now=now)

    def prune_events(self) -> int:
        return self.services.events.prune() + WorkerEventService(self.services.context).prune()

    def retain_artifacts(self, now: datetime) -> int:
        retention = self.services.retention
        if retention is None:
            return 0
        redis = self.services.redis_client
        due = redis.key("fleet", "retention", "due")
        failures = redis.key("fleet", "retention", "failures")
        settings = self.retention_settings
        if not redis.set(due, "1", nx=True, ex=max(1, ceil(settings.retry_initial_seconds))):
            return 0
        try:
            result = retention.reconcile(now=now)
        except Exception:
            attempts = redis.increment(failures)
            delay = min(
                settings.retry_max_seconds,
                settings.retry_initial_seconds * (1 << min(attempts - 1, 30)),
            )
            redis.set(due, "1", ex=max(1, ceil(delay)))
            raise
        redis.delete(failures)
        redis.set(due, "1", ex=max(1, ceil(settings.interval_seconds)))
        return result.removed
