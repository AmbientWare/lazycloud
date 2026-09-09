from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, Decimal

from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_credits import BillingCreditRepository
from database.repositories.billing_funding import BillingFundingRepository, FundingHold
from database.repositories.billing_ledger import BillingLedgerRepository
from database.repositories.billing_preferences import BillingPreferencesRepository
from database.repositories.billing_rates import ComputeRateRepository
from database.repositories.identity import WorkspaceMemberRepository
from database.repositories.orchestration import ContainerRepository
from database.tables.billing_credits import BillingCreditLotTable
from database.tables.billing_funding import BillingFundingAllocationTable
from database.tables.billing_ledger import BillingLedgerSegmentTable
from database.tables.observability import UsageRecordTable
from shared.billing_accounts import BillingAccount, BillingAccountStatus
from shared.billing_credits import CreditScope
from shared.billing_preferences import UsageBudget, usage_budget_month
from shared.billing_quotes import (
    BILLED_METRICS,
    BilledDimension,
    ContainerShape,
    LedgerBasis,
    LedgerComponent,
    MeteredSpan,
    UnpricedSpan,
    elapsed_seconds,
    price_span,
    reserved_quantity,
)
from shared.containers import TERMINAL_CONTAINER_STATUSES
from shared.errors import (
    ConflictError,
    NotFoundError,
    PaymentRequiredError,
    UpstreamUnavailableError,
)
from shared.funding import (
    FUNDING_PERMIT_SECONDS,
    FUNDING_SHUTDOWN_GRACE_SECONDS,
    FundingBalance,
    FundingPermit,
)
from shared.http.billing_preferences import BillingPreferences
from shared.timestamps import to_utc, utc_now
from shared.usage import UsageMetric, UsageRecord
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

_COMPUTE_COMPONENTS = (
    LedgerComponent.ContainerTime,
    LedgerComponent.Cpu,
    LedgerComponent.Memory,
    LedgerComponent.Gpu,
)


