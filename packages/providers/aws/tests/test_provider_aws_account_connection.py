from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Literal, overload
from urllib.parse import parse_qs, quote, urlparse

import pytest
from botocore.exceptions import ClientError
from provider_aws import (
    AwsAccountAuthorizationCleanupStatus,
    AwsAccountAuthorizationValidationError,
    AwsAccountAuthorizationValidationErrorCode,
    AwsAccountAuthorizationValidationInput,
    AwsAccountConnectionPlanner,
    AwsAccountConnectionTarget,
    AwsAccountConnectionTemplatePublication,
    AwsActiveAccountAuthorization,
    AwsExistingAccountAuthorizationValidationInput,
    AwsNodeBucketAccessGrant,
    AwsProviderControlError,
    Boto3AwsAccountAuthorizationControl,
    Boto3AwsAccountConnectionValidator,
    Boto3AwsNodeBucketAccessControl,
    aws_account_connection_template_bytes,
    aws_account_connection_template_identity,
    aws_node_bucket_access_policy,
    parse_aws_connection_stack_cleanup_action,
    parse_aws_connection_stack_create_action,
    validate_aws_account_connection_template_policy,
)
from provider_aws.account_connection import (
    AwsConnectionCloudFormationClient,
    AwsConnectionEc2Client,
    AwsConnectionIamClient,
    AwsConnectionStsClient,
)
from pydantic import JsonValue, SecretStr, TypeAdapter
from shared.aws_connections import AwsAccountNetwork

_ACCOUNT_ID = "123456789012"
_EXTERNAL_ID = "connection-external-id-0123456789abcdef"
_PLATFORM_PRINCIPAL = "arn:aws:iam::210987654321:role/customer-compute-control"
_WORKSPACE_ID = "12345678-1234-4123-8123-123456789abc"
_CONNECTION_ID = "22345678-1234-4123-8123-123456789abc"
_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


def _json_object(value: JsonValue) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise AssertionError("expected a JSON object")
    return value


def _json_array(value: JsonValue) -> list[JsonValue]:
    if not isinstance(value, list):
        raise AssertionError("expected a JSON array")
    return value


def _connection_policy_statements(template: dict[str, JsonValue]) -> list[JsonValue]:
    resources = _json_object(template["Resources"])
    role = _json_object(resources["ConnectionRole"])
    properties = _json_object(role["Properties"])
    policies = _json_array(properties["Policies"])
    policy = _json_object(policies[0])
    document = _json_object(policy["PolicyDocument"])
    return _json_array(document["Statement"])


def _planner() -> AwsAccountConnectionPlanner:
    identity = aws_account_connection_template_identity()
    return AwsAccountConnectionPlanner(
        platform_principal_arn=_PLATFORM_PRINCIPAL,
        publication=AwsAccountConnectionTemplatePublication(
            url=(
                "https://compute-artifacts.s3.us-east-1.amazonaws.com/"
                f"connections/{identity.sha256}/template.json"
            ),
            sha256=identity.sha256,
        ),
    )


def _active(generation: int = 1) -> AwsActiveAccountAuthorization:
    initial = (
        _planner()
        .plan_initial(
            user_id=_WORKSPACE_ID,
            connection_id=_CONNECTION_ID,
            account_id=_ACCOUNT_ID,
            external_id=SecretStr(_EXTERNAL_ID),
        )
        .pending
    )
    pending = initial.model_copy(
        update={
            "generation": generation,
            "stack_name": initial.stack_name.rsplit("-g", maxsplit=1)[0] + f"-g{generation}",
            "role_name": initial.role_name.rsplit("-g", maxsplit=1)[0] + f"-g{generation}",
            "role_arn": initial.role_arn.rsplit("-g", maxsplit=1)[0] + f"-g{generation}",
        }
    )
    return AwsActiveAccountAuthorization(
        account_id=pending.account_id,
        region=pending.region,
        generation=pending.generation,
        stack_name=pending.stack_name,
        role_name=pending.role_name,
        role_arn=pending.role_arn,
        external_id_sha256=pending.external_id_sha256,
        node_identity=pending.node_identity,
        stack_id=(
            f"arn:aws:cloudformation:us-east-1:{_ACCOUNT_ID}:"
            f"stack/{pending.stack_name}/stack-id-{generation}"
        ),
        validated_at=datetime(2026, 7, 15, tzinfo=UTC),
    )


def _quick_create_parameters(url: str) -> dict[str, list[str]]:
    fragment = urlparse(url).fragment
    query = fragment.partition("?")[2]
    return parse_qs(query, keep_blank_values=True)


