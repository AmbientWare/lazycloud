import json

from models.billing import MeterNames, UsageUnits

from backend.billing.product_details.base_models import (
    METER_PRICES,
    ProductDefinition,
    ProductMetadata,
    SubscriptionRecurringInterval,
)
from backend.billing.product_details.features import BaseFeatures

SCALE_PRODUCT_NAME = "Scale"

SCALE_FEATURES = BaseFeatures(
    deployment_limit=25,
    max_team_members=None,
    max_cpu_per_service=8.0,
    max_memory_per_service=32,
    max_replicas_per_service=5,  # Auto-scaling up to 5x
    custom_domains_enabled=True,
)

DESCRIPTION_MARKDOWN = f"""
For growing teams and high-scale workloads.

- Unlimited Deployments
- Up to {int(SCALE_FEATURES.max_cpu_per_service)} CPU, {SCALE_FEATURES.max_memory_per_service}GB RAM per service
- Custom domain names
- Auto-scaling (up to {SCALE_FEATURES.max_replicas_per_service}x)
- Unlimited team members
- Storage metered (${UsageUnits.get_price_dollars(MeterNames.STORAGE_USAGE):.2f}/GB-month)
"""

scale_product = ProductDefinition(
    name=SCALE_PRODUCT_NAME,
    description=DESCRIPTION_MARKDOWN,
    recurring_interval=SubscriptionRecurringInterval.MONTH,
    has_free_base=False,
    monthly_fee=4900,  # $49/month
    meter_prices=METER_PRICES,
    metadata=ProductMetadata(
        tier=SCALE_PRODUCT_NAME.lower(),
        features=json.dumps(SCALE_FEATURES.model_dump()),
    ),
)
