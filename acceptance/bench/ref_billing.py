"""Provision a Stripe test-mode billing account for one reference workspace owner.

Runs inside the reference control-plane container: python - <user id> <workspace id>
"""

import sys

from billing.accounts import BillingAccountService
from database.client import DatabaseClient
from database.settings import DatabaseApplicationName, DatabaseSettings
from provider_stripe.settings import StripeSettings

user_id, workspace_id = sys.argv[1], sys.argv[2]
payments = StripeSettings().provider_factory()()
database = DatabaseClient.from_settings(
    DatabaseSettings(application_name=DatabaseApplicationName.Admin)
)
with database.session() as session:
    BillingAccountService(session).billing_account_for(
        payments, user_id=user_id, workspace_id=workspace_id
    )
    session.commit()
print("billing account provisioned")