def test_replacement_creates_new_generation_without_mutating_active() -> None:
    active = _active()
    replacement = _planner().plan_replacement(
        user_id=_WORKSPACE_ID,
        connection_id=_CONNECTION_ID,
        account_id=_ACCOUNT_ID,
        external_id=SecretStr(_EXTERNAL_ID),
        active=active,
    )

    assert replacement.active == active
    assert replacement.pending.generation == 2
    assert replacement.pending.stack_name != active.stack_name
    assert replacement.pending.role_arn != active.role_arn
    assert replacement.pending.node_identity == active.node_identity
    parameters = _quick_create_parameters(replacement.authorization_url)
    assert "param_PredecessorStackId" not in parameters
    assert "param_PredecessorRoleArn" not in parameters

    with pytest.raises(ValueError, match="retain the connection external ID"):
        _planner().plan_replacement(
            user_id=_WORKSPACE_ID,
            connection_id=_CONNECTION_ID,
            account_id=_ACCOUNT_ID,
            external_id=SecretStr("different-external-id-0123456789abcdef"),
            active=active,
        )


def test_customer_quick_create_action_is_bound_to_exact_account_and_generation() -> None:
    plan = _planner().plan_initial(
        user_id=_WORKSPACE_ID,
        connection_id=_CONNECTION_ID,
        account_id=_ACCOUNT_ID,
        external_id=SecretStr(_EXTERNAL_ID),
    )

    action = parse_aws_connection_stack_create_action(
        plan.authorization_url,
        account_id=_ACCOUNT_ID,
        region="us-east-1",
        expected_template_url=_quick_create_parameters(plan.authorization_url)["templateURL"][0],
        expected_platform_principal_arn=_PLATFORM_PRINCIPAL,
    )

    assert action.name == plan.pending.stack_name
    assert action.parameters["ConnectionRoleName"] == plan.pending.role_name
    assert action.parameters["TargetAccountId"] == _ACCOUNT_ID

    with pytest.raises(ValueError, match="different AWS account"):
        parse_aws_connection_stack_create_action(
            plan.authorization_url.replace(_ACCOUNT_ID, "999999999999"),
            account_id=_ACCOUNT_ID,
            region="us-east-1",
            expected_template_url=_quick_create_parameters(plan.authorization_url)["templateURL"][
                0
            ],
            expected_platform_principal_arn=_PLATFORM_PRINCIPAL,
        )

    with pytest.raises(ValueError, match="different immutable template"):
        parse_aws_connection_stack_create_action(
            plan.authorization_url,
            account_id=_ACCOUNT_ID,
            region="us-east-1",
            expected_template_url="https://example.invalid/unapproved-template.json",
            expected_platform_principal_arn=_PLATFORM_PRINCIPAL,
        )

    with pytest.raises(ValueError, match="different platform principal"):
        parse_aws_connection_stack_create_action(
            plan.authorization_url,
            account_id=_ACCOUNT_ID,
            region="us-east-1",
            expected_template_url=_quick_create_parameters(plan.authorization_url)["templateURL"][
                0
            ],
            expected_platform_principal_arn=("arn:aws:iam::210987654321:role/unapproved-control"),
        )


def test_customer_cleanup_action_requires_the_exact_managed_stack() -> None:
    name = _active().stack_name
    stack_id = f"arn:aws:cloudformation:us-east-1:{_ACCOUNT_ID}:stack/{name}/stack-id"
    action_url = (
        "https://console.aws.amazon.com/cloudformation/home"
        f"?region=us-east-1#/stacks/stackinfo?stackId={quote(stack_id, safe='')}"
    )

    action = parse_aws_connection_stack_cleanup_action(
        action_url,
        account_id=_ACCOUNT_ID,
        region="us-east-1",
        expected_names=frozenset({name}),
    )

    assert action.stack_id == stack_id
    with pytest.raises(ValueError, match="outside the approved target"):
        parse_aws_connection_stack_cleanup_action(
            action_url,
            account_id=_ACCOUNT_ID,
            region="us-east-1",
            expected_names=frozenset({"compute-connection-foreign-g1"}),
        )


