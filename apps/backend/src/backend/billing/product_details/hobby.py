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

HOBBY_PRODUCT_NAME = "Hobby"

HOBBY_FEATURES = BaseFeatures(
    workspace=WorkspaceFeature(
        limit=2, deployment_limit=3
    ),  # Full dev/staging/prod for individual developers
    deployment=DeploymentFeature(
        service_limit=6,
        volume_limit=6,
        network_limit=2,
        max_replicas_per_service=5,  # Auto-scaling enabled
        max_cpu_per_service=4.0,  # 4 CPU cores per service
        max_memory_per_service=8,  # 8GB RAM per service
    ),
    domain_limit=999,  # Custom domains enabled (unlimited in practice)
    max_team_members=3,  # You + 2 collaborators
    support_level="email",  # Email support (48h response)
)

DESCRITPION_MARKDOWN = f"""
Perfect for individual developers and side projects. Includes environment isolation and custom domains.

- 1 Personal + {HOBBY_FEATURES.workspace.limit - 1} additional workspace
- {HOBBY_FEATURES.workspace.deployment_limit} Deployments per workspace
- {HOBBY_FEATURES.deployment.service_limit} Services per deployment (max 4 CPU, 8GB RAM each)
- {HOBBY_FEATURES.deployment.volume_limit} Volumes per deployment
- {HOBBY_FEATURES.deployment.network_limit} Networks per deployment
- Custom domain names
- Auto-scaling enabled (up to {HOBBY_FEATURES.deployment.max_replicas_per_service} replicas)
- Team collaboration (up to {HOBBY_FEATURES.max_team_members} members)
- Email support
"""

hobby_product = ProductDefinition(
    name=HOBBY_PRODUCT_NAME,
    description=DESCRITPION_MARKDOWN,
    recurring_interval=SubscriptionRecurringInterval.MONTH,
    has_free_base=False,
    monthly_fee=2500,  # $25/month
    meter_prices=METER_PRICES,
    metadata=ProductMetadata(
        tier=HOBBY_PRODUCT_NAME.lower(),
        features=json.dumps(HOBBY_FEATURES.model_dump()),
    ),
)
