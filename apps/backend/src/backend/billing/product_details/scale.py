import json

from backend.billing.product_details.base_models import (
    METER_PRICES,
    ProductDefinition,
    ProductMetadata,
    SubscriptionRecurringInterval,
)
from backend.billing.product_details.features import BaseFeatures

SCALE_PRODUCT_NAME = "Scale"

SCALE_FEATURES = BaseFeatures(
    deployment_limit=50,
    max_team_members=None,  # Unlimited
    max_cpu_per_service=32.0,
    max_memory_per_service=64,
    max_replicas_per_service=25,  # Auto-scaling up to 25x
    custom_domains_enabled=True,
    support_level="dedicated",
)

DESCRIPTION_MARKDOWN = f"""
For growing teams and high-scale workloads.

- {SCALE_FEATURES.deployment_limit} Deployments
- Up to {int(SCALE_FEATURES.max_cpu_per_service)} CPU, {SCALE_FEATURES.max_memory_per_service}GB RAM per service
- Custom domain names
- Auto-scaling (up to {SCALE_FEATURES.max_replicas_per_service}x)
- Unlimited team members
"""

scale_product = ProductDefinition(
    name=SCALE_PRODUCT_NAME,
    description=DESCRIPTION_MARKDOWN,
    recurring_interval=SubscriptionRecurringInterval.MONTH,
    has_free_base=False,
    monthly_fee=14900,  # $149/month
    meter_prices=METER_PRICES,
    metadata=ProductMetadata(
        tier=SCALE_PRODUCT_NAME.lower(),
        features=json.dumps(SCALE_FEATURES.model_dump()),
    ),
)
