from lazycloud_api.config import app_config
from lazycloud_api.services.cloudflare import CloudflareService
from lazycloud_api.services.ecr_auth import ECRAuthService

ecr_auth_service = ECRAuthService(
    account_id=app_config.AWS_ACCOUNT_ID,
    access_key_id=app_config.AWS_ACCESS_KEY_ID,
    secret_access_key=app_config.AWS_SECRET_ACCESS_KEY,
    region=app_config.AWS_REGION,
    base_role_arn=app_config.AWS_ECR_BASE_ROLE_ARN,
    endpoint_url=app_config.AWS_ENDPOINT_URL,
)
cloudflare_service = CloudflareService(
    api_key=app_config.CLOUDFLARE_API_KEY,
    zone_id=app_config.CLOUDFLARE_ZONE_ID,
    account_id=app_config.CLOUDFLARE_ACCOUNT_ID,
)

__all__ = ["ecr_auth_service", "cloudflare_service"]