def test_template_owns_authorization_network_and_exact_connection_node_scope() -> None:
    template = _JSON_OBJECT.validate_json(aws_account_connection_template_bytes())
    resources = _json_object(template["Resources"])
    role = _json_object(resources["ConnectionRole"])
    properties = _json_object(role["Properties"])
    assert properties["Tags"] == [{"Key": "cloud-pool:managed-by", "Value": "control-plane"}]
    security_group = _json_object(_json_object(resources["NodeSecurityGroup"])["Properties"])
    assert "SecurityGroupIngress" not in security_group
    assert security_group["SecurityGroupEgress"] == [
        {"IpProtocol": "-1", "CidrIp": "0.0.0.0/0", "Description": "Outbound node traffic"}
    ]
    for name in ("Vpc", "InternetGateway", "RouteTable", "SubnetA", "SubnetB"):
        network_properties = _json_object(_json_object(resources[name])["Properties"])
        tags = [_json_object(tag) for tag in _json_array(network_properties["Tags"])]
        assert {"Key": "cloud-pool:managed-by", "Value": "control-plane"} in tags
    by_sid: dict[str, dict[str, JsonValue]] = {}
    for value in _connection_policy_statements(template):
        statement = _json_object(value)
        sid = statement.get("Sid")
        if isinstance(sid, str):
            by_sid[sid] = statement
    mutating_network_actions = {
        "ec2:AssociateRouteTable",
        "ec2:AttachInternetGateway",
        "ec2:AuthorizeSecurityGroupIngress",
        "ec2:CreateInternetGateway",
        "ec2:CreateRoute",
        "ec2:CreateRouteTable",
        "ec2:CreateSecurityGroup",
        "ec2:CreateSubnet",
        "ec2:CreateVpc",
        "ec2:DeleteInternetGateway",
        "ec2:DeleteRouteTable",
        "ec2:DeleteSecurityGroup",
        "ec2:DeleteSubnet",
        "ec2:DeleteVpc",
        "ec2:DetachInternetGateway",
        "ec2:DisassociateRouteTable",
        "ec2:ModifySubnetAttribute",
        "ec2:ModifyVpcAttribute",
        "ec2:ReplaceRoute",
    }
    for sid, statement in by_sid.items():
        action = statement["Action"]
        actions = _json_array(action) if isinstance(action, list) else [action]
        assert all(item not in mutating_network_actions for item in actions), sid
    assert by_sid["RunTaggedInstanceResources"]["Condition"] == {
        "StringEquals": {"aws:RequestTag/cloud-pool:managed-by": "control-plane"}
    }
    assert by_sid["UseManagedInstanceLaunchResources"]["Condition"] == {
        "StringEquals": {"ec2:ResourceTag/cloud-pool:managed-by": "control-plane"}
    }
    assert by_sid["TagManagedInstancesOnLaunch"]["Condition"] == {
        "StringEquals": {
            "aws:RequestTag/cloud-pool:managed-by": "control-plane",
            "ec2:CreateAction": "RunInstances",
        }
    }
    node_role_arn = {"Fn::Sub": "arn:${AWS::Partition}:iam::${AWS::AccountId}:role/${NodeRoleName}"}
    node_identity_arns = [
        {
            "Fn::Sub": (
                "arn:${AWS::Partition}:iam::${AWS::AccountId}:"
                "instance-profile/${NodeInstanceProfileName}"
            )
        },
        node_role_arn,
    ]
    assert by_sid["CreateManagedNodeIdentity"]["Resource"] == node_identity_arns
    assert by_sid["ManageOwnedNodeIdentity"]["Resource"] == node_identity_arns
    assert by_sid["PassOwnedNodeRole"]["Resource"] == node_role_arn
    assert by_sid["PassOwnedNodeRole"]["Condition"] == {
        "StringEquals": {"iam:PassedToService": "ec2.amazonaws.com"}
    }


def test_template_policy_rejects_the_old_self_delete_deny() -> None:
    template = _JSON_OBJECT.validate_json(aws_account_connection_template_bytes())
    statements = _connection_policy_statements(template)
    statements.append(
        {
            "Sid": "DenySelfRevocation",
            "Effect": "Deny",
            "Action": "cloudformation:DeleteStack",
            "Resource": {"Ref": "AWS::StackId"},
        }
    )

    with pytest.raises(ValueError, match="exact AWS::StackId; decision was explicit_deny"):
        validate_aws_account_connection_template_policy(
            (json.dumps(template, sort_keys=True, separators=(",", ":")) + "\n").encode()
        )


def test_template_policy_rejects_access_to_another_stack() -> None:
    template = _JSON_OBJECT.validate_json(aws_account_connection_template_bytes())
    statements = _connection_policy_statements(template)
    statements.append(
        {
            "Sid": "ManageEveryAuthorization",
            "Effect": "Allow",
            "Action": "cloudformation:DeleteStack",
            "Resource": "*",
        }
    )

    with pytest.raises(ValueError, match="implicitly deny cloudformation:DeleteStack"):
        validate_aws_account_connection_template_policy(
            (json.dumps(template, sort_keys=True, separators=(",", ":")) + "\n").encode()
        )


def test_bucket_access_policy_scopes_objects_and_write_actions_to_prefix() -> None:
    policy = aws_node_bucket_access_policy(
        (
            AwsNodeBucketAccessGrant(bucket="customer-data", read_only=True),
            AwsNodeBucketAccessGrant(
                bucket="customer-results",
                prefix="workspace/jobs",
                read_only=False,
            ),
        )
    )
    raw_statements = TypeAdapter(list[dict[str, object]]).validate_python(policy["Statement"])
    statements = {statement["Sid"]: statement for statement in raw_statements}

    assert statements["ListPrefix2"]["Condition"] == {
        "StringLike": {
            "s3:prefix": ["workspace/jobs", "workspace/jobs/*"],
        }
    }
    assert statements["Objects1"]["Resource"] == ["arn:aws:s3:::customer-data/*"]
    read_actions = TypeAdapter(list[str]).validate_python(statements["Objects1"]["Action"])
    assert "s3:PutObject" not in read_actions
    assert statements["Objects2"]["Resource"] == ["arn:aws:s3:::customer-results/workspace/jobs/*"]
    write_actions = TypeAdapter(list[str]).validate_python(statements["Objects2"]["Action"])
    assert "s3:PutObject" in write_actions


