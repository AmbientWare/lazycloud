from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import uuid4

from api.server.services import ApiServices
from compute.bucket_access import (
    AwsDeploymentBucketAccessService,
    ConnectedBucketAccessGrant,
)
from compute.policy import WorkspaceComputePolicyService
from database.repositories.aws_connections import AwsAccountConnectionRepository
from shared.aws_connections import (
    AwsAccountAuthorizationGeneration,
    AwsAccountAuthorizationMode,
    AwsAccountAuthorizationPhase,
    AwsAccountConnection,
    AwsAccountConnectionPhase,
)
from shared.deployment_records import DeploymentSpec, VolumeMount
from shared.identity import WorkspaceRecord
from shared.mounts import MountAuthMode
from tests.workspaces import workspace_owner_user_id


@dataclass(slots=True)
class _BucketAccessController:
    grants: tuple[ConnectedBucketAccessGrant, ...] = ()

    def reconcile(
        self,
        connection: AwsAccountConnection,
        grants: tuple[ConnectedBucketAccessGrant, ...],
    ) -> None:
        assert connection.phase is AwsAccountConnectionPhase.Ready
        self.grants = grants


def test_aws_deployment_lifecycle_reconciles_aggregate_ambient_bucket_access(
    isolated_services: ApiServices,
) -> None:
    workspace = _seed_connected_workspace(isolated_services)
    controller = _BucketAccessController()
    bucket_access = AwsDeploymentBucketAccessService(
        context=isolated_services.context,
        controller=controller,
    )
    deployments = replace(
        isolated_services.deployments,
        placement=WorkspaceComputePolicyService(isolated_services.context),
        placement_resources=bucket_access,
    )

    first = deployments.deploy(
        DeploymentSpec(
            name="bucket-reader",
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
        ),
        workspace=workspace.id,
    )
    deployments.deploy(
        DeploymentSpec(
            name="secret-backed-bucket",
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
        ),
        workspace=workspace.id,
    )

    assert controller.grants == (
        ConnectedBucketAccessGrant(
            bucket="customer-data", prefix="workspace/input", read_only=True
        ),
    )

    deployments.delete(first.id, workspace=workspace.id)

    assert controller.grants == ()


def _seed_connected_workspace(isolated_services: ApiServices) -> WorkspaceRecord:
    """A workspace living in a ready connection owned by the default workspace's owner."""
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
    connection_id = str(uuid4())
    with isolated_services.context.database.session() as session:
        AwsAccountConnectionRepository(session).create(
            AwsAccountConnection(
                id=connection_id,
                user_id=owner_id,
                account_id=account_id,
                external_id="x" * 48,
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
    return isolated_services.control_plane_service.set_workspace(
        "bucket-access", owner_user_id=owner_id, connection_id=connection_id
    )
