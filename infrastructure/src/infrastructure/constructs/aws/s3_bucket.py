import aws_cdk as cdk
from aws_cdk import (
    aws_s3 as s3,
)
from constructs import Construct

from infrastructure.config.environments import EnvironmentConfig


class S3BucketConstruct(Construct):
    """S3 bucket for application data with environment-specific configuration"""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        config: EnvironmentConfig,
        bucket_name: str | None = None,
    ) -> None:
        super().__init__(scope, construct_id)

        self._bucket_name = bucket_name or f"{config.environment}-{config.org_name}-s3"
        self.config = config

        # Create S3 bucket
        self.bucket = self._create_s3_bucket()

    def _create_s3_bucket(self) -> s3.Bucket:
        """Create S3 bucket for application data"""
        # Different configurations based on environment
        if self.config.environment == "dev":
            noncurrent_version_days = 30
            multipart_cleanup_days = 1
            removal_policy = cdk.RemovalPolicy.DESTROY
        else:
            noncurrent_version_days = 90
            multipart_cleanup_days = 7
            removal_policy = cdk.RemovalPolicy.RETAIN

        return s3.Bucket(
            self,
            "AppBucket",
            bucket_name=self._bucket_name,
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            versioned=True,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="DeleteOldVersions",
                    noncurrent_version_expiration=cdk.Duration.days(
                        noncurrent_version_days
                    ),
                    abort_incomplete_multipart_upload_after=cdk.Duration.days(
                        multipart_cleanup_days
                    ),
                )
            ],
            removal_policy=removal_policy,
        )

    @property
    def bucket_name(self) -> str:
        """Get bucket name"""
        return self.bucket.bucket_name

    @property
    def bucket_arn(self) -> str:
        """Get bucket ARN"""
        return self.bucket.bucket_arn

    @property
    def bucket_domain_name(self) -> str:
        """Get bucket domain name"""
        return self.bucket.bucket_domain_name
