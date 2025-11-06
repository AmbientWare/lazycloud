import json

from lazycloud_api.billing.product_details.base_models import (
    METER_PRICES,
    ProductDefinition,
    ProductMetadata,
    SubscriptionRecurringInterval,
)
from lazycloud_api.billing.product_details.features import (
    BaseFeatures,
    DeploymentFeature,
    WorkspaceFeature,
)

PRO_PRODUCT_NAME = "Pro"

PRO_FEATURES = BaseFeatures(
    workspace=WorkspaceFeature(
        limit=3, deployment_limit=5
    ),  # Dev/staging/prod isolation - key premium feature
    deployment=DeploymentFeature(service_limit=10, volume_limit=10, network_limit=3),
    domain_limit=25,  # Custom domains enabled
)

DESCRITPION_MARKDOWN = f"""
Ideal for teams needing environment isolation. Includes custom domains.

- 1 Personal + {PRO_FEATURES.workspace.limit - 1} additional workspaces
- {PRO_FEATURES.workspace.deployment_limit} Deployments per workspace
- {PRO_FEATURES.deployment.service_limit} Services per deployment
- {PRO_FEATURES.deployment.volume_limit} Volumes per deployment
- {PRO_FEATURES.deployment.network_limit} Networks per deployment
- {PRO_FEATURES.domain_limit} Custom domain names
- Add team members to your custom workspaces
"""

pro_product = ProductDefinition(
    name=PRO_PRODUCT_NAME,
    description=DESCRITPION_MARKDOWN,
    recurring_interval=SubscriptionRecurringInterval.MONTH,
    has_free_base=False,
    monthly_fee=5000,  # $50/month
    meter_prices=METER_PRICES,
    metadata=ProductMetadata(
        tier=PRO_PRODUCT_NAME.lower(),
        features=json.dumps(PRO_FEATURES.model_dump()),
    ),
)
