import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from constructs import Construct

from infrastructure.config.environments import EnvironmentConfig


class EKSIAMRoles(Construct):
    """IAM roles for EKS cluster add-ons (CSI drivers)"""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        config: EnvironmentConfig,
    ) -> None:
        super().__init__(scope, construct_id)

        self.config = config

        # Create EKS-related IAM roles
        self.ebs_csi_role = self._create_ebs_csi_role()
        self.efs_csi_role = self._create_efs_csi_role()

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

    @property
    def ebs_csi_role_arn(self) -> str:
        """Get the EBS CSI role ARN"""
        return self.ebs_csi_role.attr_arn
