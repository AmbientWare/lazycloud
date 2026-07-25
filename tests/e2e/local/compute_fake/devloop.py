"""Fake-provider bootstrap/reclaim dev loop against real PostgreSQL and Redis.

Runs the full provision -> enroll -> ready / fail / reclaim cycle through the
real production owners (``ComputeService``, ``ProviderNodeEnrollmentService``,
``GatewayControlService``) with the in-memory ``provider_fake`` adapter in
place of AWS, so bootstrap/reclaim iteration never needs cloud credentials.

This is a dev-only iteration loop, not an acceptance scenario: it composes
services directly, uses its own disposable database
(``lazycloud_fake_devloop``) on the compose PostgreSQL server plus Redis
logical database 1 under a dedicated key prefix, and never touches the running
stack's ``lazycloud`` database or Redis db 0 state.

Run from the repository root:

    uv run python -m tests.e2e.local.compute_fake.devloop --mode ready
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import sqlalchemy
from agent.artifacts import AgentArtifactSettings
from api.server.services import ApiServices
from compute.agent_control import TailnetConfig
from compute.offers import ComputeOffer
from compute.providers import ProviderPoolBootstrap, ResolvedComputeProvider
from compute.service import ComputeService
from compute.state import RedisComputeStateRepository
from control.service import ControlPlaneService
from coordination.redis_client import RedisClient, RedisSettings
from database.repositories.compute import (
    AwsAccountConnectionRepository,
    ComputePoolRepository,
    ComputeProviderInstanceRecord,
    ComputeProviderInstanceRepository,
    WorkspaceComputePolicyRepository,
)
from gateway.provider_enrollment import ProviderNodeEnrollmentService
from provider_clients import AwsProviderNodeIdentityAdapter
from provider_clients.settings import AwsAccountConnectionSettings, AwsCapacitySettings
from provider_fake import FakePooledCapacityProvider, FakeReplayGuard, FakeStsHttpClient
from redis.exceptions import RedisError
from scheduler.compute_hooks import SchedulerComputeHooks
from shared.aws_connections import (
    AwsAccountAuthorizationGeneration,
    AwsAccountAuthorizationMode,
    AwsAccountAuthorizationPhase,
    AwsAccountConnection,
    AwsAccountConnectionPhase,
)
from shared.compute_enrollment import MachineBootstrapFailureReason, MachineBootstrapPhase
from shared.compute_policy import (
    AwsWorkspaceComputePolicy,
    ComputeCapacityMode,
    ComputePlacementTarget,
    ComputePoolRecord,
    ComputeResourceRequirements,
    WorkspaceComputePolicy,
)
from shared.timestamps import utc_now
from sqlalchemy.exc import OperationalError

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings

from .agent import FakeNodeAgent, FakeNodeAgentAction

SKIP = 77

DEVLOOP_DATABASE = "lazycloud_fake_devloop"
DEFAULT_DATABASE_URL = f"postgresql+psycopg://lazycloud:lazycloud@localhost:5432/{DEVLOOP_DATABASE}"
DEFAULT_REDIS_URL = "redis://localhost:6379/1"
_REDIS_KEY_PREFIX = "lazycloud-fake-devloop"

_ACCOUNT_ID = "123456789012"
_ROLE_NAME = "compute-node"
_REGION = "us-east-1"
_INSTANCE_TYPE = "i4i.xlarge"
_AGENT_ARTIFACT_URL = "https://artifacts.fake-devloop.invalid/lazycloud-agent"
_MAX_ITERATIONS = 10

_TERMINAL_STATUSES = {"deleted", "failed"}


@dataclass(slots=True)
class _ManualClock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


@dataclass(frozen=True, slots=True)
class _FakeResolver:
    ref: str
    connection_id: str
    provider: FakePooledCapacityProvider

    def list_providers(self, workspace_id: str) -> Iterable[ResolvedComputeProvider]:
        del workspace_id
        return (self._resolved(),)

    def resolve(self, workspace_id: str, provider_ref: str) -> ResolvedComputeProvider:
        del workspace_id
        if provider_ref != self.ref:
            raise KeyError(provider_ref)
        return self._resolved()

    def _resolved(self) -> ResolvedComputeProvider:
        return ResolvedComputeProvider(
            ref=self.ref,
            capacity_mode=ComputeCapacityMode.Pooled,
            connection_id=self.connection_id,
            pooled=self.provider,
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("ready", "fail", "silent"), default="ready")
    parser.add_argument(
        "--reason",
        choices=[reason.value for reason in MachineBootstrapFailureReason],
        default=MachineBootstrapFailureReason.AgentEnrollmentFailed.value,
        help="bootstrap failure reason reported by fake agents in --mode fail",
    )
    parser.add_argument("--desired", type=int, default=1)
    parser.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    parser.add_argument("--redis-url", default=DEFAULT_REDIS_URL)
    args = parser.parse_args(argv)
    if args.desired < 1:
        parser.error("--desired must be at least 1")

    try:
        _prepare_disposable_database(str(args.database_url))
    except OperationalError as exc:
        print(f"fake devloop prerequisite unavailable: PostgreSQL: {exc}", file=sys.stderr)
        return SKIP

    redis_settings = RedisSettings(url=str(args.redis_url), key_prefix=_REDIS_KEY_PREFIX)
    redis = RedisClient.from_settings(redis_settings)
    binary_redis = RedisClient.from_settings(redis_settings, decode_responses=False)
    try:
        redis.ping()
    except (RedisError, OSError) as exc:
        redis.close()
        binary_redis.close()
        print(f"fake devloop prerequisite unavailable: Redis: {exc}", file=sys.stderr)
        return SKIP

    with tempfile.TemporaryDirectory(prefix="fake-devloop-") as scratch:
        services = ApiServices.create(
            DatabaseClient.from_settings(
                DatabaseSettings(
                    url=str(args.database_url),
                    application_name=DatabaseApplicationName.Admin,
                )
            ),
            root=Path(scratch),
            redis_client=redis,
            binary_redis_client=binary_redis,
            owns_redis_client=True,
            owns_binary_redis_client=True,
            agent_artifact_settings=AgentArtifactSettings(
                binary_dir=Path(scratch),
                artifact_version="fake-devloop",
                artifact_sha256_by_arch={"amd64": "0" * 64},
            ),
            aws_account_connection_settings=AwsAccountConnectionSettings(enabled=False),
            aws_capacity_settings=AwsCapacitySettings(
                worker_image_digest=f"worker@sha256:{'0' * 64}",
                agent_artifact_url=_AGENT_ARTIFACT_URL,
                cpu_ami_ids={_REGION: "ami-00000000000000000"},
                gpu_ami_ids={_REGION: "ami-00000000000000000"},
                instance_hourly_micros={_INSTANCE_TYPE: 340_000},
            ),
        )
        try:
            return _run(
                services,
                mode=str(args.mode),
                reason=MachineBootstrapFailureReason(str(args.reason)),
                desired=int(args.desired),
            )
        finally:
            services.close()


def _run(
    services: ApiServices,
    *,
    mode: str,
    reason: MachineBootstrapFailureReason,
    desired: int,
) -> int:
    ControlPlaneService(services.context).upsert_workspace("default")
    connection_id = str(uuid4())
    _seed_connection(services, connection_id=connection_id, desired=desired)

    clock = _ManualClock(now=utc_now())
    # Production enrollment (`gateway.provider_enrollment._enrollment_target`)
    # requires `provider_ref == "aws:<connection-id>"` and a Ready AWS account
    # connection, so the fake keeps the `aws:` ref format instead of `fake:`.
    provider_ref = f"aws:{connection_id}"
    provider = FakePooledCapacityProvider(offers=(_offer(provider_ref, desired),), clock=clock)
    compute = ComputeService(
        services.context,
        provider_resolver=_FakeResolver(
            ref=provider_ref,
            connection_id=connection_id,
            provider=provider,
        ),
        pool_bootstrap_factory=_bootstrap,
        scheduler_hooks=SchedulerComputeHooks(
            RedisComputeStateRepository(services.redis()),
            services.scheduler_workers,
        ),
        capacity_owner_mutations=services.capacity_reservation_repository,
    )
    sts_client = FakeStsHttpClient(account_id=_ACCOUNT_ID, role_name=_ROLE_NAME)
    enrollment = ProviderNodeEnrollmentService(
        gateway=replace(services.gateway_service, tailnet=TailnetConfig(enabled=True)),
        compute=compute,
        identity_verifier=AwsProviderNodeIdentityAdapter(
            http_client=sts_client,
            replay_guard=FakeReplayGuard(),
        ),
    )

    pool = compute.prepare_pooled_capacity(
        workspace="default",
        requirements=ComputeResourceRequirements(cpu_millicores=1_000, memory_mb=1_024),
        region=_REGION,
        desired_machines=desired,
        workspace_machine_limit=max(desired, 10),
        root_volume_gib=200,
    )
    action = _agent_action(mode)
    acted: set[str] = set()
    print(f"mode={mode} desired={desired} pool={pool.name} ({pool.id})")

    for iteration in range(1, _MAX_ITERATIONS + 1):
        compute.reconcile_provider_capacity(now=clock())
        for record in _instances(services, pool.id):
            if (
                record.instance_id is None
                or record.instance_id in acted
                or record.status in _TERMINAL_STATUSES
                or record.bootstrap_phase is not MachineBootstrapPhase.Provisioning
            ):
                continue
            agent = FakeNodeAgent(
                enrollment=enrollment,
                pool_id=pool.id,
                region=_REGION,
                instance_id=record.instance_id,
                sts_client=sts_client,
            )
            agent.run(action, reason=reason)
            acted.add(record.instance_id)
        compute.reconcile_provider_capacity(now=clock())
        records = _instances(services, pool.id)
        durable_pool = _pool_record(services, pool.id)
        _print_instances(iteration, durable_pool, records)
        if _terminal(mode, durable_pool, records, desired=desired):
            return _finish(mode, durable_pool, records)
        clock.advance(timedelta(hours=2) if mode == "silent" else timedelta(seconds=30))

    print(
        f"devloop did not reach the {mode!r} terminal state within "
        f"{_MAX_ITERATIONS} iterations; last observed phases are printed above"
    )
    return 1


def _finish(
    mode: str,
    pool: ComputePoolRecord,
    records: list[ComputeProviderInstanceRecord],
) -> int:
    if mode == "ready":
        print(
            "all instances reached bootstrap phase joining/ready; joining is the "
            "expected terminal pre-worker state for the devloop (no real agent "
            "heartbeat or worker runs here)"
        )
    elif mode == "fail":
        print(
            "all instances hold a durable failed bootstrap phase with the "
            f"reported failure reason ({len(records)} records)"
        )
    else:
        attempts = len([record for record in records if record.instance_id is not None])
        print(
            "silent instances were reclaimed at each bootstrap phase deadline, "
            f"relaunched {attempts} time(s), and after exhausting launch attempts "
            f"the pool degraded durably: degraded_reason="
            f"{pool.provider_state.degraded_reason!r}"
        )
    return 0


def _agent_action(mode: str) -> FakeNodeAgentAction:
    if mode == "ready":
        return FakeNodeAgentAction.Succeed
    if mode == "fail":
        return FakeNodeAgentAction.Fail
    return FakeNodeAgentAction.Silent


def _terminal(
    mode: str,
    pool: ComputePoolRecord,
    records: list[ComputeProviderInstanceRecord],
    *,
    desired: int,
) -> bool:
    launched = [record for record in records if record.instance_id is not None]
    reclaimed = len(launched) >= desired and all(
        record.status in _TERMINAL_STATUSES
        or record.bootstrap_phase is MachineBootstrapPhase.Failed
        for record in launched
    )
    if mode == "ready":
        open_records = [record for record in launched if record.status not in _TERMINAL_STATUSES]
        return len(open_records) >= desired and all(
            record.bootstrap_phase in {MachineBootstrapPhase.Joining, MachineBootstrapPhase.Ready}
            for record in open_records
        )
    if mode == "fail":
        return reclaimed
    # Silent nodes are reclaimed per bootstrap-phase deadline and relaunched up
    # to the launch-attempt bound; the terminal state is a durably degraded pool
    # with every launched instance proven reclaimed.
    return reclaimed and pool.provider_state.degraded_reason is not None


def _instances(services: ApiServices, pool_id: str) -> list[ComputeProviderInstanceRecord]:
    with services.context.database.session() as session:
        return ComputeProviderInstanceRepository(session).list_for_pool(pool_id)


def _pool_record(services: ApiServices, pool_id: str) -> ComputePoolRecord:
    with services.context.database.session() as session:
        record = ComputePoolRepository(session).get(pool_id)
    if record is None:
        raise RuntimeError(f"devloop compute pool disappeared: {pool_id}")
    return record


def _print_instances(
    iteration: int,
    pool: ComputePoolRecord,
    records: list[ComputeProviderInstanceRecord],
) -> None:
    degraded = pool.provider_state.degraded_reason or "-"
    print(
        f"iteration {iteration}: pool phase={pool.phase.value} "
        f"desired={pool.desired_machines} observed={pool.observed_machines} "
        f"degraded_reason={degraded}"
    )
    if not records:
        print("  (no provider instance records)")
        return
    header = (
        f"  {'instance_id':<20} {'status':<12} {'bootstrap_phase':<16} "
        f"{'failure_reason':<26} machine_id"
    )
    print(header)
    for record in sorted(records, key=lambda item: item.instance_id or item.id):
        failure = record.bootstrap_failure_reason.value if record.bootstrap_failure_reason else "-"
        print(
            f"  {record.instance_id or '-':<20} {record.status:<12} "
            f"{record.bootstrap_phase.value:<16} {failure:<26} {record.machine_id or '-'}"
        )


def _seed_connection(services: ApiServices, *, connection_id: str, desired: int) -> None:
    now = utc_now()
    authorization = AwsAccountAuthorizationGeneration(
        id=str(uuid4()),
        generation=1,
        role_arn=f"arn:aws:iam::{_ACCOUNT_ID}:role/compute-control",
        authorization_mode=AwsAccountAuthorizationMode.ExistingRole,
        phase=AwsAccountAuthorizationPhase.Ready,
        last_validated_at=now,
        created_at=now,
        updated_at=now,
    )
    with services.context.database.session() as session:
        workspace_id = services.context.default_workspace_id(session)
        # The workspace policy is the capacity authority: a zero-capacity policy
        # actively drives internal pools back to zero, so the loop's desired
        # machines must be policy-owned to survive reconciliation.
        WorkspaceComputePolicyRepository(session).ensure_default(
            WorkspaceComputePolicy(
                id=str(uuid4()),
                workspace_id=workspace_id,
                default_placement=ComputePlacementTarget.Aws,
                aws=AwsWorkspaceComputePolicy(
                    default_region=_REGION,
                    default_instance_type=_INSTANCE_TYPE,
                    initial_cpu_workers=desired,
                    min_cpu_workers=desired,
                    max_cpu_instances=max(desired, 10),
                ),
                created_at=now,
                updated_at=now,
            )
        )
        AwsAccountConnectionRepository(session).create(
            AwsAccountConnection(
                id=connection_id,
                workspace_id=workspace_id,
                account_id=_ACCOUNT_ID,
                external_id="x" * 48,
                phase=AwsAccountConnectionPhase.Ready,
                active_authorization=authorization,
                node_role_arn=f"arn:aws:iam::{_ACCOUNT_ID}:role/{_ROLE_NAME}",
                node_instance_profile_arn=(
                    f"arn:aws:iam::{_ACCOUNT_ID}:instance-profile/{_ROLE_NAME}"
                ),
                created_at=now,
                updated_at=now,
            )
        )


def _offer(provider_ref: str, desired: int) -> ComputeOffer:
    return ComputeOffer(
        id=f"{_REGION}:{_INSTANCE_TYPE}",
        provider=provider_ref,
        cloud="aws",
        instance_type=_INSTANCE_TYPE,
        region=_REGION,
        cpu_millicores=4_000,
        memory_mb=32 * 1024,
        storage_mb=200 * 1024,
        hourly_cost_micros=340_000,
        available=max(desired, 10),
        capacity_mode=ComputeCapacityMode.Pooled,
        capability_key=f"aws:{_REGION}:{_INSTANCE_TYPE}:amd64:runc",
        supports_scale_to_zero=True,
    )


def _bootstrap(pool: ComputePoolRecord, offer: ComputeOffer) -> ProviderPoolBootstrap:
    del offer
    return ProviderPoolBootstrap(
        control_plane_url="https://control.fake-devloop.invalid",
        enrollment_request_id=pool.id,
        agent_version="0.0.0-fake",
        agent_sha256="a" * 64,
        agent_artifact_url=_AGENT_ARTIFACT_URL,
        worker_image_digest=f"registry.fake-devloop.invalid/worker@sha256:{'b' * 64}",
    )


def _prepare_disposable_database(database_url: str) -> None:
    """Recreate the devloop's own database; leave user-supplied databases alone.

    Only the fixed ``lazycloud_fake_devloop`` name is positively identified as
    local and disposable, so only that database is ever dropped.
    """
    url = sqlalchemy.engine.make_url(database_url)
    if url.database != DEVLOOP_DATABASE:
        return
    admin_engine = sqlalchemy.create_engine(
        url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        connect_args={"connect_timeout": 5},
    )
    try:
        with admin_engine.connect() as connection:
            connection.execute(
                sqlalchemy.text(f'DROP DATABASE IF EXISTS "{DEVLOOP_DATABASE}" WITH (FORCE)')
            )
            connection.execute(sqlalchemy.text(f'CREATE DATABASE "{DEVLOOP_DATABASE}"'))
    finally:
        admin_engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
