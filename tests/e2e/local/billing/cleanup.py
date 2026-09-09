"""Remove exactly what one sandbox billing run created, and prove none of it is left.

The workspace and account are passed by identifier. Provider cleanup lists only
the run's customer subscriptions and invoices. Published catalog entries and paid
invoices remain; open invoices are voided so Stripe stops collecting them.

Run against the sandbox account it was told to expect:

```sh
uv run python -m tests.e2e.local.billing.cleanup --live \
  --confirm-account acct_... --user-id ... --workspace-id ... \
  --customer-id cus_... --subscription-id sub_...
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
from foundation.environment_file import load_environment_file
from lazycloud.config import get_profile
from provider_stripe import StripeCatalog, StripeSettings
from provider_stripe.api import read, send
from shared.errors import InvalidInputError, UpstreamUnavailableError
from shared.http.errors import HttpApiError
from shared.http.users import UserResponse, UserStatusRequest
from shared.http.workspaces import WorkspaceListResponse
from shared.http_transport import HttpChannel
from shared.identity import UserStatus, WorkspaceStatus
from tests.e2e._support.process import LivePrerequisiteError, blocked
from tests.e2e.local.billing.ledger import (
    Customer,
    Invoice,
    InvoiceList,
    Subscription,
    SubscriptionList,
)

ADMIN_TIMEOUT_SECONDS = 60.0
WORKSPACE_DELETION_TIMEOUT_SECONDS = 240.0
WORKSPACE_DELETION_POLL_SECONDS = 2.0

UNSETTLED_INVOICE_STATUSES = frozenset({"draft", "open"})
"""Invoice states that are still somebody's problem.

A draft would go on to be finalized and an open one is being collected, so both
are things the run would leave running after it ended. Everything else — paid,
void, uncollectible — is a record of what happened and is left exactly as it is.
"""


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


def run_cleanup(
    client: httpx.Client,
    admin: HttpChannel,
    resources: RunResources,
) -> dict[str, Any]:
    """Settle every object this run made and report what the account holds now.

    Cancel subscriptions and settle invoices before deleting their customer.
    Named objects remain readable for cleanup retries after customer deletion.
    """

    reachable = _customer_reachable(client, resources.provider_customer_id)
    live_customer_id = resources.provider_customer_id if reachable else ""
    report: dict[str, Any] = {
        "subscriptions": _cancel_subscriptions(
            client, live_customer_id, resources.provider_subscription_id
        ),
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
    live_subscriptions = report["subscriptions"].get("live", [])
    if live_subscriptions:
        remaining.append(f"subscriptions still live: {', '.join(live_subscriptions)}")
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


def _cancel_subscriptions(
    client: httpx.Client, customer_id: str, named_subscription_id: str
) -> dict[str, Any]:
    """End every subscription this run's customer holds, not only the one it named.

    Provisioning may create the subscription before the scenario records its ID.
    Listing this customer's subscriptions also cleans up that partial setup.

    `invoice_now=false` because Stripe otherwise raises a final invoice for
    whatever usage has not been billed yet, and a cleanup that creates an invoice
    is a cleanup that leaves something behind. The usage stays on the account's
    record where the run's own comparison already read it.
    """

    subscription_ids = list(_customer_subscription_ids(client, customer_id))
    if named_subscription_id and named_subscription_id not in subscription_ids:
        subscription_ids.append(named_subscription_id)
    if not subscription_ids:
        return {"subscriptions": [], "live": []}
    settled: list[dict[str, Any]] = []
    live: list[str] = []
    for subscription_id in subscription_ids:
        current = read(Subscription, client, "GET", f"/subscriptions/{subscription_id}")
        if current.status != "canceled":
            current = read(
                Subscription,
                client,
                "DELETE",
                f"/subscriptions/{subscription_id}",
                data=[("invoice_now", "false"), ("prorate", "false")],
            )
        settled.append({"id": subscription_id, "state": current.status})
        if current.status != "canceled":
            live.append(subscription_id)
    return {"subscriptions": settled, "live": live}


def _customer_subscription_ids(client: httpx.Client, customer_id: str) -> tuple[str, ...]:
    if not customer_id:
        return ()
    listed = read(
        SubscriptionList,
        client,
        "GET",
        "/subscriptions",
        params=[("customer", customer_id), ("status", "all"), ("limit", "100")],
    )
    return tuple(item.id for item in listed.data)


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


def _customer_invoices(client: httpx.Client, customer_id: str) -> tuple[Invoice, ...]:
    listed = read(
        InvoiceList,
        client,
        "GET",
        "/invoices",
        params=[("customer", customer_id), ("limit", "100")],
    )
    return tuple(listed.data)


def _customer_residue(client: httpx.Client, customer_id: str) -> dict[str, list[str]]:
    """Everything still live on the run's customer, read after every step ran.

    Read independently of each mutation's response before deleting the customer.
    """

    if not customer_id:
        return {"subscriptions": [], "invoices": []}
    subscriptions = read(
        SubscriptionList,
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
    return not read(Customer, client, "GET", f"/customers/{customer_id}").deleted


def _delete_customer(client: httpx.Client, customer_id: str) -> dict[str, Any]:
    if not customer_id:
        return {"id": "", "state": "absent"}
    current = read(Customer, client, "GET", f"/customers/{customer_id}")
    if current.deleted:
        return {"id": customer_id, "state": "deleted"}
    removed = read(Customer, client, "DELETE", f"/customers/{customer_id}")
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


def main(argv: Sequence[str] | None = None) -> int:
    # This process is the entrypoint that owns itself, which is where a
    # developer `.env` is applied; nothing already exported is overridden.
    load_environment_file()
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
