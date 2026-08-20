"""The permissions a connected AWS account grants, defined once.

Three things need this same set and used to be at risk of disagreeing about it:
the CloudFormation template a customer deploys, the role the platform's own
account holds for its own fleet, and the document a customer needs when they
bring their own role instead of letting us create one.

A second copy of a permission set does not fail when it drifts. It fails later,
as a launch that is denied an action the copy never granted, and the error names
the API call rather than the policy that is behind. So the statements live here
and are rendered per audience.

Rendering differs only in how an ARN is spelled. CloudFormation cannot know the
account or partition until the stack runs, so it defers them to `Fn::Sub` over
pseudo-parameters; everywhere else they are known and written out. `ArnRenderer`
is that difference and the only thing that varies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pydantic import JsonValue

MANAGED_TAG_KEY = "cloud-pool:managed-by"
MANAGED_TAG_VALUE = "control-plane"

_MANAGED_RESOURCE_TAG: JsonValue = {
    "StringEquals": {f"ec2:ResourceTag/{MANAGED_TAG_KEY}": MANAGED_TAG_VALUE}
}
_MANAGED_REQUEST_TAG: JsonValue = {
    "StringEquals": {f"aws:RequestTag/{MANAGED_TAG_KEY}": MANAGED_TAG_VALUE}
}


class ArnRenderer(Protocol):
    """Spells one ARN for the audience the policy is being rendered for."""

    def arn(self, template: str) -> JsonValue: ...


@dataclass(frozen=True, slots=True)
class CloudFormationArns:
    """Defers every field to the stack, which is the only place they are known."""

    node_role_name: str = "${NodeRoleName}"
    node_instance_profile_name: str = "${NodeInstanceProfileName}"

    def arn(self, template: str) -> JsonValue:
        return {
            "Fn::Sub": template.format(
                partition="${AWS::Partition}",
                region="${AWS::Region}",
                account_id="${AWS::AccountId}",
                node_role_name=self.node_role_name,
                node_instance_profile_name=self.node_instance_profile_name,
            )
        }


@dataclass(frozen=True, slots=True)
class ConcreteArns:
    """Writes every field out, for an account whose identity is already known."""

    partition: str
    region: str
    account_id: str
    node_role_name: str
    node_instance_profile_name: str

    def arn(self, template: str) -> JsonValue:
        return template.format(
            partition=self.partition,
            region=self.region,
            account_id=self.account_id,
            node_role_name=self.node_role_name,
            node_instance_profile_name=self.node_instance_profile_name,
        )


_INSTANCE = "arn:{partition}:ec2:{region}:{account_id}:instance/*"
_VOLUME = "arn:{partition}:ec2:{region}:{account_id}:volume/*"
_LAUNCH_TEMPLATE = "arn:{partition}:ec2:{region}:{account_id}:launch-template/*"
_SECURITY_GROUP = "arn:{partition}:ec2:{region}:{account_id}:security-group/*"
_SUBNET = "arn:{partition}:ec2:{region}:{account_id}:subnet/*"
_IMAGE = "arn:{partition}:ec2:{region}:*:image/*"
_NETWORK_INTERFACE = "arn:{partition}:ec2:{region}:{account_id}:network-interface/*"
_NODE_ROLE = "arn:{partition}:iam::{account_id}:role/{node_role_name}"
_NODE_PROFILE = "arn:{partition}:iam::{account_id}:instance-profile/{node_instance_profile_name}"
_AUTOSCALING_SERVICE_ROLE = (
    "arn:{partition}:iam::*:role/aws-service-role/"
    "autoscaling.amazonaws.com/AWSServiceRoleForAutoScaling"
)


def connection_role_statements(
    arns: ArnRenderer,
    *,
    authorization_stack: JsonValue | None = None,
) -> list[JsonValue]:
    """Every action the control plane takes inside a connected account.

    `authorization_stack` names the stack the connection may clean up after
    itself, and belongs only to a managed-stack authorization. An account
    connected by an existing role has no stack to describe or delete, so the
    statement is absent rather than granted against nothing.
    """
    statements: list[JsonValue] = [
        {
            "Sid": "Inventory",
            "Effect": "Allow",
            "Action": [
                "autoscaling:DescribeAutoScalingGroups",
                "ec2:DescribeAvailabilityZones",
                "ec2:DescribeInstances",
                "ec2:DescribeInternetGateways",
                "ec2:DescribeLaunchTemplates",
                "ec2:DescribeLaunchTemplateVersions",
                "ec2:DescribeRegions",
                "ec2:DescribeRouteTables",
                "ec2:DescribeSecurityGroups",
                "ec2:DescribeSubnets",
                "ec2:DescribeVolumes",
                "ec2:DescribeVpcs",
                "sts:GetCallerIdentity",
            ],
            "Resource": "*",
        }
    ]
    if authorization_stack is not None:
        statements.append(
            {
                "Sid": "ManageCurrentAuthorization",
                "Effect": "Allow",
                "Action": [
                    "cloudformation:DeleteStack",
                    "cloudformation:DescribeStackEvents",
                    "cloudformation:DescribeStacks",
                ],
                "Resource": authorization_stack,
            }
        )
    statements.extend(
        [
            {
                "Sid": "CreateTaggedLaunchTemplates",
                "Effect": "Allow",
                "Action": "ec2:CreateLaunchTemplate",
                "Resource": "*",
                "Condition": _MANAGED_REQUEST_TAG,
            },
            {
                "Sid": "TagOnlyDuringOwnedCreate",
                "Effect": "Allow",
                "Action": "ec2:CreateTags",
                "Resource": "*",
                "Condition": {
                    "StringEquals": {
                        f"aws:RequestTag/{MANAGED_TAG_KEY}": MANAGED_TAG_VALUE,
                        "ec2:CreateAction": "CreateLaunchTemplate",
                    }
                },
            },
            {
                "Sid": "CreateTaggedAutoScalingGroups",
                "Effect": "Allow",
                "Action": "autoscaling:CreateAutoScalingGroup",
                "Resource": "*",
                "Condition": _MANAGED_REQUEST_TAG,
            },
            {
                "Sid": "ManageTaggedLaunchTemplates",
                "Effect": "Allow",
                "Action": [
                    "ec2:CreateLaunchTemplateVersion",
                    "ec2:DeleteLaunchTemplate",
                    "ec2:ModifyLaunchTemplate",
                ],
                "Resource": "*",
                "Condition": _MANAGED_RESOURCE_TAG,
            },
            {
                "Sid": "ManageTaggedAutoScalingGroups",
                "Effect": "Allow",
                "Action": [
                    "autoscaling:DeleteAutoScalingGroup",
                    "autoscaling:SetDesiredCapacity",
                    "autoscaling:TerminateInstanceInAutoScalingGroup",
                    "autoscaling:UpdateAutoScalingGroup",
                ],
                "Resource": "*",
                "Condition": {
                    "StringEquals": {
                        f"autoscaling:ResourceTag/{MANAGED_TAG_KEY}": MANAGED_TAG_VALUE
                    }
                },
            },
            {
                "Sid": "RunTaggedInstanceResources",
                "Effect": "Allow",
                "Action": "ec2:RunInstances",
                "Resource": [arns.arn(_INSTANCE), arns.arn(_VOLUME)],
                "Condition": _MANAGED_REQUEST_TAG,
            },
            {
                "Sid": "UseManagedInstanceLaunchResources",
                "Effect": "Allow",
                "Action": "ec2:RunInstances",
                "Resource": [
                    arns.arn(_LAUNCH_TEMPLATE),
                    arns.arn(_SECURITY_GROUP),
                    arns.arn(_SUBNET),
                ],
                "Condition": _MANAGED_RESOURCE_TAG,
            },
            {
                "Sid": "UseRegionalImagesForInstances",
                "Effect": "Allow",
                "Action": "ec2:RunInstances",
                "Resource": arns.arn(_IMAGE),
            },
            {
                "Sid": "CreateInstanceNetworkInterfaces",
                "Effect": "Allow",
                "Action": "ec2:RunInstances",
                "Resource": arns.arn(_NETWORK_INTERFACE),
            },
            {
                "Sid": "TagManagedInstancesOnLaunch",
                "Effect": "Allow",
                "Action": "ec2:CreateTags",
                "Resource": [arns.arn(_INSTANCE), arns.arn(_VOLUME)],
                "Condition": {
                    "StringEquals": {
                        f"aws:RequestTag/{MANAGED_TAG_KEY}": MANAGED_TAG_VALUE,
                        "ec2:CreateAction": "RunInstances",
                    }
                },
            },
            {
                "Sid": "CreateAutoScalingServiceRole",
                "Effect": "Allow",
                "Action": "iam:CreateServiceLinkedRole",
                "Resource": arns.arn(_AUTOSCALING_SERVICE_ROLE),
                "Condition": {"StringEquals": {"iam:AWSServiceName": "autoscaling.amazonaws.com"}},
            },
            {
                "Sid": "CreateManagedNodeIdentity",
                "Effect": "Allow",
                "Action": ["iam:CreateInstanceProfile", "iam:CreateRole"],
                "Resource": [arns.arn(_NODE_PROFILE), arns.arn(_NODE_ROLE)],
                "Condition": _MANAGED_REQUEST_TAG,
            },
            {
                "Sid": "ManageOwnedNodeIdentity",
                "Effect": "Allow",
                "Action": [
                    "iam:AddRoleToInstanceProfile",
                    "iam:DeleteInstanceProfile",
                    "iam:DeleteRole",
                    "iam:DeleteRolePolicy",
                    "iam:GetInstanceProfile",
                    "iam:GetRole",
                    "iam:ListRolePolicies",
                    "iam:PutRolePolicy",
                    "iam:RemoveRoleFromInstanceProfile",
                    "iam:TagInstanceProfile",
                    "iam:TagRole",
                    "iam:UntagInstanceProfile",
                    "iam:UntagRole",
                ],
                "Resource": [arns.arn(_NODE_PROFILE), arns.arn(_NODE_ROLE)],
            },
            {
                "Sid": "PassOwnedNodeRole",
                "Effect": "Allow",
                "Action": "iam:PassRole",
                "Resource": arns.arn(_NODE_ROLE),
                "Condition": {"StringEquals": {"iam:PassedToService": "ec2.amazonaws.com"}},
            },
        ]
    )
    return statements


def connection_role_policy(
    arns: ArnRenderer,
    *,
    authorization_stack: JsonValue | None = None,
) -> dict[str, JsonValue]:
    """The statements as a complete IAM policy document."""
    return {
        "Version": "2012-10-17",
        "Statement": connection_role_statements(arns, authorization_stack=authorization_stack),
    }


__all__ = [
    "MANAGED_TAG_KEY",
    "MANAGED_TAG_VALUE",
    "ArnRenderer",
    "CloudFormationArns",
    "ConcreteArns",
    "connection_role_policy",
    "connection_role_statements",
]
