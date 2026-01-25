from functools import lru_cache

from backend.config import app_config
from backend.services.aws_metrics import AWSMetricsService
from backend.services.cache import CacheService
from backend.services.cloudflare import CloudflareService
from backend.services.cost_breakdown_service import CostBreakdownService
from backend.services.depot_service import DepotService
from backend.services.invitation_service import InvitationService
from backend.services.polar import PolarService
from backend.services.prometheus import PrometheusMetricsService
from backend.services.subscription_service import SubscriptionService
from backend.services.usage_service import UsageService
from backend.services.user_onboarding import UserOnboardingService


@lru_cache(maxsize=1)
def get_cloudflare_service() -> CloudflareService:
    return CloudflareService(
        api_key=app_config.CLOUDFLARE_API_KEY,
        zone_id=app_config.CLOUDFLARE_ZONE_ID,
        account_id=app_config.CLOUDFLARE_ACCOUNT_ID,
    )


@lru_cache(maxsize=1)
def get_metrics_service() -> PrometheusMetricsService:
    return PrometheusMetricsService(
        prometheus_url=app_config.PROMETHEUS_URL,
    )


@lru_cache(maxsize=1)
def get_polar_service() -> PolarService:
    return PolarService(
        access_token=app_config.POLAR_ACCESS_TOKEN,
        is_sandbox=app_config.IS_POLAR_SANDBOX,
    )


@lru_cache(maxsize=1)
def get_user_onboarding_service() -> UserOnboardingService:
    return UserOnboardingService(
        polar_service=get_polar_service(),
    )


@lru_cache(maxsize=1)
def get_usage_service() -> UsageService:
    return UsageService(
        cost_service=get_cost_breakdown_service(),
        depot_service=get_depot_service(),
    )


@lru_cache(maxsize=1)
def get_cost_breakdown_service() -> CostBreakdownService:
    return CostBreakdownService(
        polar_service=get_polar_service(),
    )


@lru_cache(maxsize=1)
def get_subscription_service() -> SubscriptionService:
    return SubscriptionService(polar_service=get_polar_service())


@lru_cache(maxsize=1)
def get_invitation_service() -> InvitationService:
    return InvitationService()


@lru_cache(maxsize=1)
def get_depot_service() -> DepotService:
    return DepotService(
        api_token=app_config.DEPOT_API_TOKEN,
        org_id=app_config.DEPOT_ORG_ID,
        enabled=app_config.DEPOT_ENABLED,
    )


@lru_cache(maxsize=1)
def get_aws_metrics_service() -> AWSMetricsService:
    return AWSMetricsService(
        region=app_config.AWS_REGION,
        access_key_id=app_config.AWS_ACCESS_KEY_ID,
        secret_access_key=app_config.AWS_SECRET_ACCESS_KEY,
        endpoint_url=app_config.AWS_ENDPOINT_URL,
    )


@lru_cache(maxsize=1)
def get_cache_service() -> CacheService:
    """Get the singleton cache service instance."""
    return CacheService(redis_url=app_config.REDIS_URL)


__all__ = [
    "AWSMetricsService",
    "CacheService",
    "DepotService",
    "CloudflareService",
    "CostBreakdownService",
    "PrometheusMetricsService",
    "PolarService",
    "SubscriptionService",
    "InvitationService",
    "UserOnboardingService",
    "UsageService",
    "get_aws_metrics_service",
    "get_cache_service",
    "get_depot_service",
    "get_cloudflare_service",
    "get_cost_breakdown_service",
    "get_metrics_service",
    "get_polar_service",
    "get_subscription_service",
    "get_user_onboarding_service",
    "get_usage_service",
    "get_invitation_service",
]
