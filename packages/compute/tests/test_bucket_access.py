from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from uuid import uuid4

from api.server.services import ApiServices
from compute.bucket_access import (
    AwsDeploymentBucketAccessService,
    ConnectedBucketAccessGrant,
)
from compute.policy import WorkspaceComputePolicyService
from database.repositories.compute import AwsAccountConnectionRepository
from shared.aws_connections import (
    AwsAccountAuthorizationGeneration,
    AwsAccountAuthorizationMode,
    AwsAccountAuthorizationPhase,
    AwsAccountConnection,
    AwsAccountConnectionPhase,
)
from shared.capacity import MachinePool
from shared.deployment_records import DeploymentSpec, VolumeMount
from shared.mounts import MountAuthMode
from tests.workspaces import workspace_owner_user_id


@dataclass(slots=True)
class _BucketAccessController:
    calls: list[tuple[ConnectedBucketAccessGrant, ...]] = field(default_factory=list)

    def reconcile(
        self,
        connection: AwsAccountConnection,
        grants: tuple[ConnectedBucketAccessGrant, ...],
    ) -> None:
        assert connection.phase is AwsAccountConnectionPhase.Ready
        self.calls.append(grants)


def test_aws_deployment_lifecycle_reconciles_aggregate_ambient_bucket_access(
    isolated_services: ApiServices,
) -> None:
    _seed_ready_connection(isolated_services)
    controller = _BucketAccessController()
    bucket_access = AwsDeploymentBucketAccessService(
        context=isolated_services.context,
        controller=controller,
    )
    deployments = replace(
        isolated_services.deployments,
        pool_resolver=WorkspaceComputePolicyService(isolated_services.context),
        placement_resources=bucket_access,
    )

    first = deployments.deploy(
        DeploymentSpec(
            name="bucket-reader",
            metadata={"pool": "aws"},
            volumes=[
                VolumeMount(
                    name="customer-data",
                    mount_path="/data",
                    read_only=True,
                    config={
                        "bucket_name": "customer-data",
                        "prefix": "workspace/input",
                        "auth_mode": MountAuthMode.Ambient,
                    },
                )
            ],
        )
    )
    deployments.deploy(
        DeploymentSpec(
            name="secret-backed-bucket",
            metadata={"pool": "aws"},
            volumes=[
                VolumeMount(
                    name="other-data",
                    mount_path="/other",
                    config={
                        "bucket_name": "other-data",
                        "auth_mode": MountAuthMode.SecretReferences,
                        "access_key": "bucket-access-key",
                        "secret_key": "bucket-secret-key",
                    },
                )
            ],
        )
    )

    assert controller.calls == [
        (
            ConnectedBucketAccessGrant(
                bucket="customer-data",
                prefix="workspace/input",
                read_only=True,
            ),
        ),
        (
            ConnectedBucketAccessGrant(
                bucket="customer-data",
                prefix="workspace/input",
                read_only=True,
            ),
        ),
    ]

    deployments.delete(first.id)

    assert controller.calls[-1] == ()


def _seed_ready_connection(isolated_services: ApiServices) -> None:
    """A ready connection owned by the account that owns the default workspace."""
    now = datetime.now(UTC)
    account_id = "123456789012"
    authorization = AwsAccountAuthorizationGeneration(
        id=str(uuid4()),
        generation=1,
        role_arn=f"arn:aws:iam::{account_id}:role/compute-control",
        authorization_mode=AwsAccountAuthorizationMode.ExistingRole,
        phase=AwsAccountAuthorizationPhase.Ready,
        last_validated_at=now,
        created_at=now,
        updated_at=now,
    )
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
    owner_id = workspace_owner_user_id(isolated_services.context, workspace_id)
    with isolated_services.context.database.session() as session:
        AwsAccountConnectionRepository(session).create(
            AwsAccountConnection(
                id=str(uuid4()),
                user_id=owner_id,
                account_id=account_id,
                external_id="x" * 48,
                pool=MachinePool("aws"),
                phase=AwsAccountConnectionPhase.Ready,
                active_authorization=authorization,
                node_role_arn=f"arn:aws:iam::{account_id}:role/compute-node",
                node_instance_profile_arn=(
                    f"arn:aws:iam::{account_id}:instance-profile/compute-node"
                ),
                created_at=now,
                updated_at=now,
            )
        )
