"""Remove exactly what one sandbox billing run created, and prove none of it is left.

Independently callable, and named rather than searched: the customer, the
subscription, the workspace and the account are all passed in by identifier, and
the only thing this ever lists is what hangs off the run's own customer — the
allowances it was granted and the invoices it was issued, neither of which a run
can name in advance because the platform buys a fresh grant on every renewal and
Stripe raises an invoice whenever a period closes.

The published catalog — the three meters, the four products, the four prices and
the webhook endpoint — belongs to the account and not to any run, and is never
touched here. Neither is a paid invoice: that one is the record that money moved,
and deleting records of charges is not cleanup. An invoice still open is the
opposite — an obligation nobody will ever collect, and one Stripe would go on
chasing — so it is voided.

Run against the sandbox account it was told to expect:

```sh
uv run python -m tests.e2e.local.billing.cleanup --live \
  --confirm-account acct_... --user-id ... --workspace-id ... \
  --customer-id cus_... --subscription-id sub_... --credit-grant-id credgr_...
```

Re-running it is safe and is how "zero remaining" is proven: every step reads
the object's current state first and reports it rather than acting again.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx
from lazycloud.config import get_profile
from provider_stripe import StripeCatalog, StripeSettings
from provider_stripe.api import StripeObject, read, send
from pydantic import Field
from shared.errors import InvalidInputError, UpstreamUnavailableError
from shared.http.errors import HttpApiError
from shared.http.users import UserResponse, UserStatusRequest
from shared.http.workspaces import WorkspaceListResponse
from shared.http_transport import HttpChannel
from shared.identity import UserStatus, WorkspaceStatus
from shared.timestamps import utc_now
from tests.e2e._support.process import LivePrerequisiteError, blocked

ADMIN_TIMEOUT_SECONDS = 60.0
WORKSPACE_DELETION_TIMEOUT_SECONDS = 240.0
WORKSPACE_DELETION_POLL_SECONDS = 2.0

UNSETTLED_INVOICE_STATUSES = frozenset({"draft", "open"})
"""Invoice states that are still somebody's problem.