@dataclass(frozen=True, slots=True)
class BillingFundingService:
    session: Session

    def get_preferences(self, *, user_id: str) -> BillingPreferences:
        return BillingPreferencesRepository(self.session).get(user_id)

    def set_preferences(
        self, *, user_id: str, preferences: BillingPreferences, now: datetime | None = None
    ) -> BillingPreferences:
        return BillingPreferencesRepository(self.session).set(user_id, preferences)

    def usage_budget(self, *, user_id: str, at: datetime | None = None) -> UsageBudget:
        moment = to_utc(at or utc_now())
        start, end = usage_budget_month(moment)
        repository = BillingPreferencesRepository(self.session)
        limit = repository.get(user_id).monthly_usage_limit_nanos
        spent = repository.gross_usage(user_id=user_id, start=start, end=end)
        held = sum(
            self._budget_exposure(hold, start=start, end=end, now=moment)
            for hold in BillingFundingRepository(self.session).for_account(user_id)
        )
        return UsageBudget(
            start,
            end,
            limit,
            spent,
            held,
            None if limit is None else max(0, limit - spent - held),
        )

    def balance(
        self,
        *,
        user_id: str,
        dimension: BilledDimension,
        at: datetime | None = None,
    ) -> FundingBalance:
        moment = to_utc(at or utc_now())
        credits = BillingCreditRepository(self.session)
        balance = credits.balance(user_id=user_id, at=moment, dimension=dimension)
        debt = credits.debt_nanos(user_id=user_id, at=moment)
        held_rows = self.session.execute(
            select(
                BillingCreditLotTable.scope,
                func.sum(BillingFundingAllocationTable.amount_nanos),
            )
            .join(
                BillingFundingAllocationTable,
                BillingFundingAllocationTable.credit_lot_id == BillingCreditLotTable.id,
            )
            .where(
                BillingCreditLotTable.user_id == user_id,
                BillingCreditLotTable.effective_at <= moment,
                or_(
                    BillingCreditLotTable.expires_at.is_(None),
                    BillingCreditLotTable.expires_at > moment,
                ),
            )
            .group_by(BillingCreditLotTable.scope)
        ).all()
        held = sum(
            int(amount) for scope, amount in held_rows if CreditScope(scope).covers(dimension)
        )
        available = (
            sum(
                lot.amount_nanos
                for lot in credits.spendable_lots(
                    user_id=user_id,
                    at=moment,
                    dimension=dimension,
                )
            )
            if debt == 0
            else 0
        )
        return FundingBalance(balance, held, debt, available)

    def reserve_pending(
        self,
        *,
        container_id: str,
        workspace_id: str,
        candidate_shapes: Sequence[ContainerShape],
        now: datetime | None = None,
    ) -> None:
        moment = to_utc(now or utc_now())
        if not candidate_shapes:
            raise ConflictError("a container requires a priced funding shape")
        owner = WorkspaceMemberRepository(self.session).owner(workspace_id)
        if owner is None:
            raise PaymentRequiredError("billed work requires a workspace billing owner")
        account = self._account(owner.user_id)
        repository = BillingFundingRepository(self.session)
        existing = repository.get(container_id)
        if existing is not None:
            if existing.workspace_id != workspace_id or existing.user_id != owner.user_id:
                raise ConflictError("the funded container belongs to another billing account")
            if existing.cancelled_at is not None or existing.terminal_at is not None:
                raise ConflictError("a completed funded container cannot be started again")
            if not any(existing.shape == candidate for candidate in candidate_shapes):
                raise ConflictError("the funded container request cannot change on retry")
            return
        until = moment + timedelta(seconds=FUNDING_PERMIT_SECONDS + FUNDING_SHUTDOWN_GRACE_SECONDS)
        priced = [(self._exposure(shape, moment, until), shape) for shape in candidate_shapes]
        cost, shape = max(priced, key=lambda item: item[0])
        self._check_usage_budget(
            user_id=owner.user_id,
            container_id=container_id,
            shape=shape,
            now=moment,
            until=until,
        )
        repository.create(
            container_id=container_id,
            workspace_id=workspace_id,
            user_id=owner.user_id,
            shape=shape,
        )
        if account.complimentary_since is None:
            repository.reserve_credit(
                container_id=container_id,
                amount_nanos=cost,
                at=moment,
                eligible_until=until,
            )

    def authorize(
        self,
        *,
        container_id: str,
        worker_id: str,
        shape: ContainerShape,
        now: datetime | None = None,
    ) -> FundingPermit:
        moment = to_utc(now or utc_now())
        repository = BillingFundingRepository(self.session)
        hold = repository.lock(container_id)
        if shape.gpu_count != hold.shape.gpu_count:
            raise ConflictError("GPU allocation differs from the funded container request")
        shape = replace(
            shape, cpu_millicores=hold.shape.cpu_millicores, memory_mib=hold.shape.memory_mib
        )
        self._active_container(hold)
        account = self._account(hold.user_id)
        if hold.authorized_at is not None:
            self._worker(hold, worker_id)
            if hold.shape != shape:
                raise ConflictError("the funded billing shape cannot change after dispatch")
            if hold.valid_until is None or hold.valid_until <= moment:
                raise PaymentRequiredError("the container's funded runtime permit expired")
            return hold.permit()
        valid_until = moment + timedelta(seconds=FUNDING_PERMIT_SECONDS)
        funded_until = valid_until + timedelta(seconds=FUNDING_SHUTDOWN_GRACE_SECONDS)
        self._check_usage_budget(
            user_id=hold.user_id,
            container_id=container_id,
            shape=shape,
            now=moment,
            until=funded_until,
            hold=hold,
        )
        if account.complimentary_since is None:
            repository.reserve_credit(
                container_id=container_id,
                amount_nanos=self._exposure(shape, moment, funded_until),
                at=moment,
                eligible_until=funded_until,
            )
        return repository.authorize(
            container_id=container_id,
            worker_id=worker_id,
            shape=shape,
            authorized_at=moment,
            valid_until=valid_until,
        )

    def renew(
        self,
        *,
        container_id: str,
        worker_id: str,
        now: datetime | None = None,
    ) -> FundingPermit:
        moment = to_utc(now or utc_now())
        repository = BillingFundingRepository(self.session)
        hold = repository.lock(container_id)
        self._worker(hold, worker_id)
        self._active_container(hold)
        account = self._account(hold.user_id)
        if hold.valid_until is None or hold.valid_until <= moment:
            raise PaymentRequiredError("the container's funded runtime permit expired")
        valid_until = moment + timedelta(seconds=FUNDING_PERMIT_SECONDS)
        funded_until = valid_until + timedelta(seconds=FUNDING_SHUTDOWN_GRACE_SECONDS)
        self._check_usage_budget(
            user_id=hold.user_id,
            container_id=container_id,
            shape=hold.shape,
            now=moment,
            until=funded_until,
            hold=hold,
        )
        if account.complimentary_since is None:
            repository.reserve_credit(
                container_id=container_id,
                amount_nanos=self._remaining_exposure(hold, funded_until),
                at=moment,
                eligible_until=funded_until,
            )
        return repository.renew(container_id=container_id, valid_until=valid_until)

    def record_window(
        self,
        *,
        container_id: str,
        worker_id: str,
        started_at: datetime,
        ended_at: datetime,
        usage_record_ids: Sequence[str],
    ) -> None:
        repository = BillingFundingRepository(self.session)
        hold = repository.lock(container_id)
        self._worker(hold, worker_id)
        if hold.authorized_at is None or started_at < hold.authorized_at or ended_at <= started_at:
            raise ConflictError("metering does not belong to the authorized runtime interval")
        if hold.terminal_at is not None and to_utc(ended_at) > hold.terminal_at:
            raise ConflictError("metering cannot extend beyond the recorded runtime exit")
        rows = self.session.scalars(
            select(UsageRecordTable).where(
                UsageRecordTable.id.in_(usage_record_ids),
            )
        ).all()
        if len(rows) != len(set(usage_record_ids)):
            raise ConflictError("the complete metering window has missing usage records")
        for row in rows:
            record = UsageRecord.model_validate(row.payload)
            if (
                record.workspace_id != hold.workspace_id
                or record.resource_id != container_id
                or (record.resource_type != "container")
            ):
                raise ConflictError("the metering window contains another container's usage")
            billed = BILLED_METRICS.get(record.metric)
            if billed is None or billed.dimension is not BilledDimension.ComputeRuntime:
                continue
            priced = self.session.scalars(
                select(BillingLedgerSegmentTable).where(
                    BillingLedgerSegmentTable.usage_record_id == row.id,
                )
            ).all()
            if not priced or any(
                to_utc(segment.span_started_at) != to_utc(started_at)
                or to_utc(segment.span_ended_at) != to_utc(ended_at)
                for segment in priced
            ):
                raise ConflictError("complete compute metering requires its priced ledger interval")
        required = {
            UsageMetric.ContainerDurationMilliseconds,
            UsageMetric.CpuUsedCoreSeconds,
            UsageMetric.MemoryRssByteSeconds,
        }
        if not required.issubset({UsageRecord.model_validate(row.payload).metric for row in rows}):
            raise ConflictError(
                "complete compute metering requires duration, CPU and memory evidence"
            )
        repository.record_window(
            container_id=container_id,
            started_at=started_at,
            ended_at=ended_at,
            usage_record_ids=usage_record_ids,
        )
        self._release_known_excess(repository.lock(container_id))

    def observe_terminal(
        self,
        *,
        container_id: str,
        worker_id: str,
        exited_at: datetime,
    ) -> None:
        repository = BillingFundingRepository(self.session)
        if repository.get(container_id) is None:
            return
        hold = repository.lock(container_id)
        self._worker(hold, worker_id)
        if hold.authorized_at is None or to_utc(exited_at) < hold.authorized_at:
            raise ConflictError("runtime exit predates its funded authorization")
        if hold.metered_through is not None and to_utc(exited_at) < hold.metered_through:
            raise ConflictError("runtime exit predates its completed metering")
        repository.observe_terminal(container_id=container_id, at=exited_at)
        self._release_known_excess(repository.lock(container_id))

    def cancel_pending(self, *, container_id: str, now: datetime | None = None) -> bool:
        repository = BillingFundingRepository(self.session)
        if repository.get(container_id) is None:
            return False
        return repository.cancel_pending(container_id=container_id, at=to_utc(now or utc_now()))

    def reconcile_destroyed_workers(
        self, *, user_id: str, now: datetime | None = None, limit: int = 100
    ) -> int:
        moment = to_utc(now or utc_now())
        repository = BillingFundingRepository(self.session)
        resolved = 0
        for candidate in repository.destroyed_worker_candidates(
            user_id=user_id,
            expired_before=moment - timedelta(seconds=FUNDING_SHUTDOWN_GRACE_SECONDS),
            limit=limit,
        ):
            hold = repository.lock(candidate.container_id)
            if hold.valid_until is None or hold.loss_resolved_at is not None:
                continue
            funded_until = hold.valid_until + timedelta(seconds=FUNDING_SHUTDOWN_GRACE_SECONDS)
            if moment < funded_until:
                continue
            evidence = repository.destroyed_worker_evidence(hold)
            if (
                evidence is None
                or evidence.observed_at > moment
                or hold.authorized_at is None
                or evidence.observed_at < hold.authorized_at
            ):
                continue
            ledger = BillingLedgerRepository(self.session)
            for record in repository.unsettled_compute_records(hold.container_id):
                ledger.price_record(record)
            ledger.settle_pending_credits(owner_user_id=user_id)
            if repository.unsettled_compute_records(hold.container_id):
                continue
            end = min(hold.terminal_at or funded_until, funded_until)
            exposure = self._remaining_exposure(hold, end)
            repository.resolve_destroyed_worker(
                container_id=hold.container_id,
                evidence=evidence,
                at=moment,
                exposure_nanos=exposure,
            )
            resolved += 1
        return resolved

    def _release_known_excess(self, hold: FundingHold) -> None:
        if hold.valid_until is None:
            return
        end = hold.terminal_at or hold.valid_until + timedelta(
            seconds=FUNDING_SHUTDOWN_GRACE_SECONDS
        )
        BillingFundingRepository(self.session).release_excess(
            container_id=hold.container_id,
            required_nanos=self._remaining_exposure(hold, end),
        )

    def _check_usage_budget(
        self,
        *,
        user_id: str,
        container_id: str,
        shape: ContainerShape,
        now: datetime,
        until: datetime,
        hold: FundingHold | None = None,
    ) -> None:
        repository = BillingPreferencesRepository(self.session)
        limit = repository.get(user_id).monthly_usage_limit_nanos
        if limit is None:
            return
        other_holds = tuple(
            item
            for item in BillingFundingRepository(self.session).for_account(user_id)
            if item.container_id != container_id
        )
        month_start, month_end = usage_budget_month(now)
        while month_start < until:
            spent = repository.gross_usage(user_id=user_id, start=month_start, end=month_end)
            held = sum(
                self._budget_exposure(item, start=month_start, end=month_end, now=now)
                for item in other_holds
            )
            start = max(
                month_start, (hold.metered_through or hold.authorized_at or now) if hold else now
            )
            end = min(month_end, until)
            own = self._exposure(shape, start, end) if start < end else 0
            if hold is not None and start < end:
                own = max(
                    0,
                    own
                    - repository.gross_usage(
                        user_id=user_id,
                        start=start,
                        end=end,
                        container_id=container_id,
                    ),
                )
            if spent + held + own > limit or limit == 0:
                raise PaymentRequiredError("the saved monthly usage limit cannot fund this runtime")
            month_start, month_end = usage_budget_month(month_end)

    def _budget_exposure(
        self, hold: FundingHold, *, start: datetime, end: datetime, now: datetime
    ) -> int:
        lower = max(start, hold.metered_through or hold.authorized_at or now)
        upper = min(
            end,
            hold.terminal_at
            or (
                hold.valid_until + timedelta(seconds=FUNDING_SHUTDOWN_GRACE_SECONDS)
                if hold.valid_until is not None
                else now
                + timedelta(seconds=FUNDING_PERMIT_SECONDS + FUNDING_SHUTDOWN_GRACE_SECONDS)
            ),
        )
        if lower >= upper:
            return 0
        maximum = self._exposure(hold.shape, lower, upper)
        posted = BillingPreferencesRepository(self.session).gross_usage(
            user_id=hold.user_id,
            start=lower,
            end=upper,
            container_id=hold.container_id,
        )
        return max(0, maximum - posted)

    def _remaining_exposure(self, hold: FundingHold, until: datetime) -> int:
        if hold.loss_resolved_at is not None:
            return 0
        start = hold.metered_through or hold.authorized_at
        if start is None:
            raise ConflictError("the funded container has not been dispatched")
        repository = BillingFundingRepository(self.session)
        maximum = self._exposure(hold.shape, start, until) if start < until else 0
        credited = repository.credited_since(container_id=hold.container_id, since=start)
        return max(0, maximum - credited) + repository.pending_before(
            container_id=hold.container_id,
            before=start,
        )

    def _account(self, user_id: str) -> BillingAccount:
        account = BillingAccountRepository(self.session).get_by_user(user_id, for_update=True)
        if account is None:
            raise PaymentRequiredError("the workspace has no billing account")
        if account.complimentary_since is not None:
            return account
        if account.status is BillingAccountStatus.PastDue:
            raise PaymentRequiredError("the billing account has an unpaid payment")
        if account.plan is None or not account.provider_subscription_id:
            raise PaymentRequiredError("the billing account has no active subscription")
        cutover = BillingCreditRepository(self.session).cutover(user_id=user_id)
        if cutover is None or cutover.completed_at is None:
            raise PaymentRequiredError(
                "credit settlement migration must complete before funded work starts"
            )
        return account

    def _active_container(self, hold: FundingHold) -> None:
        container = ContainerRepository(self.session).get(
            hold.container_id,
            workspace_id=hold.workspace_id,
        )
        if container is None:
            raise NotFoundError("the funded container does not exist")
        if (
            container.status in TERMINAL_CONTAINER_STATUSES
            or hold.cancelled_at is not None
            or (hold.terminal_at is not None)
            or hold.loss_resolved_at is not None
        ):
            raise ConflictError("a terminal container cannot receive another funded runtime permit")

    @staticmethod
    def _worker(hold: FundingHold, worker_id: str) -> None:
        if not worker_id or hold.worker_id != worker_id:
            raise ConflictError("the runtime permit belongs to a different worker")

    def _exposure(self, shape: ContainerShape, start: datetime, end: datetime) -> int:
        quotes = ComputeRateRepository(self.session).quotes_for(
            shape=shape,
            components=_COMPUTE_COMPONENTS,
            started_at=start,
            ended_at=end,
        )
        total = 0
        for component in _COMPUTE_COMPONENTS:
            priced = price_span(
                MeteredSpan(
                    component=component,
                    basis=LedgerBasis.Reserved,
                    started_at=start,
                    ended_at=end,
                    quantity=reserved_quantity(component, shape, elapsed_seconds(start, end)),
                ),
                quotes,
            )
            if isinstance(priced, UnpricedSpan):
                raise UpstreamUnavailableError(
                    "published compute rates do not cover the funding window"
                )
            for segment in priced.segments:
                per_millisecond = segment.quote.rate_nanos_per_unit * reserved_quantity(
                    component,
                    shape,
                    Decimal("0.001"),
                )
                if per_millisecond == 0:
                    continue
                milliseconds = int(
                    (
                        elapsed_seconds(segment.started_at, segment.ended_at) * 1000
                    ).to_integral_value(rounding=ROUND_CEILING)
                )
                # Separate reserved/excess records round independently; a millisecond
                # is the smallest accepted worker window. This reserves their upper bound.
                total += (
                    int(per_millisecond.to_integral_value(rounding=ROUND_CEILING)) + 1
                ) * milliseconds
        return total


__all__ = ["BillingFundingService"]
