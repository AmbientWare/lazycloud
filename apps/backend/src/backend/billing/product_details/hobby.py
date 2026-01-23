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
    deployment_limit=5,
    max_team_members=1,
    max_cpu_per_service=2.0,
    max_memory_per_service=4,
    max_replicas_per_service=2,  # Basic auto-scaling up to 2x
    custom_domains_enabled=False,
    support_level="email",
)

DESCRIPTION_MARKDOWN = f"""
Perfect for individual developers and side projects.

- {DEVELOPER_FEATURES.deployment_limit} Deployments
- Up to {int(DEVELOPER_FEATURES.max_cpu_per_service)} CPU, {DEVELOPER_FEATURES.max_memory_per_service}GB RAM per service
- Auto-scaling (up to {DEVELOPER_FEATURES.max_replicas_per_service}x)
- Platform-generated domain names
"""

developer_product = ProductDefinition(
    name=DEVELOPER_PRODUCT_NAME,
    description=DESCRIPTION_MARKDOWN,
    recurring_interval=SubscriptionRecurringInterval.MONTH,
    has_free_base=False,
    monthly_fee=1900,  # $19/month
    meter_prices=METER_PRICES,
    metadata=ProductMetadata(
        tier=DEVELOPER_PRODUCT_NAME.lower(),
        features=json.dumps(DEVELOPER_FEATURES.model_dump()),
    ),
)