A draft would go on to be finalized and an open one is being collected, so both
are things the run would leave running after it ended. Everything else — paid,
void, uncollectible — is a record of what happened and is left exactly as it is.
"""


class _Subscription(StripeObject):
    id: str = ""
    status: str = ""


class _SubscriptionList(StripeObject):
    data: list[_Subscription] = Field(default_factory=list)


class _CreditGrant(StripeObject):
    id: str = ""
    expires_at: int | None = None
    voided_at: int | None = None

    @property
    def settled(self) -> bool:
        if self.voided_at is not None:
            return True
        return self.expires_at is not None and self.expires_at <= int(utc_now().timestamp())


class _CreditGrantList(StripeObject):
    data: list[_CreditGrant] = Field(default_factory=list)


class _Customer(StripeObject):
    id: str = ""
    deleted: bool = False


class _Invoice(StripeObject):
    id: str = ""
    status: str = ""
    amount_paid: int = 0


class _InvoiceList(StripeObject):
    data: list[_Invoice] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RunResources:
    """Everything one run made, named exactly.

    Every field is optional because a run can fail at any step, and a cleanup
    that refuses to act without all of them would strand whatever the failed run
    did manage to create.
    """

    user_id: str = ""
    workspace_id: str = ""
    provider_customer_id: str = ""
    provider_subscription_id: str = ""
    provider_credit_grant_id: str = ""


def run_cleanup(
    client: httpx.Client,
    admin: HttpChannel,
    resources: RunResources,
) -> dict[str, Any]:
    """Settle every object this run made and report what the account holds now.

    Ordered so each step's outcome is still readable by the next: the grants are
    expired and the subscription cancelled while the customer still exists, the
    invoices are settled and the account re-read for residue before the customer
    goes, and the workspace is deleted last because its ledger rows are what the
    provider was metered for.

    Whether the customer is still there is established first, because a second
    run of this — which is how "nothing is left" is demonstrated — happens after
    the first one deleted them, and Stripe refuses to list allowances against a
    customer that no longer exists. The grant the run recorded is still readable
    by its own id, so it is still checked.
    """

    reachable = _customer_reachable(client, resources.provider_customer_id)
    report: dict[str, Any] = {
        "credit_grants": _expire_credit_grants(
            client,
            resources.provider_customer_id if reachable else "",
            resources.provider_credit_grant_id,
        ),
        "subscription": _cancel_subscription(client, resources.provider_subscription_id),
        "invoices": _settle_invoices(client, resources.provider_customer_id),
        "residue": _customer_residue(client, resources.provider_customer_id),
        "customer": _delete_customer(client, resources.provider_customer_id),
        "workspace": _delete_workspace(admin, resources.workspace_id),
        "account": _disable_account(admin, resources.user_id),
    }
    report["remaining"] = _remaining(report)
    return report


def _remaining(report: dict[str, Any]) -> list[str]:
    """Everything this run created that the account or platform still holds live."""

    remaining: list[str] = []
    active_grants = report["credit_grants"].get("active", [])
    if active_grants:
        remaining.append(f"credit grants still active: {', '.join(active_grants)}")
    if report["subscription"].get("state") not in {"canceled", "absent"}:
        remaining.append(f"subscription is {report['subscription'].get('state')}")
    unsettled = report["invoices"].get("unsettled", [])
    if unsettled:
        remaining.append(f"invoices still unsettled: {', '.join(unsettled)}")
    for kind, held in report["residue"].items():
        if held:
            remaining.append(f"the customer still holds {kind}: {', '.join(held)}")
    if report["customer"].get("state") not in {"deleted", "absent"}:
        remaining.append(f"customer is {report['customer'].get('state')}")
    if report["workspace"].get("state") not in {"deleted", "absent"}:
        remaining.append(f"workspace is {report['workspace'].get('state')}")
    if report["account"].get("state") not in {"disabled", "absent"}:
        remaining.append(f"account is {report['account'].get('state')}")
    return remaining


def _expire_credit_grants(
    client: httpx.Client, customer_id: str, named_grant_id: str
) -> dict[str, Any]:
    """End every allowance this run was granted, not only the one it recorded.

    The account row holds the newest grant, and a run whose subscription renewed
    has been given more than one — the platform buys a fresh allowance each
    period and the row forgets the last. Listing them off the run's own customer
    is the only way to name all of them, and it can reach nobody else's.
    """

    grant_ids = list(_customer_grant_ids(client, customer_id))
    if named_grant_id and named_grant_id not in grant_ids:
        grant_ids.append(named_grant_id)
    if not grant_ids:
        return {"grants": [], "active": []}
    settled: list[dict[str, Any]] = []
    active: list[str] = []
    for grant_id in grant_ids:
        grant = read(_CreditGrant, client, "GET", f"/billing/credit_grants/{grant_id}")
        if not grant.settled:
            grant = read(_CreditGrant, client, "POST", f"/billing/credit_grants/{grant_id}/expire")
        settled.append(
            {"id": grant_id, "expires_at": grant.expires_at, "voided_at": grant.voided_at}
        )
        if not grant.settled:
            active.append(grant_id)
    return {"grants": settled, "active": active}


def _customer_grant_ids(client: httpx.Client, customer_id: str) -> tuple[str, ...]:
    if not customer_id:
        return ()
    listed = read(
        _CreditGrantList,
        client,
        "GET",
        "/billing/credit_grants",
        params=[("customer", customer_id), ("limit", "100")],
    )
    return tuple(grant.id for grant in listed.data)


def _cancel_subscription(client: httpx.Client, subscription_id: str) -> dict[str, Any]:
    """End the subscription without producing an invoice for it.

    `invoice_now=false` because Stripe otherwise raises a final invoice for
    whatever usage has not been billed yet, and a cleanup that creates an invoice
    is a cleanup that leaves something behind. The usage stays on the account's
    record where the run's own comparison already read it.
    """

    if not subscription_id:
        return {"id": "", "state": "absent"}
    current = read(_Subscription, client, "GET", f"/subscriptions/{subscription_id}")
    if current.status == "canceled":
        return {"id": subscription_id, "state": "canceled"}
    cancelled = read(
        _Subscription,
        client,
        "DELETE",
        f"/subscriptions/{subscription_id}",
        data=[("invoice_now", "false"), ("prorate", "false")],
    )
    return {"id": subscription_id, "state": cancelled.status}


def _settle_invoices(client: httpx.Client, customer_id: str) -> dict[str, Any]:
    """Leave no invoice of this run's still waiting to be paid or finalized.

    A draft is deleted and an open one voided — an open invoice with nobody left
    to pay it is dunning mail and a growing balance on the account, which is
    exactly the residue a run must not leave. Paid and voided invoices are read
    and reported, never touched: what money did move is the evidence, not the
    mess.
    """

    if not customer_id:
        return {"paid": [], "settled": [], "unsettled": []}
    paid: list[dict[str, Any]] = []
    for invoice in _customer_invoices(client, customer_id):
        if invoice.status == "draft":
            send(client, "DELETE", f"/invoices/{invoice.id}")
        elif invoice.status == "open":
            send(client, "POST", f"/invoices/{invoice.id}/void")
        elif invoice.status == "paid":
            paid.append({"id": invoice.id, "amount_paid": invoice.amount_paid})
    after = _customer_invoices(client, customer_id)
    return {
        "paid": paid,
        "settled": [
            {"id": invoice.id, "status": invoice.status}
            for invoice in after
            if invoice.status not in UNSETTLED_INVOICE_STATUSES
        ],
        "unsettled": [
            invoice.id for invoice in after if invoice.status in UNSETTLED_INVOICE_STATUSES
        ],
    }


def _customer_invoices(client: httpx.Client, customer_id: str) -> tuple[_Invoice, ...]:
    listed = read(
        _InvoiceList,
        client,
        "GET",
        "/invoices",
        params=[("customer", customer_id), ("limit", "100")],
    )
    return tuple(listed.data)


def _customer_residue(client: httpx.Client, customer_id: str) -> dict[str, list[str]]:
    """Everything still live on the run's customer, read after every step ran.

    Read again rather than inferred from what each step returned: a step reports
    what it did, and this reports what is there. Allowances are absent because
    they are the one thing this cannot ask a customer for once that customer is
    gone; `credit_grants` above reads each of them by id and says which are still
    active.
    """

    if not customer_id:
        return {"subscriptions": [], "invoices": []}
    subscriptions = read(
        _SubscriptionList,
        client,
        "GET",
        "/subscriptions",
        params=[("customer", customer_id), ("status", "all"), ("limit", "100")],
    )
    return {
        "subscriptions": [item.id for item in subscriptions.data if item.status != "canceled"],
        "invoices": [
            invoice.id
            for invoice in _customer_invoices(client, customer_id)
            if invoice.status in UNSETTLED_INVOICE_STATUSES
        ],
    }


def _customer_reachable(client: httpx.Client, customer_id: str) -> bool:
    """Whether Stripe will still answer questions asked about this customer."""

    if not customer_id:
        return False
    return not read(_Customer, client, "GET", f"/customers/{customer_id}").deleted


def _delete_customer(client: httpx.Client, customer_id: str) -> dict[str, Any]:
    if not customer_id:
        return {"id": "", "state": "absent"}
    current = read(_Customer, client, "GET", f"/customers/{customer_id}")
    if current.deleted:
        return {"id": customer_id, "state": "deleted"}
    removed = read(_Customer, client, "DELETE", f"/customers/{customer_id}")
    return {"id": customer_id, "state": "deleted" if removed.deleted else "present"}


def _delete_workspace(admin: HttpChannel, workspace_id: str) -> dict[str, Any]:
    """Delete the run's workspace and hold on until the platform says it is gone.

    Its billing rows outlive it by design, so this also proves that: a workspace
    whose ledger blocked its own deletion would fail here rather than quietly
    leave a live tenant behind.
    """

    if not workspace_id:
        return {"id": "", "state": "absent"}
    status = _workspace_status(admin, workspace_id)
    if status is None:
        return {"id": workspace_id, "state": "absent"}
    if status is not WorkspaceStatus.Deleted:
        admin.delete(f"/api/v1/workspaces/{workspace_id}")
    deadline = time.monotonic() + WORKSPACE_DELETION_TIMEOUT_SECONDS
    while True:
        current = _workspace_status(admin, workspace_id)
        if current is None:
            return {"id": workspace_id, "state": "absent"}
        if current is WorkspaceStatus.Deleted:
            return {"id": workspace_id, "state": "deleted"}
        if time.monotonic() >= deadline:
            return {"id": workspace_id, "state": current.value}
        time.sleep(WORKSPACE_DELETION_POLL_SECONDS)


def _disable_account(admin: HttpChannel, user_id: str) -> dict[str, Any]:
    """Retire the account the run made, which is as far as an account goes.

    Disabled rather than deleted because there is no deleting one: the account
    owns the ledger rows the provider was metered for, and those outlive every
    workspace and every run by design. Its credentials stop working, which is
    what the run has to leave behind.
    """

    if not user_id:
        return {"id": "", "state": "absent"}
    updated = UserResponse.model_validate(
        admin.request(
            "PUT",
            f"/api/v1/users/{user_id}/status",
            payload=UserStatusRequest(status=UserStatus.Disabled).model_dump(mode="json"),
        )
    )
    return {"id": user_id, "state": updated.status.value}


def _workspace_status(admin: HttpChannel, workspace_id: str) -> WorkspaceStatus | None:
    listing = WorkspaceListResponse.model_validate(
        admin.get("/api/v1/workspaces?include_deleted=true&include_deleting=true")
    )
    for item in listing.workspaces:
        if item.id == workspace_id:
            return item.status
    return None


def build_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--confirm-account", default="")
    parser.add_argument("--user-id", default="")
    parser.add_argument("--workspace-id", default="")
    parser.add_argument("--customer-id", default="")
    parser.add_argument("--subscription-id", default="")
    parser.add_argument("--credit-grant-id", default="")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    build_arguments(parser)
    args = parser.parse_args(argv)
    if not args.live:
        return blocked(LivePrerequisiteError("this cleanup requires the explicit --live opt-in"))
    settings = StripeSettings()
    if not settings.configured:
        return blocked(
            LivePrerequisiteError("LAZYCLOUD_STRIPE_API_KEY is required to reach the sandbox")
        )
    client = settings.provider().client
    account_id = StripeCatalog(client=client).account_id()
    if args.confirm_account != account_id:
        return blocked(
            LivePrerequisiteError(
                f"the credential in hand belongs to {account_id}, not --confirm-account"
            )
        )
    profile = get_profile()
    if not profile.token:
        return blocked(LivePrerequisiteError("an administrator lazycloud profile is required"))
    admin = HttpChannel(
        endpoint=profile.resolved_endpoint(),
        token=profile.token,
        timeout_seconds=ADMIN_TIMEOUT_SECONDS,
    )
    try:
        report = run_cleanup(
            client,
            admin,
            RunResources(
                user_id=args.user_id,
                workspace_id=args.workspace_id,
                provider_customer_id=args.customer_id,
                provider_subscription_id=args.subscription_id,
                provider_credit_grant_id=args.credit_grant_id,
            ),
        )
    except (HttpApiError, InvalidInputError, UpstreamUnavailableError) as exc:
        print(f"cleanup failed: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()
    print(json.dumps({"account": account_id, "cleanup": report}, indent=1, sort_keys=True))
    return 0 if not report["remaining"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
