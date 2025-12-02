import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from constructs import Construct

from infrastructure.config.environments import EnvironmentConfig


class SharedIAMRoles(Construct):
    """Shared IAM roles for all environments"""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        config: EnvironmentConfig,
    ) -> None:
        super().__init__(scope, construct_id)

        self.config = config

        # Create all shared IAM roles
        self.external_dns_role = self._create_external_dns_role()
        self.external_secrets_role = self._create_external_secrets_role()
        self.ebs_csi_role = self._create_ebs_csi_role()
        self.efs_csi_role = self._create_efs_csi_role()
        self.karpenter_controller_role = self._create_karpenter_controller_role()
        self.karpenter_node_role = self._create_karpenter_node_role()
        self.load_balancer_controller_role = (
            self._create_load_balancer_controller_role()
        )

    def _create_external_dns_role(self) -> iam.CfnRole:
        """
        Create IAM role for external-dns with Cloudflare API token access
        Uses AWS Secrets Manager to store the Cloudflare API token
        """

        # Trust policy for EKS Pod Identity
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

        # Permissions policy - access to Secrets Manager for Cloudflare API token
        permissions_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": [
                        "secretsmanager:GetSecretValue",
                        "secretsmanager:DescribeSecret",
                    ],
                    "Resource": f"arn:aws:secretsmanager:*:{cdk.Aws.ACCOUNT_ID}:secret:{self.config.org_name}/*",
                },
            ],
        }

        role = iam.CfnRole(
            self,
            "ExternalDNSRole",
            assume_role_policy_document=trust_policy,
            policies=[
                iam.CfnRole.PolicyProperty(
                    policy_name="ExternalDNSPolicy", policy_document=permissions_policy
                )
            ],
        )

        # Add tags
        cdk.Tags.of(role).add("Purpose", "ExternalDNS")
        cdk.Tags.of(role).add("Environment", "shared")
        cdk.Tags.of(role).add("Organization", self.config.org_name)

        return role

    def _create_external_secrets_role(self) -> iam.CfnRole:
        """
        Create IAM role for external-secrets with AWS Secrets Manager permissions
        Based on External Secrets Operator documentation: https://external-secrets.io/latest/provider/aws-secrets-manager/
        """

        # Trust policy for EKS Pod Identity
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

        # Permissions policy based on External Secrets documentation
        permissions_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": [
                        "secretsmanager:ListSecrets",
                        "secretsmanager:BatchGetSecretValue",
                    ],
                    "Resource": "*",
                },
                {
                    "Effect": "Allow",
                    "Action": [
                        "secretsmanager:GetResourcePolicy",
                        "secretsmanager:GetSecretValue",
                        "secretsmanager:DescribeSecret",
                        "secretsmanager:ListSecretVersionIds",
                    ],
                    "Resource": [
                        f"arn:aws:secretsmanager:*:{cdk.Aws.ACCOUNT_ID}:secret:{self.config.org_name}/*"
                    ],
                },
            ],
        }

        role = iam.CfnRole(
            self,
            "ExternalSecretsRole",
            assume_role_policy_document=trust_policy,
            policies=[
                iam.CfnRole.PolicyProperty(
                    policy_name="ExternalSecretsPolicy",
                    policy_document=permissions_policy,
                )
            ],
        )

        # Add tags
        cdk.Tags.of(role).add("Purpose", "ExternalSecrets")
        cdk.Tags.of(role).add("Environment", "shared")
        cdk.Tags.of(role).add("Organization", self.config.org_name)

        return role

    def _create_ebs_csi_role(self) -> iam.CfnRole:
        """
        Create IAM role for EBS CSI driver with EBS permissions
        Based on the recommended policy from AWS EBS CSI driver documentation
        """

        # Trust policy for EKS Pod Identity
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

        # Permissions policy based on AWS EBS CSI driver documentation
        permissions_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": [
                        "ec2:DescribeAvailabilityZones",
                        "ec2:DescribeInstances",
                        "ec2:DescribeSnapshots",
                        "ec2:DescribeTags",
                        "ec2:DescribeVolumes",
                        "ec2:DescribeVolumesModifications",
                    ],
                    "Resource": "*",
                },
                {
                    "Effect": "Allow",
                    "Action": ["ec2:CreateSnapshot", "ec2:ModifyVolume"],
                    "Resource": "arn:aws:ec2:*:*:volume/*",
                },
                {
                    "Effect": "Allow",
                    "Action": ["ec2:AttachVolume", "ec2:DetachVolume"],
                    "Resource": [
                        "arn:aws:ec2:*:*:volume/*",
                        "arn:aws:ec2:*:*:instance/*",
                    ],
                },
                {
                    "Effect": "Allow",
                    "Action": ["ec2:CreateVolume", "ec2:EnableFastSnapshotRestores"],
                    "Resource": "arn:aws:ec2:*:*:snapshot/*",
                },
                {
                    "Effect": "Allow",
                    "Action": ["ec2:CreateTags"],
                    "Resource": [
                        "arn:aws:ec2:*:*:volume/*",
                        "arn:aws:ec2:*:*:snapshot/*",
                    ],
                    "Condition": {
                        "StringEquals": {
                            "ec2:CreateAction": ["CreateVolume", "CreateSnapshot"]
                        }
                    },
                },
                {
                    "Effect": "Allow",
                    "Action": ["ec2:DeleteTags"],
                    "Resource": [
                        "arn:aws:ec2:*:*:volume/*",
                        "arn:aws:ec2:*:*:snapshot/*",
                    ],
                },
                {
                    "Effect": "Allow",
                    "Action": ["ec2:CreateVolume"],
                    "Resource": "arn:aws:ec2:*:*:volume/*",
                    "Condition": {
                        "StringLike": {"aws:RequestTag/ebs.csi.aws.com/cluster": "true"}
                    },
                },
                {
                    "Effect": "Allow",
                    "Action": ["ec2:CreateVolume"],
                    "Resource": "arn:aws:ec2:*:*:volume/*",
                    "Condition": {"StringLike": {"aws:RequestTag/CSIVolumeName": "*"}},
                },
                {
                    "Effect": "Allow",
                    "Action": ["ec2:DeleteVolume"],
                    "Resource": "arn:aws:ec2:*:*:volume/*",
                    "Condition": {
                        "StringLike": {
                            "ec2:ResourceTag/ebs.csi.aws.com/cluster": "true"
                        }
                    },
                },
                {
                    "Effect": "Allow",
                    "Action": ["ec2:DeleteVolume"],
                    "Resource": "arn:aws:ec2:*:*:volume/*",
                    "Condition": {"StringLike": {"ec2:ResourceTag/CSIVolumeName": "*"}},
                },
                {
                    "Effect": "Allow",
                    "Action": ["ec2:DeleteVolume"],
                    "Resource": "arn:aws:ec2:*:*:volume/*",
                    "Condition": {
                        "StringLike": {
                            "ec2:ResourceTag/kubernetes.io/created-for/pvc/name": "*"
                        }
                    },
                },
                {
                    "Effect": "Allow",
                    "Action": ["ec2:CreateSnapshot"],
                    "Resource": "arn:aws:ec2:*:*:snapshot/*",
                    "Condition": {
                        "StringLike": {"aws:RequestTag/CSIVolumeSnapshotName": "*"}
                    },
                },
                {
                    "Effect": "Allow",
                    "Action": ["ec2:CreateSnapshot"],
                    "Resource": "arn:aws:ec2:*:*:snapshot/*",
                    "Condition": {
                        "StringLike": {"aws:RequestTag/ebs.csi.aws.com/cluster": "true"}
                    },
                },
                {
                    "Effect": "Allow",
                    "Action": ["ec2:DeleteSnapshot"],
                    "Resource": "arn:aws:ec2:*:*:snapshot/*",
                    "Condition": {
                        "StringLike": {"ec2:ResourceTag/CSIVolumeSnapshotName": "*"}
                    },
                },
                {
                    "Effect": "Allow",
                    "Action": ["ec2:DeleteSnapshot"],
                    "Resource": "arn:aws:ec2:*:*:snapshot/*",
                    "Condition": {
                        "StringLike": {
                            "ec2:ResourceTag/ebs.csi.aws.com/cluster": "true"
                        }
                    },
                },
            ],
        }

        role = iam.CfnRole(
            self,
            "EBSCSIRole",
            assume_role_policy_document=trust_policy,
            policies=[
                iam.CfnRole.PolicyProperty(
                    policy_name="EBSCSIPolicy",
                    policy_document=permissions_policy,
                )
            ],
        )

        # Add tags
        cdk.Tags.of(role).add("Purpose", "EBSCSI")
        cdk.Tags.of(role).add("Environment", "shared")
        cdk.Tags.of(role).add("Organization", self.config.org_name)

        return role

    def _create_efs_csi_role(self) -> iam.CfnRole:
        """
        Create IAM role for EFS CSI driver with EFS permissions
        Based on the recommended policy from AWS EFS CSI driver documentation
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
                    "Sid": "AllowDescribe",
                    "Effect": "Allow",
                    "Action": [
                        "elasticfilesystem:DescribeAccessPoints",
                        "elasticfilesystem:DescribeFileSystems",
                        "elasticfilesystem:DescribeMountTargets",
                        "ec2:DescribeAvailabilityZones",
                    ],
                    "Resource": "*",
                },
                {
                    "Sid": "AllowCreateAccessPoint",
                    "Effect": "Allow",
                    "Action": ["elasticfilesystem:CreateAccessPoint"],
                    "Resource": "*",
                    "Condition": {
                        "Null": {"aws:RequestTag/efs.csi.aws.com/cluster": "false"},
                        "ForAllValues:StringEquals": {
                            "aws:TagKeys": "efs.csi.aws.com/cluster"
                        },
                    },
                },
                {
                    "Sid": "AllowTagNewAccessPoints",
                    "Effect": "Allow",
                    "Action": ["elasticfilesystem:TagResource"],
                    "Resource": "*",
                    "Condition": {
                        "StringEquals": {
                            "elasticfilesystem:CreateAction": "CreateAccessPoint"
                        },
                        "Null": {"aws:RequestTag/efs.csi.aws.com/cluster": "false"},
                        "ForAllValues:StringEquals": {
                            "aws:TagKeys": "efs.csi.aws.com/cluster"
                        },
                    },
                },
                {
                    "Sid": "AllowDeleteAccessPoint",
                    "Effect": "Allow",
                    "Action": "elasticfilesystem:DeleteAccessPoint",
                    "Resource": "*",
                    "Condition": {
                        "Null": {"aws:ResourceTag/efs.csi.aws.com/cluster": "false"}
                    },
                },
            ],
        }

        role = iam.CfnRole(
            self,
            "EFSCSIRole",
            assume_role_policy_document=trust_policy,
            policies=[
                iam.CfnRole.PolicyProperty(
                    policy_name="EFSCSIPolicy",
                    policy_document=permissions_policy,
                )
            ],
        )

        # Add tags
        cdk.Tags.of(role).add("Purpose", "EFSCSI")
        cdk.Tags.of(role).add("Environment", "shared")
        cdk.Tags.of(role).add("Organization", self.config.org_name)

        return role

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
                    "Resource": f"arn:aws:iam::{cdk.Aws.ACCOUNT_ID}:role/{self.config.org_name}-{self.config.environment}-karpenter-node",
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
                    "Resource": f"arn:aws:iam::{cdk.Aws.ACCOUNT_ID}:role/{self.config.org_name}-{self.config.environment}-karpenter-node",
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
            role_name=f"{self.config.org_name}-{self.config.environment}-karpenter-controller",
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
            role_name=f"{self.config.org_name}-{self.config.environment}-karpenter-node",
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

    @property
    def external_dns_role_arn(self) -> str:
        """Get the external DNS role ARN"""
        return self.external_dns_role.attr_arn

    @property
    def external_secrets_role_arn(self) -> str:
        """Get the external secrets role ARN"""
        return self.external_secrets_role.attr_arn

    @property
    def ebs_csi_role_arn(self) -> str:
        """Get the EBS CSI role ARN"""
        return self.ebs_csi_role.attr_arn

    @property
    def load_balancer_controller_role_arn(self) -> str:
        """Get the Load Balancer Controller role ARN"""
        return self.load_balancer_controller_role.attr_arn

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
