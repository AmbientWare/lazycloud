from lazycloud_api.billing.product_details.base_models import (
    METER_PRICES,
    ProductDefinition,
    SubscriptionRecurringInterval,
)
from lazycloud_api.billing.product_details.features import (
    BaseFeatures,
    DeploymentFeature,
    WorkspaceFeature,
)

ENTERPRISE_PRODUCT_NAME = "Enterprise"

ENTERPRISE_FEATURES = BaseFeatures(
    workspace=WorkspaceFeature(
        limit=10, deployment_limit=20
    ),  # Team-scale isolation with multiple projects/environments
    deployment=DeploymentFeature(service_limit=25, volume_limit=25, network_limit=10),
    domain_limit=200,  # Custom domains enabled
)

DESCRITPION_MARKDOWN = f"""
Built for large teams and production workloads. Maximum scale and flexibility.

- 1 Personal + {ENTERPRISE_FEATURES.workspace.limit - 1} additional workspaces
- {ENTERPRISE_FEATURES.workspace.deployment_limit} Deployments per workspace
- {ENTERPRISE_FEATURES.deployment.service_limit} Services per deployment
- {ENTERPRISE_FEATURES.deployment.volume_limit} Volumes per deployment
- {ENTERPRISE_FEATURES.deployment.network_limit} Networks per deployment
- {ENTERPRISE_FEATURES.domain_limit} Custom domain names
- Add team members to your custom workspaces
"""

enterprise_product = ProductDefinition(
    name=ENTERPRISE_PRODUCT_NAME,
    description=DESCRITPION_MARKDOWN,
    recurring_interval=SubscriptionRecurringInterval.MONTH,
    has_free_base=False,
    monthly_fee=10000,  # $100/month
    meter_prices=METER_PRICES,
    metadata={"tier": ENTERPRISE_PRODUCT_NAME.lower()},
)
