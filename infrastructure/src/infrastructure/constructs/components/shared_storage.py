import aws_cdk as cdk
from constructs import Construct

from infrastructure.config.environments import EnvironmentConfig
from infrastructure.constructs.aws import S3BucketConstruct


class SharedStorage(Construct):
    """Storage-related services like S3"""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        config: EnvironmentConfig,
        vpc,
    ) -> None:
        super().__init__(scope, construct_id)

        self.config = config

        # Create S3 Buckets
        self.s3_bucket_construct = S3BucketConstruct(
            self,
            "S3Bucket",
            config=self.config,
        )

    @property
    def bucket_name(self):
        """Get the S3 bucket name"""
        return self.s3_bucket_construct.bucket_name

    def create_outputs(self, stack: cdk.Stack) -> None:
        """Create CloudFormation outputs for storage services"""

        # S3 Outputs
        cdk.CfnOutput(
            stack,
            "S3BucketName",
            value=self.bucket_name,
            description="S3 Bucket Name",
        )