class _Sts:
    def __init__(self, state: _AwsState, *, assumed: bool) -> None:
        self.state = state
        self.assumed = assumed

    def assume_role(
        self,
        *,
        RoleArn: str,
        RoleSessionName: str,
        DurationSeconds: int,
        ExternalId: str | None = None,
    ) -> Mapping[str, object]:
        del RoleSessionName, DurationSeconds
        if self.state.revoked or (self.state.enforce_external_id and ExternalId != _EXTERNAL_ID):
            raise ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "not authorized"}},
                "AssumeRole",
            )
        assert RoleArn.startswith(f"arn:aws:iam::{_ACCOUNT_ID}:role/")
        return {
            "Credentials": {
                "AccessKeyId": "temporary-access-key",
                "SecretAccessKey": "temporary-secret-key",
                "SessionToken": "temporary-session-token",
            }
        }

    def get_caller_identity(self) -> Mapping[str, object]:
        assert self.assumed
        return {
            "Account": _ACCOUNT_ID,
            "Arn": f"arn:aws:sts::{_ACCOUNT_ID}:assumed-role/connection/session",
        }


class _Ec2:
    def __init__(self, state: _AwsState) -> None:
        self.state = state

    def describe_regions(self, *, RegionNames: list[str]) -> Mapping[str, object]:
        return {"Regions": [{"RegionName": RegionNames[0]}]}

    def describe_volumes(
        self,
        *,
        Filters: list[dict[str, object]],
        MaxResults: int,
    ) -> Mapping[str, object]:
        assert Filters == [
            {
                "Name": "tag:cloud-pool:managed-by",
                "Values": ["control-plane"],
            }
        ]
        assert MaxResults == 5
        self.state.volume_inventory_reads += 1
        return {"Volumes": []}

    def describe_subnets(self, *, SubnetIds: list[str]) -> Mapping[str, object]:
        return {
            "Subnets": [
                {
                    "SubnetId": subnet_id,
                    "VpcId": self.state.subnets[subnet_id][0],
                    "AvailabilityZone": self.state.subnets[subnet_id][1],
                }
                for subnet_id in SubnetIds
                if subnet_id in self.state.subnets
            ]
        }

    def describe_security_groups(self, *, GroupIds: list[str]) -> Mapping[str, object]:
        return {
            "SecurityGroups": [
                {"GroupId": group_id, "VpcId": self.state.security_groups[group_id]}
                for group_id in GroupIds
                if group_id in self.state.security_groups
            ]
        }


class _Iam:
    def __init__(self, state: _AwsState) -> None:
        self.state = state

    def get_role(self, *, RoleName: str) -> Mapping[str, object]:
        arn = self.state.roles.get(RoleName)
        if arn is None:
            raise _not_found("GetRole")
        return {"Role": {"Arn": arn}}

    def create_role(
        self,
        *,
        RoleName: str,
        AssumeRolePolicyDocument: str,
        Tags: list[dict[str, str]],
    ) -> Mapping[str, object]:
        assert "ec2.amazonaws.com" in AssumeRolePolicyDocument
        assert Tags == [{"Key": "cloud-pool:managed-by", "Value": "control-plane"}]
        arn = f"arn:aws:iam::{_ACCOUNT_ID}:role/{RoleName}"
        self.state.roles[RoleName] = arn
        self.state.role_policies[RoleName] = ["managed-compute-control"]
        return {"Role": {"Arn": arn}}

    def get_instance_profile(self, *, InstanceProfileName: str) -> Mapping[str, object]:
        profile = self.state.profiles.get(InstanceProfileName)
        if profile is None:
            raise _not_found("GetInstanceProfile")
        arn, roles = profile
        return {"InstanceProfile": {"Arn": arn, "Roles": [{"Arn": role} for role in roles]}}

    def create_instance_profile(
        self,
        *,
        InstanceProfileName: str,
        Tags: list[dict[str, str]],
    ) -> Mapping[str, object]:
        assert Tags == [{"Key": "cloud-pool:managed-by", "Value": "control-plane"}]
        arn = f"arn:aws:iam::{_ACCOUNT_ID}:instance-profile/{InstanceProfileName}"
        self.state.profiles[InstanceProfileName] = (arn, [])
        return {"InstanceProfile": {"Arn": arn, "Roles": []}}

    def add_role_to_instance_profile(
        self,
        *,
        InstanceProfileName: str,
        RoleName: str,
    ) -> Mapping[str, object]:
        arn, _ = self.state.profiles[InstanceProfileName]
        self.state.profiles[InstanceProfileName] = (arn, [self.state.roles[RoleName]])
        return {}

    def remove_role_from_instance_profile(
        self,
        *,
        InstanceProfileName: str,
        RoleName: str,
    ) -> Mapping[str, object]:
        del RoleName
        arn, _ = self.state.profiles[InstanceProfileName]
        self.state.profiles[InstanceProfileName] = (arn, [])
        return {}

    def delete_instance_profile(self, *, InstanceProfileName: str) -> Mapping[str, object]:
        self.state.profiles.pop(InstanceProfileName, None)
        return {}

    def list_role_policies(self, *, RoleName: str) -> Mapping[str, object]:
        return {"PolicyNames": self.state.role_policies.get(RoleName, []), "IsTruncated": False}

    def delete_role_policy(self, *, RoleName: str, PolicyName: str) -> Mapping[str, object]:
        self.state.role_policies[RoleName].remove(PolicyName)
        self.state.policy_documents.pop((RoleName, PolicyName), None)
        return {}

    def put_role_policy(
        self,
        *,
        RoleName: str,
        PolicyName: str,
        PolicyDocument: str,
    ) -> Mapping[str, object]:
        policies = self.state.role_policies.setdefault(RoleName, [])
        if PolicyName not in policies:
            policies.append(PolicyName)
        self.state.policy_documents[(RoleName, PolicyName)] = json.loads(PolicyDocument)
        return {}

    def delete_role(self, *, RoleName: str) -> Mapping[str, object]:
        self.state.roles.pop(RoleName, None)
        self.state.role_policies.pop(RoleName, None)
        return {}


