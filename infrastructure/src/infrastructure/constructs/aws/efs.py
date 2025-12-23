from aws_cdk import (
    aws_ec2 as ec2,
    aws_efs as efs,
    RemovalPolicy,
)
from constructs import Construct

from infrastructure.config.environments import EnvironmentConfig


class EfsConstruct(Construct):
    """EFS file system for persistent shared storage"""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        vpc: ec2.Vpc,
        config: EnvironmentConfig,
        eks_security_group_id: str,
    ) -> None:
        super().__init__(scope, construct_id)

        self.config = config
        self.vpc = vpc

        # Create security group for EFS
        self.security_group = ec2.SecurityGroup(
            self,
            "EfsSecurityGroup",
            vpc=vpc,
            description="Security group for EFS mount targets",
            allow_all_outbound=True,
        )

        # Allow NFS traffic from EKS cluster security group
        self.security_group.add_ingress_rule(
            peer=ec2.SecurityGroup.from_security_group_id(
                self, "EksSecurityGroup", eks_security_group_id
            ),
            connection=ec2.Port.tcp(2049),
            description="Allow NFS from EKS cluster",
        )

        # Create EFS file system
        self.file_system = efs.FileSystem(
            self,
            "FileSystem",
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
            security_group=self.security_group,
            performance_mode=efs.PerformanceMode.GENERAL_PURPOSE,
            throughput_mode=efs.ThroughputMode.ELASTIC,
            encrypted=True,
            removal_policy=RemovalPolicy.RETAIN,
            file_system_name=f"{config.org_name}-{config.environment}-efs",
        )

    @property
    def file_system_id(self) -> str:
        """Get the EFS file system ID"""
        return self.file_system.file_system_id
