import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from constructs import Construct

from infrastructure.config.environments import EnvironmentConfig


class ControllerIAMRoles(Construct):
    """IAM roles for AWS controllers (Karpenter, Load Balancer Controller)"""

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
        self.load_balancer_controller_role = (
            self._create_load_balancer_controller_role()
        )

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

    def _create_load_balancer_controller_role(self) -> iam.CfnRole:
        """
        Create IAM role for AWS Load Balancer Controller
        Based on the recommended policy from AWS Load Balancer Controller documentation
        """

        trust_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "pods.eks.amazonaws.com"},
                    "Action": ["sts:AssumeRole", "sts:TagSession"],
                }
            ],
        }

        permissions_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["iam:CreateServiceLinkedRole"],
                    "Resource": "*",
                    "Condition": {
                        "StringEquals": {
                            "iam:AWSServiceName": "elasticloadbalancing.amazonaws.com"
                        }
                    },
                },
                {
                    "Effect": "Allow",
                    "Action": [
                        "ec2:DescribeAccountAttributes",
                        "ec2:DescribeAddresses",
                        "ec2:DescribeAvailabilityZones",
                        "ec2:DescribeInternetGateways",
                        "ec2:DescribeVpcs",
                        "ec2:DescribeVpcPeeringConnections",
                        "ec2:DescribeSubnets",
                        "ec2:DescribeSecurityGroups",
                        "ec2:DescribeInstances",
                        "ec2:DescribeNetworkInterfaces",
                        "ec2:DescribeTags",
                        "ec2:GetCoipPoolUsage",
                        "ec2:DescribeCoipPools",
                        "elasticloadbalancing:DescribeLoadBalancers",
                        "elasticloadbalancing:DescribeLoadBalancerAttributes",
                        "elasticloadbalancing:DescribeListeners",
                        "elasticloadbalancing:DescribeListenerAttributes",
                        "elasticloadbalancing:DescribeListenerCertificates",
                        "elasticloadbalancing:DescribeSSLPolicies",
                        "elasticloadbalancing:DescribeRules",
                        "elasticloadbalancing:DescribeTargetGroups",
                        "elasticloadbalancing:DescribeTargetGroupAttributes",
                        "elasticloadbalancing:DescribeTargetHealth",
                        "elasticloadbalancing:DescribeTags",
                        "elasticloadbalancing:DescribeTrustStores",
                    ],
                    "Resource": "*",
                },
                {
                    "Effect": "Allow",
                    "Action": [
                        "cognito-idp:DescribeUserPoolClient",
                        "acm:ListCertificates",
                        "acm:DescribeCertificate",
                        "iam:ListServerCertificates",
                        "iam:GetServerCertificate",
                        "waf-regional:GetWebACL",
                        "waf-regional:GetWebACLForResource",
                        "waf-regional:AssociateWebACL",
                        "waf-regional:DisassociateWebACL",
                        "wafv2:GetWebACL",
                        "wafv2:GetWebACLForResource",
                        "wafv2:AssociateWebACL",
                        "wafv2:DisassociateWebACL",
                        "shield:GetSubscriptionState",
                        "shield:DescribeProtection",
                        "shield:CreateProtection",
                        "shield:DeleteProtection",
                    ],
                    "Resource": "*",
                },
                {
                    "Effect": "Allow",
                    "Action": [
                        "ec2:AuthorizeSecurityGroupIngress",
                        "ec2:RevokeSecurityGroupIngress",
                    ],
                    "Resource": "*",
                },
                {
                    "Effect": "Allow",
                    "Action": ["ec2:CreateSecurityGroup"],
                    "Resource": "*",
                },
                {
                    "Effect": "Allow",
                    "Action": ["ec2:CreateTags"],
                    "Resource": "arn:aws:ec2:*:*:security-group/*",
                    "Condition": {
                        "StringEquals": {"ec2:CreateAction": "CreateSecurityGroup"},
                        "Null": {"aws:RequestTag/elbv2.k8s.aws/cluster": "false"},
                    },
                },
                {
                    "Effect": "Allow",
                    "Action": [
                        "ec2:CreateTags",
                        "ec2:DeleteTags",
                    ],
                    "Resource": "arn:aws:ec2:*:*:security-group/*",
                    "Condition": {
                        "Null": {
                            "aws:RequestTag/elbv2.k8s.aws/cluster": "true",
                            "aws:ResourceTag/elbv2.k8s.aws/cluster": "false",
                        }
                    },
                },
                {
                    "Effect": "Allow",
                    "Action": [
                        "ec2:AuthorizeSecurityGroupIngress",
                        "ec2:RevokeSecurityGroupIngress",
                        "ec2:DeleteSecurityGroup",
                    ],
                    "Resource": "*",
                    "Condition": {
                        "Null": {"aws:ResourceTag/elbv2.k8s.aws/cluster": "false"}
                    },
                },
                {
                    "Effect": "Allow",
                    "Action": [
                        "elasticloadbalancing:CreateLoadBalancer",
                        "elasticloadbalancing:CreateTargetGroup",
                    ],
                    "Resource": "*",
                    "Condition": {
                        "Null": {"aws:RequestTag/elbv2.k8s.aws/cluster": "false"}
                    },
                },
                {
                    "Effect": "Allow",
                    "Action": [
                        "elasticloadbalancing:CreateListener",
                        "elasticloadbalancing:DeleteListener",
                        "elasticloadbalancing:CreateRule",
                        "elasticloadbalancing:DeleteRule",
                    ],
                    "Resource": "*",
                },
                {
                    "Effect": "Allow",
                    "Action": [
                        "elasticloadbalancing:AddTags",
                        "elasticloadbalancing:RemoveTags",
                    ],
                    "Resource": [
                        "arn:aws:elasticloadbalancing:*:*:targetgroup/*/*",
                        "arn:aws:elasticloadbalancing:*:*:loadbalancer/net/*/*",
                        "arn:aws:elasticloadbalancing:*:*:loadbalancer/app/*/*",
                    ],
                    "Condition": {
                        "Null": {
                            "aws:RequestTag/elbv2.k8s.aws/cluster": "true",
                            "aws:ResourceTag/elbv2.k8s.aws/cluster": "false",
                        }
                    },
                },
                {
                    "Effect": "Allow",
                    "Action": [
                        "elasticloadbalancing:AddTags",
                        "elasticloadbalancing:RemoveTags",
                    ],
                    "Resource": [
                        "arn:aws:elasticloadbalancing:*:*:listener/net/*/*/*",
                        "arn:aws:elasticloadbalancing:*:*:listener/app/*/*/*",
                        "arn:aws:elasticloadbalancing:*:*:listener-rule/net/*/*/*",
                        "arn:aws:elasticloadbalancing:*:*:listener-rule/app/*/*/*",
                    ],
                },
                {
                    "Effect": "Allow",
                    "Action": [
                        "elasticloadbalancing:ModifyLoadBalancerAttributes",
                        "elasticloadbalancing:SetIpAddressType",
                        "elasticloadbalancing:SetSecurityGroups",
                        "elasticloadbalancing:SetSubnets",
                        "elasticloadbalancing:DeleteLoadBalancer",
                        "elasticloadbalancing:ModifyTargetGroup",
                        "elasticloadbalancing:ModifyTargetGroupAttributes",
                        "elasticloadbalancing:DeleteTargetGroup",
                    ],
                    "Resource": "*",
                    "Condition": {
                        "Null": {"aws:ResourceTag/elbv2.k8s.aws/cluster": "false"}
                    },
                },
                {
                    "Effect": "Allow",
                    "Action": [
                        "elasticloadbalancing:AddTags",
                    ],
                    "Resource": [
                        "arn:aws:elasticloadbalancing:*:*:targetgroup/*/*",
                        "arn:aws:elasticloadbalancing:*:*:loadbalancer/net/*/*",
                        "arn:aws:elasticloadbalancing:*:*:loadbalancer/app/*/*",
                    ],
                    "Condition": {
                        "StringEquals": {
                            "elasticloadbalancing:CreateAction": [
                                "CreateTargetGroup",
                                "CreateLoadBalancer",
                            ]
                        },
                        "Null": {"aws:RequestTag/elbv2.k8s.aws/cluster": "false"},
                    },
                },
                {
                    "Effect": "Allow",
                    "Action": [
                        "elasticloadbalancing:RegisterTargets",
                        "elasticloadbalancing:DeregisterTargets",
                    ],
                    "Resource": "arn:aws:elasticloadbalancing:*:*:targetgroup/*/*",
                },
                {
                    "Effect": "Allow",
                    "Action": [
                        "elasticloadbalancing:SetWebAcl",
                        "elasticloadbalancing:ModifyListener",
                        "elasticloadbalancing:AddListenerCertificates",
                        "elasticloadbalancing:RemoveListenerCertificates",
                        "elasticloadbalancing:ModifyRule",
                    ],
                    "Resource": "*",
                },
            ],
        }

        role = iam.CfnRole(
            self,
            "LoadBalancerControllerRole",
            assume_role_policy_document=trust_policy,
            policies=[
                iam.CfnRole.PolicyProperty(
                    policy_name="LoadBalancerControllerPolicy",
                    policy_document=permissions_policy,
                )
            ],
        )

        # Add tags
        cdk.Tags.of(role).add("Purpose", "LoadBalancerController")
        cdk.Tags.of(role).add("Environment", "shared")
        cdk.Tags.of(role).add("Organization", self.config.org_name)

        return role

    @property
    def load_balancer_controller_role_arn(self) -> str:
        """Get the Load Balancer Controller role ARN"""
        return self.load_balancer_controller_role.attr_arn
