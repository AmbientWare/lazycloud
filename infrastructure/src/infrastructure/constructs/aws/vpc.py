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

        # Add secondary CIDR for pod networking
        self._add_secondary_cidr()

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

    def _add_secondary_cidr(self) -> None:
        """Add secondary CIDR block for pod networking"""
        if self.config.availability_zones is None:
            raise ValueError("Availability zones are required")

        # Add secondary CIDR block using RFC 6598 space for pod networking
        cidr_block = ec2.CfnVPCCidrBlock(
            self,
            "SecondaryCIDR",
            vpc_id=self.vpc.vpc_id,
            cidr_block="100.64.0.0/16",
        )

        # Create pod subnets in each AZ using the secondary CIDR
        # Using /19 gives us 8,192 IPs per AZ (3 AZs = ~24,576 pod IPs)
        for i, az in enumerate(self.config.availability_zones):
            # Calculate CIDR for this AZ: 100.64.0.0/19, 100.64.32.0/19, 100.64.64.0/19
            cidr_offset = i * 32  # Each /19 uses 32 in the third octet
            subnet_cidr = f"100.64.{cidr_offset}.0/19"

            subnet = ec2.PrivateSubnet(
                self,
                f"PodSubnet{i + 1}",
                vpc_id=self.vpc.vpc_id,
                availability_zone=az,
                cidr_block=subnet_cidr,
            )

            # Ensure subnet waits for secondary CIDR to be attached
            subnet.node.add_dependency(cidr_block)

            # Tag for EKS pod networking
            cdk.Tags.of(subnet).add("kubernetes.io/role/internal-elb", "1")
            cdk.Tags.of(subnet).add(
                "kubernetes.io/cluster/"
                + f"{self.config.org_name}-{self.config.environment}-{self.config.aws_region}-eks",
                "shared",
            )

    def _tag_subnets_for_karpenter(self) -> None:
        """Tag private subnets for Karpenter discovery"""
        cluster_name = f"{self.config.org_name}-{self.config.environment}-{self.config.aws_region}-eks"

        # Tag private subnets for Karpenter discovery
        for subnet in self.vpc.private_subnets:
            cdk.Tags.of(subnet).add("karpenter.sh/discovery", cluster_name)
