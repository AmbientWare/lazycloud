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

BASE_PRODUCT_NAME = "Basic"

BASE_FEATURES = BaseFeatures(
    workspace=WorkspaceFeature(
        limit=1, deployment_limit=1
    ),  # only Personal workspace, no isolation
    deployment=DeploymentFeature(service_limit=5, volume_limit=5, network_limit=1),
    domain_limit=0,  # Platform-generated domain only
)

DESCRITPION_MARKDOWN = f"""
Perfect for getting started. Pay only for what you use.

- 1 Personal workspace
- {BASE_FEATURES.workspace.deployment_limit} Deployment per workspace
- {BASE_FEATURES.deployment.service_limit} Services per deployment
- {BASE_FEATURES.deployment.volume_limit} Volumes per deployment
- {BASE_FEATURES.deployment.network_limit} Network per deployment
- Platform-generated domains names
"""

base_product = ProductDefinition(
    name=BASE_PRODUCT_NAME,
    description=DESCRITPION_MARKDOWN,
    recurring_interval=SubscriptionRecurringInterval.MONTH,
    has_free_base=True,
    monthly_fee=None,
    meter_prices=METER_PRICES,
    metadata=ProductMetadata(
        tier=BASE_PRODUCT_NAME.lower(),
        features=json.dumps(BASE_FEATURES.model_dump()),
    ),
)
