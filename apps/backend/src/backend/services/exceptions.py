"""Service-level exceptions."""


class NoActiveSubscriptionError(Exception):
    """Exception raised when a user has no active subscription.

    This is a recoverable error - the user may be an admin/test user
    or simply needs to subscribe.
    """

    def __init__(self, external_customer_id: str):
        self.external_customer_id = external_customer_id
        super().__init__(
            f"No active subscription found for {external_customer_id}. "
            f"User must have an active subscription."
        )
