import json

from backend.billing.product_details.base_models import (
    METER_PRICES,
    ProductDefinition,
    ProductMetadata,
    SubscriptionRecurringInterval,
)
from backend.billing.product_details.features import (
    BaseFeatures,
    DeploymentFeature,
    WorkspaceFeature,
)

PRO_PRODUCT_NAME = "Pro"

PRO_FEATURES = BaseFeatures(
    workspace=WorkspaceFeature(
        limit=4, deployment_limit=5
    ),  # 3 apps with environment isolation - key premium feature
    deployment=DeploymentFeature(
        service_limit=12,
        volume_limit=12,
        network_limit=4,
        max_replicas_per_service=10,
    ),
    domain_limit=999,  # Custom domains enabled (unlimited in practice)
)

DESCRITPION_MARKDOWN = f"""
Ideal for teams and production workloads. Multiple apps with environment isolation.

- 1 Personal + {PRO_FEATURES.workspace.limit - 1} additional workspaces
- {PRO_FEATURES.workspace.deployment_limit} Deployments per workspace
- {PRO_FEATURES.deployment.service_limit} Services per deployment
- {PRO_FEATURES.deployment.volume_limit} Volumes per deployment
- {PRO_FEATURES.deployment.network_limit} Networks per deployment
- Custom domain names
- Add team members to your custom workspaces
"""

pro_product = ProductDefinition(
    name=PRO_PRODUCT_NAME,
    description=DESCRITPION_MARKDOWN,
    recurring_interval=SubscriptionRecurringInterval.MONTH,
    has_free_base=False,
    monthly_fee=10000,  # $100/month
    meter_prices=METER_PRICES,
    metadata=ProductMetadata(
        tier=PRO_PRODUCT_NAME.lower(),
        features=json.dumps(PRO_FEATURES.model_dump()),
    ),
)
