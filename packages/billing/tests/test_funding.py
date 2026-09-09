from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from api.server.services import ApiServices
from billing.funding import BillingFundingService
from billing.rate_publication import publish_metered_rate_history
from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_allowance import BillingAllowanceRepository
from database.repositories.billing_credits import BillingCreditRepository
from database.repositories.billing_funding import BillingFundingRepository
from database.repositories.billing_ledger import ContainerBillingShapeRepository
from database.repositories.compute import (
    ComputeProviderInstanceRecord,
    ComputeProviderInstanceRepository,
)
from database.repositories.orchestration import ContainerRepository, MachineRepository
from database.tables.billing_credits import BillingCreditSettlementTable
from database.tables.billing_funding import BillingFundingHoldTable
from database.tables.billing_outbox import BillingMeterOutboxTable
from shared.billing_credits import CreditGrant, CreditKind, CreditScope
from shared.billing_quotes import BilledDimension, ContainerShape
from shared.billing_rate_card import METERED_RATES_EFFECTIVE_AT
from shared.compute_fleet import Machine
from shared.container_requests import container_cpu_ceiling_millicores, container_memory_ceiling_mib
from shared.containers import ContainerRecord
from shared.errors import ConflictError, PaymentRequiredError
from shared.timestamps import utc_now
from shared.usage import (
    METERING_WINDOW_ENDED_AT_METADATA_KEY,
    METERING_WINDOW_STARTED_AT_METADATA_KEY,
    UsageBillingOwner,
    UsageMetric,
    UsageRecord,
    UsageUnit,
)
from sqlalchemy import select
from tests.service_fixtures import workspace_owner_user_id