class _CloudFormation:
    def __init__(self, state: _AwsState) -> None:
        self.state = state

    def describe_stacks(self, *, StackName: str) -> Mapping[str, object]:
        stack = self.state.stacks.get(StackName)
        if stack is None:
            stack = next(
                (
                    candidate
                    for candidate in self.state.stacks.values()
                    if candidate.get("StackId") == StackName
                ),
                None,
            )
        if stack is None:
            raise ClientError(
                {
                    "Error": {
                        "Code": "ValidationError",
                        "Message": f"Stack with id {StackName} does not exist",
                    }
                },
                "DescribeStacks",
            )
        return {"Stacks": [stack]}

    def delete_stack(
        self,
        *,
        StackName: str,
        ClientRequestToken: str,
    ) -> Mapping[str, object]:
        self.state.delete_requests.append((StackName, ClientRequestToken))
        self.state.node_identity_counts_at_stack_delete.append(
            (len(self.state.roles), len(self.state.profiles))
        )
        stack = next(
            candidate
            for candidate in self.state.stacks.values()
            if candidate.get("StackId") == StackName
        )
        stack["StackStatus"] = "DELETE_IN_PROGRESS"
        return {}


class _Session:
    def __init__(self, state: _AwsState, *, assumed: bool) -> None:
        self.state = state
        self.assumed = assumed

    @overload
    def client(self, service_name: Literal["sts"]) -> AwsConnectionStsClient: ...

    @overload
    def client(self, service_name: Literal["iam"]) -> AwsConnectionIamClient: ...

    @overload
    def client(self, service_name: Literal["ec2"]) -> AwsConnectionEc2Client: ...

    @overload
    def client(
        self, service_name: Literal["cloudformation"]
    ) -> AwsConnectionCloudFormationClient: ...

    def client(
        self,
        service_name: Literal["sts", "iam", "ec2", "cloudformation"],
    ) -> (
        AwsConnectionStsClient
        | AwsConnectionIamClient
        | AwsConnectionEc2Client
        | AwsConnectionCloudFormationClient
    ):
        if service_name == "sts":
            return _Sts(self.state, assumed=self.assumed)
        if service_name == "iam":
            return _Iam(self.state)
        if service_name == "ec2":
            return _Ec2(self.state)
        if service_name == "cloudformation":
            return _CloudFormation(self.state)
        raise AssertionError(service_name)


class _AwsState:
    def __init__(self) -> None:
        self.revoked = False
        self.enforce_external_id = True
        self.roles: dict[str, str] = {}
        self.role_policies: dict[str, list[str]] = {}
        self.policy_documents: dict[tuple[str, str], dict[str, object]] = {}
        self.profiles: dict[str, tuple[str, list[str]]] = {}
        self.stacks: dict[str, dict[str, object]] = {}
        self.delete_requests: list[tuple[str, str]] = []
        self.node_identity_counts_at_stack_delete: list[tuple[int, int]] = []
        self.volume_inventory_reads = 0
        self.subnets: dict[str, tuple[str, str]] = {}
        self.security_groups: dict[str, str] = {}

    def sessions(
        self,
        *,
        region_name: str,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
        aws_session_token: str | None = None,
    ) -> _Session:
        assert region_name == "us-east-1"
        credentials = (aws_access_key_id, aws_secret_access_key, aws_session_token)
        return _Session(self, assumed=all(value is not None for value in credentials))


def _not_found(operation: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": "NoSuchEntity", "Message": "not found"}},
        operation,
    )


_STACK_VPC_ID = "vpc-00000000000000001"
_STACK_SUBNET_IDS = ("subnet-00000000000000001", "subnet-00000000000000002")
_STACK_SECURITY_GROUP_ID = "sg-00000000000000001"


def _stack(
    active: AwsActiveAccountAuthorization,
    *,
    status: str = "CREATE_COMPLETE",
    include_network_outputs: bool = True,
) -> dict[str, object]:
    outputs: list[dict[str, object]] = [
        {"OutputKey": "ConnectionRoleArn", "OutputValue": active.role_arn}
    ]
    if include_network_outputs:
        outputs.extend(
            [
                {"OutputKey": "VpcId", "OutputValue": _STACK_VPC_ID},
                {"OutputKey": "SubnetIds", "OutputValue": ",".join(_STACK_SUBNET_IDS)},
                {"OutputKey": "SecurityGroupId", "OutputValue": _STACK_SECURITY_GROUP_ID},
            ]
        )
    return {
        "StackId": active.stack_id,
        "StackName": active.stack_name,
        "StackStatus": status,
        "Outputs": outputs,
    }


