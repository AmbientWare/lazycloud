from lazycloud_api.config import app_config
from lazycloud_api.services.aws.ecr_auth import ECRAuthService

# Initialize with defaults from app_config
# base_role_arn can be overridden via AWS_ECR_BASE_ROLE_ARN env var
# Defaults to standard LazyCloudECRBaseRole if not provided
ecr_auth_service = ECRAuthService(
    account_id=app_config.AWS_ACCOUNT_ID,
    access_key_id=app_config.AWS_ACCESS_KEY_ID,
    secret_access_key=app_config.AWS_SECRET_ACCESS_KEY,
    region=app_config.AWS_REGION or "us-east-1",
    endpoint_url=app_config.AWS_ENDPOINT_URL,
    base_role_arn=app_config.AWS_ECR_BASE_ROLE_ARN,
)

__all__ = ["ecr_auth_service"]