def test_destroyed_worker_releases_unknown_hold_and_waives_late_usage(
    postgres_services: ApiServices,
) -> None:
    at = utc_now()
    container_id, machine_id, instance_id = (str(uuid4()) for _ in range(3))
    with postgres_services.context.database.session() as session:
        workspace_id = postgres_services.context.default_workspace_id(session)
        user_id = workspace_owner_user_id(postgres_services.context, workspace_id)
        publish_metered_rate_history(session, effective_at=METERED_RATES_EFFECTIVE_AT)
        credits = BillingCreditRepository(session)
        credits.prepare_cutover(user_id=user_id, effective_at=at)
        credits.complete_cutover(user_id=user_id, at=at)
        credits.issue(
            user_id=user_id,
            grant=CreditGrant(
                "payment:lost-worker",
                CreditKind.Purchased,
                CreditScope.AllMetered,
                1_000_000_000,
                at,
            ),
        )
        allowance = BillingAllowanceRepository(session)
        period = allowance.current_period(user_id=user_id, at=at)
        assert period is not None
        allowance.confirm_credit(user_id=user_id, period_started_at=period.started_at, at=at)
        shape = ContainerShape(UsageBillingOwner.PlatformFleet, "", 1000, 1024, 0)
        funding = BillingFundingService(session)
        funding.reserve_pending(
            container_id=container_id, workspace_id=workspace_id, candidate_shapes=(shape,), now=at
        )
        MachineRepository(session).upsert(Machine(id=machine_id), workspace_id=workspace_id)
        ContainerRepository(session).upsert(
            ContainerRecord(
                id=container_id,
                name="lost-worker",
                image="image",
                command=["true"],
                workspace_id=workspace_id,
                runtime_worker_id="worker-lost",
                runtime_machine_id=machine_id,
            )
        )
        ContainerBillingShapeRepository(session).record(
            container_id=container_id, workspace_id=workspace_id, shape=shape
        )
        funding.authorize(container_id=container_id, worker_id="worker-lost", shape=shape, now=at)
        instance = ComputeProviderInstanceRecord(
            id=instance_id,
            provider="aws",
            offer_id="offer",
            status="deleted",
            source="pooled",
            machine_id=machine_id,
            instance_id="i-lost",
        )
        ComputeProviderInstanceRepository(session).upsert(instance)
        sibling_id = str(uuid4())
        MachineRepository(session).upsert(Machine(id=sibling_id), workspace_id=workspace_id)
        session.commit()
        with pytest.raises(ConflictError, match="machine evidence is retained"):
            postgres_services.compute.delete_machine(machine_id, workspace=workspace_id)
        postgres_services.compute.delete_machine(sibling_id, workspace=workspace_id)
        assert MachineRepository(session).get(sibling_id, workspace_id=workspace_id) is None
        assert (
            funding.reconcile_destroyed_workers(user_id=user_id, now=at + timedelta(seconds=110))
            == 0
        )
        known = UsageRecord(
            id=str(uuid4()),
            workspace_id=workspace_id,
            resource_type="container",
            resource_id=container_id,
            metric=UsageMetric.ContainerDurationMilliseconds,
            quantity=2000,
            unit=UsageUnit.Milliseconds,
            labels={"cpu_millicores": "1000", "mem_mb": "1024", "gpu_count": "0"},
            metadata={
                METERING_WINDOW_STARTED_AT_METADATA_KEY: at.isoformat(),
                METERING_WINDOW_ENDED_AT_METADATA_KEY: (at + timedelta(seconds=2)).isoformat(),
            },
        )
        postgres_services.usage.append_in_session(session, known)
        paid = session.get(BillingCreditSettlementTable, known.id)
        assert paid is not None and paid.credited_nanos is not None and paid.credited_nanos > 0
        original_credit = paid.credited_nanos
        evidence_at = at + timedelta(seconds=95)
        ComputeProviderInstanceRepository(session).upsert(
            instance.model_copy(
                update={"metadata": {"provider_storage_destroyed_at": evidence_at.isoformat()}}
            )
        )
        account = BillingAccountRepository(session).get_by_user(user_id)
        assert account is not None
        BillingAccountRepository(session).upsert(
            user_id=user_id,
            status=account.status,
            provider_customer_id=account.provider_customer_id,
            provider_subscription_id="",
            provider_credit_grant_id="",
            plan=None,
        )
        assert BillingFundingRepository(session).recovery_accounts(
            after_user_id=None, expired_before=at + timedelta(seconds=100), limit=20
        ) == (user_id,)
        assert funding.reconcile_destroyed_workers(user_id=user_id, now=evidence_at) == 0
        assert (
            funding.reconcile_destroyed_workers(user_id=user_id, now=at + timedelta(seconds=110))
            == 1
        )
        hold = session.get(BillingFundingHoldTable, container_id)
        assert (
            hold is not None
            and hold.loss_machine_id == machine_id
            and hold.loss_provider_instance_id == instance_id
        )
        assert hold.loss_exposure_nanos is not None and hold.loss_exposure_nanos > 0
        BillingFundingRepository(session).require_machine_evidence_released((machine_id,))
        assert (
            funding.balance(
                user_id=user_id,
                dimension=BilledDimension.ComputeRuntime,
                at=at + timedelta(seconds=110),
            ).held_nanos
            == 0
        )
        assert (
            funding.reconcile_destroyed_workers(user_id=user_id, now=at + timedelta(seconds=120))
            == 0
        )
        late = known.model_copy(
            update={
                "id": str(uuid4()),
                "metadata": {
                    METERING_WINDOW_STARTED_AT_METADATA_KEY: (
                        at + timedelta(seconds=2)
                    ).isoformat(),
                    METERING_WINDOW_ENDED_AT_METADATA_KEY: (at + timedelta(seconds=4)).isoformat(),
                },
            }
        )
        postgres_services.usage.append_in_session(session, late)
        waived = session.get(BillingCreditSettlementTable, late.id)
        assert (
            waived is not None
            and waived.gross_nanos > 0
            and waived.credited_nanos == 0
            and waived.payable_nanos == waived.gross_nanos
        )
        event = session.scalar(
            select(BillingMeterOutboxTable).where(
                BillingMeterOutboxTable.usage_record_id == late.id
            )
        )
        assert event is not None and event.status == "waived"
        original = session.get(BillingCreditSettlementTable, known.id)
        assert original is not None and original.credited_nanos == original_credit


