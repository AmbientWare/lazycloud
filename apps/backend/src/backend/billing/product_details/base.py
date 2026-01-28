import json

from backend.billing.product_details.base_models import (
    METER_PRICES,
    ProductDefinition,
    ProductMetadata,
    SubscriptionRecurringInterval,
)
from backend.billing.product_details.features import BaseFeatures

DEVELOPER_PRODUCT_NAME = "Developer"

DEVELOPER_FEATURES = BaseFeatures(
    deployment_limit=1,
    max_team_members=1,
    max_cpu_per_service=1.0,
    max_memory_per_service=4,
    max_replicas_per_service=1,  # No auto-scaling
    custom_domains_enabled=False,
)

DESCRIPTION_MARKDOWN = f"""
Perfect for getting started. Pay only for what you use.

- {DEVELOPER_FEATURES.deployment_limit} Deployments
- Up to {int(DEVELOPER_FEATURES.max_cpu_per_service)} CPU, {DEVELOPER_FEATURES.max_memory_per_service}GB RAM per service
- Platform-generated domain names
"""

developer_product = ProductDefinition(
    name=DEVELOPER_PRODUCT_NAME,
    description=DESCRIPTION_MARKDOWN,
    recurring_interval=SubscriptionRecurringInterval.MONTH,
    has_free_base=True,
    monthly_fee=None,
    meter_prices=METER_PRICES,
    metadata=ProductMetadata(
        tier=DEVELOPER_PRODUCT_NAME.lower(),
        features=json.dumps(DEVELOPER_FEATURES.model_dump()),
    ),
)
