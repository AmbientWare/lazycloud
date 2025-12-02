from datetime import datetime, timedelta, timezone

import boto3
from botocore.config import Config
from loguru import logger


class AWSMetricsService:
    """Get metrics from AWS APIs (CloudWatch, etc). Extensible for ingress/egress."""

    def __init__(
        self,
        region: str,
        access_key_id: str,
        secret_access_key: str,
        endpoint_url: str | None = None,
    ):
        self.region = region
        self.endpoint_url = endpoint_url

        # Disabled in Minikube/LocalStack (no real CloudWatch)
        is_localstack = endpoint_url and (
            "localhost" in endpoint_url or "localstack" in endpoint_url
        )
        self.enabled = bool(access_key_id and secret_access_key and not is_localstack)

        if not self.enabled:
            logger.info(
                "AWSMetricsService disabled (no credentials or using LocalStack)"
            )
            self._cloudwatch = None
            return

        client_kwargs = {
            "region_name": self.region,
            "aws_access_key_id": access_key_id,
            "aws_secret_access_key": secret_access_key,
        }

        if endpoint_url:
            client_kwargs["endpoint_url"] = endpoint_url
            client_kwargs["config"] = Config(
                region_name=self.region,
                signature_version="v4",
                retries={"max_attempts": 3, "mode": "standard"},
            )

        self._cloudwatch = boto3.client("cloudwatch", **client_kwargs)
        logger.info(f"AWSMetricsService enabled for region {region}")

    async def get_efs_storage_bytes(self, file_system_id: str) -> float | None:
        """Get EFS actual usage from CloudWatch StorageBytes metric. Raises on API failure."""
        if not self.enabled or not self._cloudwatch:
            return None

        now = datetime.now(timezone.utc)
        start_time = now - timedelta(minutes=30)

        response = self._cloudwatch.get_metric_statistics(
            Namespace="AWS/EFS",
            MetricName="StorageBytes",
            Dimensions=[
                {"Name": "FileSystemId", "Value": file_system_id},
                {"Name": "StorageClass", "Value": "Total"},
            ],
            StartTime=start_time,
            EndTime=now,
            Period=900,
            Statistics=["Average"],
        )

        datapoints = response.get("Datapoints", [])
        if not datapoints:
            return None

        latest = max(datapoints, key=lambda x: x["Timestamp"])
        storage_bytes = latest.get("Average", 0.0)

        logger.debug(
            f"EFS {file_system_id} storage: {storage_bytes / (1024**3):.2f} GB"
        )
        return storage_bytes