def test_two_concurrent_starts_cannot_reserve_the_same_available_credit(
    postgres_services: ApiServices,
) -> None:
    at = utc_now()
    with postgres_services.context.database.session() as session:
        workspace_id = postgres_services.context.default_workspace_id(session)
        user_id = workspace_owner_user_id(postgres_services.context, workspace_id)
        publish_metered_rate_history(session, effective_at=METERED_RATES_EFFECTIVE_AT)
        credits = BillingCreditRepository(session)
        credits.prepare_cutover(user_id=user_id, effective_at=at)
        credits.complete_cutover(user_id=user_id, at=at)
        lot_id = credits.issue(
            user_id=user_id,
            grant=CreditGrant(
                "payment:concurrency",
                CreditKind.Purchased,
                CreditScope.AllMetered,
                1_000_000_000,
                at,
            ),
        )
        shape = ContainerShape(
            UsageBillingOwner.PlatformFleet,
            "",
            container_cpu_ceiling_millicores(1000),
            container_memory_ceiling_mib(1024),
            0,
        )
        funding = BillingFundingService(session)
        sizing_id = str(uuid4())
        funding.reserve_pending(
            container_id=sizing_id, workspace_id=workspace_id, candidate_shapes=(shape,), now=at
        )
        needed = funding.balance(
            user_id=user_id, dimension=BilledDimension.ComputeRuntime, at=at
        ).held_nanos
        assert needed > 0
        assert funding.cancel_pending(container_id=sizing_id, now=at)
        credits.adjust(
            user_id=user_id,
            credit_lot_id=lot_id,
            source_id="refund:unused",
            amount_nanos=needed - 1_000_000_000,
            effective_at=at,
        )
    barrier = Barrier(2)

    def start() -> bool:
        barrier.wait(timeout=5)
        try:
            with postgres_services.context.database.session() as session:
                BillingFundingService(session).reserve_pending(
                    container_id=str(uuid4()),
                    workspace_id=workspace_id,
                    candidate_shapes=(shape,),
                    now=at,
                )
            return True
        except PaymentRequiredError:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(start) for _ in range(2)]
        assert sorted(future.result(timeout=15) for future in futures) == [False, True]
    with postgres_services.context.database.session() as session:
        balance = BillingFundingService(session).balance(
            user_id=user_id,
            dimension=BilledDimension.ComputeRuntime,
            at=at,
        )
        assert balance.held_nanos == needed
        assert balance.available_nanos == 0


