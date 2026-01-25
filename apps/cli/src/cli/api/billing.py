from responses.billing import BillingCycleResponse

from cli.api.base import BaseAPI


class BillingAPI(BaseAPI):
    """API client for billing endpoints"""

    def __init__(self):
        super().__init__("billing")

    async def get_billing_cycle(self) -> BillingCycleResponse:
        """Get billing cycle dates for the current user's subscription."""
        response = await self._get_async("/cycle")
        return BillingCycleResponse.model_validate(response)
