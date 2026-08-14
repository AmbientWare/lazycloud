# Stripe Deployment

This Terraform configuration owns one thing: the webhook endpoint this platform
receives payment deliveries on, and the list of events it is subscribed to.

## Why only that

The endpoint is the only object at the provider whose creation returns a secret.
The signing secret is issued once and never shown again, so Terraform state is
the only place it survives — which is exactly what a Terraform resource is for
and exactly what a command run at a shell is not.

## Publish the catalog before anyone signs in

`lazycloud-admin billing publish-catalog --confirm-account <acct> --confirm` runs
before the first person reaches the platform, not after. Signing in provisions a
billing account — a customer, a subscription on the free plan and the grant that
funds its first cycle — and it fails closed, so an account that cannot be
provisioned gets no session. Subscribing resolves the plan and metered prices by
lookup key, so against an unpublished catalog every sign-in fails with "Stripe
has no active price published for: …" and nobody can get in at all. The same
holds for a new plan: publish it before the code that puts anyone on it ships.

The plans, the meters and the prices are published by
`lazycloud-admin billing publish-catalog` instead, and not because they are
unimportant. This platform addresses every one of them by a name it chose — a
product id, a price lookup key, a meter event name — so that nothing has to store
what Stripe generated. The Terraform provider marks a product's id computed and
cannot set one, so declaring the catalog here would hand the account
Stripe-assigned identifiers and force this repository to keep a lookup table in
step with them. The command reads before it writes, creates by deterministic
name, and never deletes. It refuses without `--confirm-account` naming the
account the credential in hand belongs to, and reports what is missing without
creating any of it until `--confirm`.

The billing portal configuration is absent for a duller reason: the provider has
no resource for it. Stripe auto-creates a default configuration the first time a
portal session is requested, which is what this account is running on today.

## The event list is not decoration

`enabled_events` in `main.tf` is exactly what `BillingWebhookService` acts on:
two events that say a card was saved, and four that say where a subscribed
account stands. An event outside that list is still delivered and then discarded
— so a longer list is not harmless, it is work the endpoint does for nothing, and
it reads as a claim that this platform reacts to deliveries it in fact ignores.
The list changes in the same commit that teaches the handler a new one.

None of the four standing events is believed. Each is a cue to read the
subscription back from Stripe, which is the one object that answers whether the
account is paid up, which cycle it is in, and whether it still exists — so a
delivery retried for days lands on what the account is now rather than on what it
was when the delivery was written. `invoice.payment_failed` is read as a
statement of standing and never as a cue to do anything about it: retrying a
refused card is Stripe's, and this platform having its own opinion about when to
try again is the defect the hand-built engine was deleted for.

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

`webhook_secret` becomes `LAZYCLOUD_STRIPE_WEBHOOK_SECRET` on the control plane.
Until it is set the endpoint refuses every delivery with
a 400 — deliberately, since the URL is public and what it carries changes which
card a customer is charged on.

Recreate the service after setting it; a running container does not re-read its
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