def test_burst_usage_settles_its_hold_and_refunded_spent_credit_blocks_new_work(
    postgres_services: ApiServices,
) -> None:
    at = utc_now()
    end = at + timedelta(seconds=10)
    container_id = str(uuid4())
    worker_id = "funding-worker"
    with postgres_services.context.database.session() as session:
        workspace_id = postgres_services.context.default_workspace_id(session)
        user_id = workspace_owner_user_id(postgres_services.context, workspace_id)
        publish_metered_rate_history(session, effective_at=METERED_RATES_EFFECTIVE_AT)
        credits = BillingCreditRepository(session)
        credits.prepare_cutover(user_id=user_id, effective_at=at)
        credits.complete_cutover(user_id=user_id, at=at)
        lot_id = credits.issue(
            user_id=user_id,
            grant=CreditGrant(
                "payment:runtime",
                CreditKind.Purchased,
                CreditScope.AllMetered,
                1_000_000_000,
                at,
            ),
        )
        allowance = BillingAllowanceRepository(session)
        period = allowance.current_period(user_id=user_id, at=at)
        assert period is not None
        allowance.confirm_credit(user_id=user_id, period_started_at=period.started_at, at=at)
        actual = ContainerShape(UsageBillingOwner.PlatformFleet, "", 1000, 1024, 0)
        ceiling = ContainerShape(
            UsageBillingOwner.PlatformFleet,
            "",
            container_cpu_ceiling_millicores(1000),
            container_memory_ceiling_mib(1024),
            0,
        )
        funding = BillingFundingService(session)
        funding.reserve_pending(
            container_id=container_id,
            workspace_id=workspace_id,
            candidate_shapes=(ceiling,),
            now=at,
        )
        ContainerRepository(session).records.upsert(
            ContainerRecord(
                id=container_id,
                name="funded",
                image="image",
                command=["true"],
                workspace_id=workspace_id,
            ),
            workspace_id=workspace_id,
        )
        ContainerBillingShapeRepository(session).record(
            container_id=container_id,
            workspace_id=workspace_id,
            shape=actual,
        )
        permit = funding.authorize(
            container_id=container_id, worker_id=worker_id, shape=actual, now=at
        )
        assert (
            funding.authorize(container_id=container_id, worker_id=worker_id, shape=actual, now=at)
            == permit
        )
        assert not funding.cancel_pending(container_id=container_id, now=at)
        with pytest.raises(ConflictError, match="different worker"):
            funding.renew(container_id=container_id, worker_id="other-worker", now=at)
        with pytest.raises(PaymentRequiredError, match="expired"):
            funding.renew(
                container_id=container_id,
                worker_id=worker_id,
                now=permit.valid_until + timedelta(seconds=1),
            )
    records = [
        UsageRecord(
            id=str(uuid4()),
            workspace_id=workspace_id,
            resource_type="container",
            resource_id=container_id,
            metric=metric,
            quantity=quantity,
            unit=unit,
            labels={"cpu_millicores": "1000", "mem_mb": "1024", "gpu_count": "0"},
            metadata={
                METERING_WINDOW_STARTED_AT_METADATA_KEY: at.isoformat(),
                METERING_WINDOW_ENDED_AT_METADATA_KEY: end.isoformat(),
            },
        )
        for metric, quantity, unit in (
            (UsageMetric.ContainerDurationMilliseconds, 10000, UsageUnit.Milliseconds),
            (UsageMetric.CpuUsedCoreSeconds, ceiling.cpu_millicores / 1000 * 10, UsageUnit.Seconds),
            (
                UsageMetric.MemoryRssByteSeconds,
                ceiling.memory_mib * 1024**2 * 10,
                UsageUnit.ByteSeconds,
            ),
        )
    ]
    with postgres_services.context.database.session() as session:
        postgres_services.usage.append_in_session(session, records[0])
        hold = BillingFundingRepository(session).get(container_id)
        assert hold is not None and hold.metered_through is None
        BillingCreditRepository(session).adjust(
            user_id=user_id,
            credit_lot_id=lot_id,
            source_id="refund:full",
            amount_nanos=-1_000_000_000,
            effective_at=at,
        )
    with postgres_services.context.database.session() as session:
        later_end = end + timedelta(seconds=10)
        later_records = [
            record.model_copy(
                update={
                    "id": str(uuid4()),
                    "metadata": {
                        METERING_WINDOW_STARTED_AT_METADATA_KEY: end.isoformat(),
                        METERING_WINDOW_ENDED_AT_METADATA_KEY: later_end.isoformat(),
                    },
                }
            )
            for record in records
        ]
        for record in later_records:
            postgres_services.usage.append_in_session(session, record)
        funding = BillingFundingService(session)
        funding.record_window(
            container_id=container_id,
            worker_id=worker_id,
            started_at=end,
            ended_at=later_end,
            usage_record_ids=[record.id for record in later_records],
        )
        funding.observe_terminal(
            container_id=container_id, worker_id=worker_id, exited_at=later_end
        )
        hold = BillingFundingRepository(session).get(container_id)
        assert hold is not None and hold.metered_through == at
        assert (
            funding.balance(
                user_id=user_id, dimension=BilledDimension.ComputeRuntime, at=at
            ).held_nanos
            > 0
        )
    with postgres_services.context.database.session() as session:
        for record in records[1:]:
            postgres_services.usage.append_in_session(session, record)
        funding = BillingFundingService(session)
        funding.record_window(
            container_id=container_id,
            worker_id=worker_id,
            started_at=at,
            ended_at=end,
            usage_record_ids=[record.id for record in records],
        )
        balance = funding.balance(user_id=user_id, dimension=BilledDimension.ComputeRuntime, at=at)
        assert balance.held_nanos == 0
        assert balance.credits.purchased_nanos < 0
        spent = -balance.credits.purchased_nanos
        budget = funding.usage_budget(user_id=user_id, at=at)
        assert budget.spent_nanos == spent and budget.held_nanos == 0
        credits = BillingCreditRepository(session)
        credits.issue(
            user_id=user_id,
            grant=CreditGrant(
                "trial:debt",
                CreditKind.Trial,
                CreditScope.Compute,
                5_000_000_000,
                at,
                at + timedelta(days=1),
            ),
        )
        balance = funding.balance(user_id=user_id, dimension=BilledDimension.ComputeRuntime, at=at)
        assert balance.credits.purchased_nanos == -spent
        assert balance.debt_nanos == spent and balance.available_nanos == 0
    with (
        pytest.raises(PaymentRequiredError, match="refund or dispute"),
        postgres_services.context.database.session() as session,
    ):
        BillingFundingService(session).reserve_pending(
            container_id=str(uuid4()),
            workspace_id=workspace_id,
            candidate_shapes=(ceiling,),
            now=at,
        )
