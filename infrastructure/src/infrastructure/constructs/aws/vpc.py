import aws_cdk as cdk
from aws_cdk import (
    aws_ec2 as ec2,
)
from constructs import Construct

from infrastructure.config.environments import EnvironmentConfig


class VpcConstruct(Construct):
    """VPC with public and private subnets"""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        config: EnvironmentConfig,
    ) -> None:
        super().__init__(scope, construct_id)

        self.config = config

        # Create VPC
        self.vpc = self._create_vpc()

        # Tag subnets for Karpenter discovery
        self._tag_subnets_for_karpenter()

    def _create_vpc(self) -> ec2.Vpc:
        """Create VPC with public and private subnets"""
        if self.config.availability_zones is None:
            raise ValueError("Availability zones are required")

        return ec2.Vpc(
            self,
            "VPC",
            ip_addresses=ec2.IpAddresses.cidr(self.config.vpc_cidr),
            availability_zones=self.config.availability_zones,
            nat_gateways=1
            if self.config.single_nat_gateway
            else len(self.config.availability_zones),
            enable_dns_hostnames=self.config.enable_dns_hostnames,
            enable_dns_support=self.config.enable_dns_support,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                ),
                ec2.SubnetConfiguration(
                    name="Private",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=24,
                ),
            ],
        )

    @property
    def vpc_id(self) -> str:
        """Get VPC ID"""
        return self.vpc.vpc_id

    @property
    def vpc_cidr_block(self) -> str:
        """Get VPC CIDR block"""
        return self.vpc.vpc_cidr_block

    @property
    def private_subnets(self) -> list[ec2.ISubnet]:
        """Get private subnets"""
        return self.vpc.private_subnets

    @property
    def public_subnets(self) -> list[ec2.ISubnet]:
        """Get public subnets"""
        return self.vpc.public_subnets

    def _tag_subnets_for_karpenter(self) -> None:
        """Tag private subnets for Karpenter discovery"""
        cluster_name = f"{self.config.org_name}-{self.config.environment}-{self.config.aws_region}-eks"

        # Tag private subnets for Karpenter discovery
        for subnet in self.vpc.private_subnets:
            cdk.Tags.of(subnet).add("karpenter.sh/discovery", cluster_name)