def test_bucket_access_control_reconciles_one_inline_node_role_policy() -> None:
    active = _active()
    identity = active.node_identity
    state = _AwsState()
    state.roles[identity.role_name] = identity.role_arn
    state.role_policies[identity.role_name] = []
    state.profiles[identity.instance_profile_name] = (
        identity.instance_profile_arn,
        [identity.role_arn],
    )
    target = AwsAccountConnectionTarget(
        account_id=_ACCOUNT_ID,
        region="us-east-1",
        role_arn=active.role_arn,
        external_id=SecretStr(_EXTERNAL_ID),
        node_role_arn=identity.role_arn,
        node_instance_profile_arn=identity.instance_profile_arn,
    )
    control = Boto3AwsNodeBucketAccessControl(state.sessions)

    control.reconcile(
        target,
        (AwsNodeBucketAccessGrant(bucket="customer-data", prefix="jobs"),),
    )

    document = state.policy_documents[(identity.role_name, "mounted-bucket-access")]
    assert document == aws_node_bucket_access_policy(
        (AwsNodeBucketAccessGrant(bucket="customer-data", prefix="jobs"),)
    )

    control.reconcile(target, ())

    assert "mounted-bucket-access" not in state.role_policies[identity.role_name]
    assert (identity.role_name, "mounted-bucket-access") not in state.policy_documents


@pytest.mark.parametrize("stack_status", ["CREATE_COMPLETE", "UPDATE_COMPLETE"])
def test_validation_accepts_ready_stack_and_ensures_node_identity(stack_status: str) -> None:
    pending = (
        _planner()
        .plan_initial(
            user_id=_WORKSPACE_ID,
            connection_id=_CONNECTION_ID,
            account_id=_ACCOUNT_ID,
            external_id=SecretStr(_EXTERNAL_ID),
        )
        .pending
    )
    expected = _active()
    state = _AwsState()
    state.stacks[pending.stack_name] = _stack(expected, status=stack_status)

    result = Boto3AwsAccountConnectionValidator(state.sessions).validate_authorization(
        AwsAccountAuthorizationValidationInput(
            pending=pending,
            external_id=SecretStr(_EXTERNAL_ID),
        )
    )

    assert result.authorization.stack_id == expected.stack_id
    assert result.node_identity == pending.node_identity
    assert result.network.vpc_id == _STACK_VPC_ID
    assert result.network.subnet_ids == _STACK_SUBNET_IDS
    assert result.network.security_group_id == _STACK_SECURITY_GROUP_ID
    assert state.roles[pending.node_identity.role_name] == pending.node_identity.role_arn
    assert state.profiles[pending.node_identity.instance_profile_name][1] == [
        pending.node_identity.role_arn
    ]
    assert state.volume_inventory_reads == 1


def test_validation_rejects_stack_without_managed_network_outputs() -> None:
    pending = (
        _planner()
        .plan_initial(
            user_id=_WORKSPACE_ID,
            connection_id=_CONNECTION_ID,
            account_id=_ACCOUNT_ID,
            external_id=SecretStr(_EXTERNAL_ID),
        )
        .pending
    )
    state = _AwsState()
    state.stacks[pending.stack_name] = _stack(_active(), include_network_outputs=False)

    with pytest.raises(AwsProviderControlError, match="managed network outputs"):
        Boto3AwsAccountConnectionValidator(state.sessions).validate_authorization(
            AwsAccountAuthorizationValidationInput(
                pending=pending,
                external_id=SecretStr(_EXTERNAL_ID),
            )
        )

    assert state.roles == {}
    assert state.profiles == {}


@pytest.mark.parametrize(
    "stack_status",
    [
        "ROLLBACK_COMPLETE",
        "DELETE_COMPLETE",
        "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS",
        "UPDATE_ROLLBACK_COMPLETE",
        "IMPORT_COMPLETE",
    ],
)
def test_validation_rejects_non_ready_stack_states(stack_status: str) -> None:
    pending = (
        _planner()
        .plan_initial(
            user_id=_WORKSPACE_ID,
            connection_id=_CONNECTION_ID,
            account_id=_ACCOUNT_ID,
            external_id=SecretStr(_EXTERNAL_ID),
        )
        .pending
    )
    state = _AwsState()
    state.stacks[pending.stack_name] = _stack(_active(), status=stack_status)

    with pytest.raises(AwsProviderControlError, match=stack_status):
        Boto3AwsAccountConnectionValidator(state.sessions).validate_authorization(
            AwsAccountAuthorizationValidationInput(
                pending=pending,
                external_id=SecretStr(_EXTERNAL_ID),
            )
        )

    assert state.roles == {}
    assert state.profiles == {}


