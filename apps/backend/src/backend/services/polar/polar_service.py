from enum import StrEnum

from loguru import logger
from polar_sdk import Polar

from .cost_breakdown import PolarCostBreakdownModule
from .customers import PolarCustomersModule
from .meters import PolarMetersModule
from .pricing import PolarPricingModule
from .products import PolarProductsModule
from .subscriptions import PolarSubscriptionsModule
from .usage import PolarUsageModule


class PolarServer(StrEnum):
    """Polar server environment."""

    SANDBOX = "sandbox"
    PRODUCTION = "production"


class PolarService:
    """Service for managing billing operations with Polar."""

    def __init__(self, access_token: str, is_sandbox: bool):
        """Initialize the Polar client."""
        self.enabled = bool(access_token)
        self.is_sandbox = is_sandbox

        self.client = None
        if self.enabled:
            try:
                self.client = Polar(
                    access_token=access_token,
                    server=PolarServer.SANDBOX.value
                    if self.is_sandbox
                    else PolarServer.PRODUCTION.value,
                )
                logger.info("Polar billing service initialized")
            except Exception as e:
                logger.error(f"Failed to initialize Polar client: {e}")
                self.enabled = False
        else:
            logger.warning(
                "Polar billing service disabled: POLAR_ACCESS_TOKEN not configured"
            )

        if self.client is None:
            raise RuntimeError("Polar client is not initialized")

        self.customers = PolarCustomersModule(client=self.client, enabled=self.enabled)
        self.meters = PolarMetersModule(client=self.client, enabled=self.enabled)
        self.products = PolarProductsModule(client=self.client, enabled=self.enabled)
        self.pricing = PolarPricingModule(
            client=self.client, enabled=self.enabled, products_module=self.products
        )
        self.cost_breakdown = PolarCostBreakdownModule(pricing_module=self.pricing)
        self.subscriptions = PolarSubscriptionsModule(
            client=self.client, enabled=self.enabled
        )
        self.usage = PolarUsageModule(client=self.client, enabled=self.enabled)
