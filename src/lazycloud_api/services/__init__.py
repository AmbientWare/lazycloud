from lazycloud_api.services.cloudflare import CloudflareService
from lazycloud_api.services.ecr_auth import ECRAuthService

ecr_auth_service = ECRAuthService()
cloudflare_service = CloudflareService()

__all__ = ["ecr_auth_service", "cloudflare_service"]
