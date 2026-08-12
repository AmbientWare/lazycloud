# Stripe Deployment

This Terraform configuration owns one thing: the webhook endpoint this platform
receives payment outcomes on, and the list of events it is subscribed to.

## Why only that

The provider also offers products, prices, coupons, tax rates, billing meters and
a whole v2 pricing-plan family. None of them belong here.
`packages/providers/stripe/AGENTS.md` states that this platform computes what is
owed and hands the provider finished amounts, and that anything here holding a
price would become a second answer to a question the platform already answers. A
catalog declared in Terraform is that same mistake one layer further out — two
places deciding money, drifting apart on their own schedules.

The billing portal configuration is also absent, for a duller reason: the
provider has no resource for it. Stripe auto-creates a default configuration the
first time a portal session is requested, which is what this account is running
on today.

## The event list is not decoration

`enabled_events` in `main.tf` is exactly what `BillingWebhookService` acts on:
anything matching its invoice prefix, plus the two events that say a card was
saved. An event outside that list is still delivered, still claimed against the
deduplication table, and then discarded — so a longer list is not harmless, it is
work the endpoint does for nothing.

If the handler learns a new event, this list changes in the same commit.

## Backend and credentials

Terraform state contains secrets. The signing secret is returned only at
creation, so state is the only place it survives — treat this state as
credential-bearing and keep its bucket accordingly. This repository deliberately
does not create or delete that bucket.

```sh
export AWS_PROFILE=platform-operations
export STRIPE_API_KEY=rk_live_or_test_operator_key

terraform -chdir=deploy/stripe init \
  -backend-config="bucket=$STRIPE_STATE_BUCKET" \
  -backend-config="key=$STRIPE_STATE_KEY" \
  -backend-config="region=$STRIPE_STATE_REGION" \
  -backend-config="encrypt=true" \
  -backend-config="use_lockfile=true"
```

Use a separate state key per Stripe account. A sandbox and a live account are
different accounts and must never share state.

## Plan and apply

```sh
terraform -chdir=deploy/stripe fmt -check
terraform -chdir=deploy/stripe validate
terraform -chdir=deploy/stripe plan -out=stripe.tfplan
terraform -chdir=deploy/stripe apply stripe.tfplan
```

`confirm_dedicated_account` defaults to `false`, so a bare apply refuses.

## After applying

`webhook_secret` becomes `LAZYCLOUD_STRIPE_WEBHOOK_SECRET` on both the control
plane and the scheduler. Until it is set the endpoint refuses every delivery with
a 400 — deliberately, since the URL is public and what it carries changes what
customers owe.

Recreate both services after setting it; a running container does not re-read its
environment.

## This module is for a deployed environment

The endpoint needs a URL Stripe can reach. Local development does not use this
module at all — `stripe listen --forward-to localhost:8000/webhooks/stripe` mints
its own session secret and registers no endpoint, which is both simpler and
leaves nothing behind.

Registering an endpoint whose URL cannot be reached is worse than registering
none: Stripe retries failed deliveries for days and disables endpoints that keep
failing.

## Provider version

Pinned to a pre-release, because that is what Stripe publishes. The provider is
generated from Stripe's internal tooling and its own README asks for an exact pin
in production — a floating constraint on a pre-release would let a generator run
change this account's shape without a commit here.