def test_validation_rejects_role_that_does_not_enforce_external_id() -> None:
    pending = (
        _planner()
        .plan_initial(
            user_id=_WORKSPACE_ID,
            connection_id=_CONNECTION_ID,
            account_id=_ACCOUNT_ID,
            external_id=SecretStr(_EXTERNAL_ID),
        )
        .pending
    )

    state = _AwsState()
    state.enforce_external_id = False

    with pytest.raises(AwsAccountAuthorizationValidationError) as exc_info:
        Boto3AwsAccountConnectionValidator(state.sessions).validate_authorization(
            AwsAccountAuthorizationValidationInput(
                pending=pending,
                external_id=SecretStr(_EXTERNAL_ID),
            )
        )
    assert exc_info.value.code is AwsAccountAuthorizationValidationErrorCode.ExternalIdNotEnforced


def test_existing_role_uses_same_exact_node_identity_lifecycle() -> None:
    authorization = _planner().plan_existing_role(
        user_id=_WORKSPACE_ID,
        connection_id=_CONNECTION_ID,
        account_id=_ACCOUNT_ID,
        role_arn=f"arn:aws:iam::{_ACCOUNT_ID}:role/customer-managed-compute",
        external_id=SecretStr(_EXTERNAL_ID),
    )
    state = _AwsState()
    validator = Boto3AwsAccountConnectionValidator(state.sessions)

    validation = validator.validate_existing_authorization(
        AwsExistingAccountAuthorizationValidationInput(
            authorization=authorization,
            external_id=SecretStr(_EXTERNAL_ID),
        )
    )

    assert validation.node_identity == authorization.node_identity
    assert state.volume_inventory_reads == 1
    control = Boto3AwsAccountAuthorizationControl(state.sessions)
    operation_id = "cleanup-12345678123441238123123456789abc"

    preserved = control.reconcile_authorization_cleanup(
        authorization,
        external_id=SecretStr(_EXTERNAL_ID),
        operation_id=operation_id,
        remove_node_identity=False,
    )
    assert preserved.status is AwsAccountAuthorizationCleanupStatus.Complete
    assert preserved.role_assumable is True
    assert authorization.node_identity.role_name in state.roles

    action = control.reconcile_authorization_cleanup(
        authorization,
        external_id=SecretStr(_EXTERNAL_ID),
        operation_id=operation_id,
        remove_node_identity=True,
    )
    assert action.status is AwsAccountAuthorizationCleanupStatus.ActionRequired
    assert action.error_message == "Revoke or delete the existing AWS authorization role."
    assert authorization.node_identity.role_name not in state.roles
    assert authorization.node_identity.instance_profile_name not in state.profiles

    state.revoked = True
    completed = control.reconcile_authorization_cleanup(
        authorization,
        external_id=SecretStr(_EXTERNAL_ID),
        operation_id=operation_id,
        remove_node_identity=True,
    )
    assert completed.status is AwsAccountAuthorizationCleanupStatus.Complete
    assert completed.role_assumable is False


def test_managed_cleanup_deletes_node_identity_then_exact_stack_idempotently() -> None:
    active = _active()
    state = _AwsState()
    state.stacks[active.stack_id] = _stack(active)
    state.roles[active.node_identity.role_name] = active.node_identity.role_arn
    state.role_policies[active.node_identity.role_name] = []
    state.profiles[active.node_identity.instance_profile_name] = (
        active.node_identity.instance_profile_arn,
        [active.node_identity.role_arn],
    )
    control = Boto3AwsAccountAuthorizationControl(state.sessions)
    operation_id = "cleanup-12345678123441238123123456789abc"

    requested = control.reconcile_authorization_cleanup(
        active,
        external_id=SecretStr(_EXTERNAL_ID),
        operation_id=operation_id,
        remove_node_identity=True,
    )
    assert requested.status is AwsAccountAuthorizationCleanupStatus.Pending
    assert requested.stack_status == "DELETE_IN_PROGRESS"
    assert active.node_identity.role_name not in state.roles
    assert active.node_identity.instance_profile_name not in state.profiles
    assert state.delete_requests == [(active.stack_id, operation_id)]
    assert state.node_identity_counts_at_stack_delete == [(0, 0)]

    repeated = control.reconcile_authorization_cleanup(
        active,
        external_id=SecretStr(_EXTERNAL_ID),
        operation_id=operation_id,
        remove_node_identity=True,
    )
    assert repeated.status is AwsAccountAuthorizationCleanupStatus.Pending
    assert state.delete_requests == [(active.stack_id, operation_id)]

    state.stacks.pop(active.stack_id)
    state.revoked = True
    completed = control.reconcile_authorization_cleanup(
        active,
        external_id=SecretStr(_EXTERNAL_ID),
        operation_id=operation_id,
        remove_node_identity=True,
    )
    assert completed.status is AwsAccountAuthorizationCleanupStatus.Complete
    assert completed.stack_exists is False
    assert completed.role_assumable is False


