import json

from backend.billing.product_details.base_models import (
    METER_PRICES,
    ProductDefinition,
    ProductMetadata,
    SubscriptionRecurringInterval,
)
from backend.billing.product_details.features import BaseFeatures

PRO_PRODUCT_NAME = "Pro"

PRO_FEATURES = BaseFeatures(
    deployment_limit=10,
    max_team_members=10,
    max_cpu_per_service=8.0,
    max_memory_per_service=16,
    max_replicas_per_service=5,  # Auto-scaling up to 5x
    custom_domains_enabled=True,
    support_level="priority",
)

DESCRIPTION_MARKDOWN = f"""
Ideal for teams and production workloads.

- {PRO_FEATURES.deployment_limit} Deployments
- Up to {int(PRO_FEATURES.max_cpu_per_service)} CPU, {PRO_FEATURES.max_memory_per_service}GB RAM per service
- Custom domain names
- Auto-scaling (up to {PRO_FEATURES.max_replicas_per_service}x)
- Team collaboration (up to {PRO_FEATURES.max_team_members} members)
- Priority support
"""

pro_product = ProductDefinition(
    name=PRO_PRODUCT_NAME,
    description=DESCRIPTION_MARKDOWN,
    recurring_interval=SubscriptionRecurringInterval.MONTH,
    has_free_base=False,
    monthly_fee=4900,  # $49/month
    meter_prices=METER_PRICES,
    metadata=ProductMetadata(
        tier=PRO_PRODUCT_NAME.lower(),
        features=json.dumps(PRO_FEATURES.model_dump()),
    ),
)
