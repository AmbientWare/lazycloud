import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from constructs import Construct

from infrastructure.config.environments import EnvironmentConfig


class ControllerIAMRoles(Construct):
    """IAM roles for AWS controllers (Karpenter)"""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        config: EnvironmentConfig,
    ) -> None:
        super().__init__(scope, construct_id)

        self.config = config

        # Create controller IAM roles
        self.karpenter_controller_role = self._create_karpenter_controller_role()
        self.karpenter_node_role = self._create_karpenter_node_role()

    def _create_karpenter_controller_role(self) -> iam.CfnRole:
        """
        Create IAM role for Karpenter controller with EC2 permissions
        Based on the recommended policy from Karpenter documentation
        Uses EKS Pod Identity for authentication
        """

        trust_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": ["pods.eks.amazonaws.com"]},
                    "Action": ["sts:AssumeRole", "sts:TagSession"],
                }
            ],
        }

        permissions_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "Karpenter",
                    "Effect": "Allow",
                    "Action": [
                        "ssm:GetParameter",
                        "ec2:DescribeImages",
                        "ec2:RunInstances",
                        "ec2:DescribeSubnets",
                        "ec2:DescribeSecurityGroups",
                        "ec2:DescribeLaunchTemplates",
                        "ec2:DescribeInstances",
                        "ec2:DescribeInstanceTypes",
                        "ec2:DescribeInstanceTypeOfferings",
                        "ec2:DescribeAvailabilityZones",
                        "ec2:DeleteLaunchTemplate",
                        "ec2:CreateTags",
                        "ec2:CreateLaunchTemplate",
                        "ec2:CreateFleet",
                        "ec2:DescribeSpotPriceHistory",
                        "pricing:GetProducts",
                        "eks:DescribeCluster",
                    ],
                    "Resource": "*",
                },
                {
                    "Sid": "TerminateInstances",
                    "Effect": "Allow",
                    "Action": "ec2:TerminateInstances",
                    "Resource": "*",
                    "Condition": {
                        "StringLike": {"ec2:ResourceTag/karpenter.sh/nodepool": "*"}
                    },
                },
                {
                    "Sid": "PassNodeIAMRole",
                    "Effect": "Allow",
                    "Action": "iam:PassRole",
                    "Resource": f"arn:aws:iam::{cdk.Aws.ACCOUNT_ID}:role/{self.config.org_name}-karpenter-node",
                },
                {
                    "Sid": "CreateNodeInstanceProfile",
                    "Effect": "Allow",
                    "Action": [
                        "iam:CreateInstanceProfile",
                        "iam:TagInstanceProfile",
                        "iam:AddRoleToInstanceProfile",
                        "iam:RemoveRoleFromInstanceProfile",
                        "iam:DeleteInstanceProfile",
                        "iam:GetInstanceProfile",
                        "iam:ListInstanceProfiles",
                    ],
                    "Resource": "*",
                },
                {
                    "Sid": "TagNodeInstanceProfile",
                    "Effect": "Allow",
                    "Action": ["iam:TagRole"],
                    "Resource": f"arn:aws:iam::{cdk.Aws.ACCOUNT_ID}:role/{self.config.org_name}-karpenter-node",
                },
                {
                    "Sid": "InterruptionQueue",
                    "Effect": "Allow",
                    "Action": [
                        "sqs:DeleteMessage",
                        "sqs:GetQueueAttributes",
                        "sqs:GetQueueUrl",
                        "sqs:ReceiveMessage",
                    ],
                    "Resource": f"arn:aws:sqs:*:{cdk.Aws.ACCOUNT_ID}:*",
                },
                {
                    "Sid": "CreateSpotServiceLinkedRole",
                    "Effect": "Allow",
                    "Action": "iam:CreateServiceLinkedRole",
                    "Resource": "*",
                    "Condition": {
                        "StringEquals": {"iam:AWSServiceName": "spot.amazonaws.com"}
                    },
                },
            ],
        }

        role = iam.CfnRole(
            self,
            "KarpenterControllerRole",
            role_name=f"{self.config.org_name}-karpenter-controller",
            assume_role_policy_document=trust_policy,
            policies=[
                iam.CfnRole.PolicyProperty(
                    policy_name="KarpenterControllerPolicy",
                    policy_document=permissions_policy,
                )
            ],
        )

        # Add tags
        cdk.Tags.of(role).add("Purpose", "KarpenterController")
        cdk.Tags.of(role).add("Environment", "shared")
        cdk.Tags.of(role).add("Organization", self.config.org_name)

        return role

    def _create_karpenter_node_role(self) -> iam.CfnRole:
        """
        Create IAM role for Karpenter node instances
        """

        trust_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": ["ec2.amazonaws.com"]},
                    "Action": ["sts:AssumeRole"],
                }
            ],
        }

        role = iam.CfnRole(
            self,
            "KarpenterNodeRole",
            role_name=f"{self.config.org_name}-karpenter-node",
            assume_role_policy_document=trust_policy,
            managed_policy_arns=[
                "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
                "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
                "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
                "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
            ],
        )

        # Add tags
        cdk.Tags.of(role).add("Purpose", "KarpenterNode")
        cdk.Tags.of(role).add("Environment", "shared")
        cdk.Tags.of(role).add("Organization", self.config.org_name)

        return role