def test_abandoned_pending_cleanup_waits_until_role_is_assumable() -> None:
    pending = (
        _planner()
        .plan_initial(
            user_id=_WORKSPACE_ID,
            connection_id=_CONNECTION_ID,
            account_id=_ACCOUNT_ID,
            external_id=SecretStr(_EXTERNAL_ID),
        )
        .pending
    )
    state = _AwsState()
    state.revoked = True
    control = Boto3AwsAccountAuthorizationControl(state.sessions)
    operation_id = "cleanup-12345678123441238123123456789abc"

    waiting = control.reconcile_authorization_cleanup(
        pending,
        external_id=SecretStr(_EXTERNAL_ID),
        operation_id=operation_id,
        remove_node_identity=True,
    )
    assert waiting.status is AwsAccountAuthorizationCleanupStatus.Verifying
    assert waiting.stack_exists is None

    state.revoked = False
    active = _active()
    state.stacks[pending.stack_name] = _stack(active)
    requested = control.reconcile_authorization_cleanup(
        pending,
        external_id=SecretStr(_EXTERNAL_ID),
        operation_id=operation_id,
        remove_node_identity=True,
    )
    assert requested.status is AwsAccountAuthorizationCleanupStatus.Pending
    assert state.delete_requests == [(active.stack_id, operation_id)]


def test_delete_failed_requires_customer_action_for_exact_stack() -> None:
    active = _active()
    state = _AwsState()
    state.stacks[active.stack_id] = _stack(active, status="DELETE_FAILED")
    result = Boto3AwsAccountAuthorizationControl(state.sessions).reconcile_authorization_cleanup(
        active,
        external_id=SecretStr(_EXTERNAL_ID),
        operation_id="cleanup-12345678123441238123123456789abc",
        remove_node_identity=False,
    )

    assert result.status is AwsAccountAuthorizationCleanupStatus.ActionRequired
    assert result.stack_status == "DELETE_FAILED"
    assert result.console_url is not None
    assert parse_qs(urlparse(result.console_url).fragment.partition("?")[2])["stackId"] == [
        active.stack_id
    ]


def test_cleanup_rejects_invalid_cloudformation_operation_token() -> None:
    with pytest.raises(ValueError, match="ClientRequestToken"):
        Boto3AwsAccountAuthorizationControl(_AwsState().sessions).reconcile_authorization_cleanup(
            _active(),
            external_id=SecretStr(_EXTERNAL_ID),
            operation_id="1-invalid",
            remove_node_identity=False,
        )


def test_existing_role_carries_a_supplied_network_through_validation() -> None:
    network = AwsAccountNetwork(
        vpc_id="vpc-0123456789abcdef0",
        subnet_ids=("subnet-0123456789abcdef0", "subnet-0123456789abcdef1"),
        security_group_id="sg-0123456789abcdef0",
    )
    authorization = _planner().plan_existing_role(
        user_id=_WORKSPACE_ID,
        connection_id=_CONNECTION_ID,
        account_id=_ACCOUNT_ID,
        role_arn=f"arn:aws:iam::{_ACCOUNT_ID}:role/customer-managed-compute",
        external_id=SecretStr(_EXTERNAL_ID),
        network=network,
    )
    state = _AwsState()
    state.subnets = {
        "subnet-0123456789abcdef0": (network.vpc_id, "us-east-1a"),
        "subnet-0123456789abcdef1": (network.vpc_id, "us-east-1b"),
    }
    state.security_groups = {network.security_group_id: network.vpc_id}

    validation = Boto3AwsAccountConnectionValidator(state.sessions).validate_existing_authorization(
        AwsExistingAccountAuthorizationValidationInput(
            authorization=authorization,
            external_id=SecretStr(_EXTERNAL_ID),
        )
    )

    assert validation.network == network


def test_existing_role_network_in_one_availability_zone_is_refused() -> None:
    """The failure this catches is the one that otherwise launches fine.

    A cross-VPC subnet id fails at the first launch attempt anyway. Two subnets
    in one zone provisions, serves, and only shows itself when that zone is what
    went down, so it has to be refused where the values were entered.
    """
    network = AwsAccountNetwork(
        vpc_id="vpc-0123456789abcdef0",
        subnet_ids=("subnet-0123456789abcdef0", "subnet-0123456789abcdef1"),
        security_group_id="sg-0123456789abcdef0",
    )
    authorization = _planner().plan_existing_role(
        user_id=_WORKSPACE_ID,
        connection_id=_CONNECTION_ID,
        account_id=_ACCOUNT_ID,
        role_arn=f"arn:aws:iam::{_ACCOUNT_ID}:role/customer-managed-compute",
        external_id=SecretStr(_EXTERNAL_ID),
        network=network,
    )
    state = _AwsState()
    state.subnets = {
        "subnet-0123456789abcdef0": (network.vpc_id, "us-east-1a"),
        "subnet-0123456789abcdef1": (network.vpc_id, "us-east-1a"),
    }
    state.security_groups = {network.security_group_id: network.vpc_id}

    with pytest.raises(AwsProviderControlError, match="one availability zone"):
        Boto3AwsAccountConnectionValidator(state.sessions).validate_existing_authorization(
            AwsExistingAccountAuthorizationValidationInput(
                authorization=authorization,
                external_id=SecretStr(_EXTERNAL_ID),
            )
        )
