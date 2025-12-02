import aws_cdk as cdk
from aws_cdk import (
    aws_ec2 as ec2,
    aws_elasticache as elasticache,
)
from constructs import Construct

from infrastructure.config.environments import EnvironmentConfig


class ElastiCacheConstruct(Construct):
    """ElastiCache Serverless cluster for Redis/Valkey"""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        vpc: ec2.Vpc,
        elasticache_max_data_storage: int,
        elasticache_max_ecpu_per_second: int,
        config: EnvironmentConfig,
    ) -> None:
        super().__init__(scope, construct_id)

        self.config = config
        self.vpc = vpc
        self.elasticache_max_data_storage = elasticache_max_data_storage
        self.elasticache_max_ecpu_per_second = elasticache_max_ecpu_per_second

        # Create security group for ElastiCache
        self.security_group = self._create_security_group()

        # Create subnet group for ElastiCache
        self.subnet_group = self._create_subnet_group()

        # Create ElastiCache serverless cluster
        self.cluster = self._create_cluster()

    def _create_security_group(self) -> ec2.SecurityGroup:
        """Create security group for ElastiCache cluster"""
        sg = ec2.SecurityGroup(
            self,
            "SecurityGroup",
            vpc=self.vpc,
            description="Security group for ElastiCache cluster",
            allow_all_outbound=True,
        )

        # Allow inbound Redis traffic from VPC
        sg.add_ingress_rule(
            peer=ec2.Peer.ipv4(self.vpc.vpc_cidr_block),
            connection=ec2.Port.tcp(6379),
            description="Allow Redis traffic from VPC",
        )

        return sg

    def _create_subnet_group(self) -> elasticache.CfnSubnetGroup:
        """Create subnet group for ElastiCache"""
        # ElastiCache Serverless requires 2-3 subnets in different AZs
        private_subnets = self.vpc.private_subnets
        if len(private_subnets) < 2:
            raise ValueError(
                f"ElastiCache Serverless requires at least 2 subnets, but only {len(private_subnets)} available"
            )

        # Use first 3 subnets (or all if less than 3)
        selected_subnets = private_subnets[:3]

        return elasticache.CfnSubnetGroup(
            self,
            "SubnetGroup",
            description="Subnet group for ElastiCache cluster",
            subnet_ids=[subnet.subnet_id for subnet in selected_subnets],
            cache_subnet_group_name=f"{self.config.org_name}-{self.config.environment}-{self.config.aws_region}-cache-subnet-group",
        )

    def _create_cluster(self) -> elasticache.CfnServerlessCache:
        """Create ElastiCache serverless cluster"""
        # ElastiCache Serverless requires 2-3 subnets in different AZs
        private_subnets = self.vpc.private_subnets
        if len(private_subnets) < 2:
            raise ValueError(
                f"ElastiCache Serverless requires at least 2 subnets, but only {len(private_subnets)} available"
            )

        # Use first 3 subnets (or all if less than 3)
        selected_subnets = private_subnets[:3]

        return elasticache.CfnServerlessCache(
            self,
            "ServerlessCache",
            serverless_cache_name=f"{self.config.org_name}-{self.config.environment}-{self.config.aws_region}-cache",
            engine="valkey",  # Use Valkey (Redis-compatible)
            description=f"Valkey serverless cache for {self.config.environment}",
            cache_usage_limits=elasticache.CfnServerlessCache.CacheUsageLimitsProperty(
                data_storage=elasticache.CfnServerlessCache.DataStorageProperty(
                    maximum=self.elasticache_max_data_storage,
                    unit="GB",
                ),
                ecpu_per_second=elasticache.CfnServerlessCache.ECPUPerSecondProperty(
                    maximum=self.elasticache_max_ecpu_per_second,
                ),
            ),
            security_group_ids=[self.security_group.security_group_id],
            subnet_ids=[subnet.subnet_id for subnet in selected_subnets],
            tags=[
                cdk.CfnTag(key="Environment", value=self.config.environment),
                cdk.CfnTag(key="Organization", value=self.config.org_name),
                cdk.CfnTag(key="Service", value="ElastiCache"),
            ],
        )

    @property
    def endpoint(self) -> str:
        """Get the cluster endpoint"""
        return self.cluster.attr_endpoint_address

    @property
    def port(self) -> str:
        """Get the cluster port"""
        return self.cluster.attr_endpoint_port

    @property
    def connection_string(self) -> str:
        """Get the connection string"""
        return f"{self.endpoint}:{self.port}"
