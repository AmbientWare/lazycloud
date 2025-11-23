from functools import lru_cache

from lazycloud_api.config import app_config
from lazycloud_api.database import db
from lazycloud_api.database.invitations import WorkspaceInvitationService
from lazycloud_api.database.workspaces import WorkspaceService
from lazycloud_api.services.cloudflare import CloudflareService
from lazycloud_api.services.cost_breakdown_service import CostBreakdownService
from lazycloud_api.services.ecr_auth import ECRAuthService
from lazycloud_api.services.polar import PolarService
from lazycloud_api.services.prometheus import PrometheusMetricsService
from lazycloud_api.services.subscription_service import SubscriptionService
from lazycloud_api.services.usage_service import UsageService
from lazycloud_api.services.user_onboarding import UserOnboardingService


@lru_cache(maxsize=1)
def get_ecr_auth_service() -> ECRAuthService:
    return ECRAuthService(
        account_id=app_config.AWS_ACCOUNT_ID,
        access_key_id=app_config.AWS_ACCESS_KEY_ID,
        secret_access_key=app_config.AWS_SECRET_ACCESS_KEY,
        region=app_config.AWS_REGION,
        base_role_arn=app_config.AWS_ECR_BASE_ROLE_ARN,
        endpoint_url=app_config.AWS_ENDPOINT_URL,
    )


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
def get_invitation_service() -> WorkspaceInvitationService:
    return db.invitations


@lru_cache(maxsize=1)
def get_workspace_service() -> WorkspaceService:
    return db.workspaces


__all__ = [
    "ECRAuthService",
    "CloudflareService",
    "CostBreakdownService",
    "PrometheusMetricsService",
    "PolarService",
    "SubscriptionService",
    "UserOnboardingService",
    "UsageService",
    "WorkspaceInvitationService",
    "WorkspaceService",
    "get_ecr_auth_service",
    "get_cloudflare_service",
    "get_cost_breakdown_service",
    "get_metrics_service",
    "get_polar_service",
    "get_subscription_service",
    "get_user_onboarding_service",
    "get_usage_service",
    "get_invitation_service",
    "get_workspace_service",
]
