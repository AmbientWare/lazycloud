# Configure Stripe billing

This Terraform module creates the webhook endpoint and selects the events
LazyCloud receives. Use it for a deployed API URL Stripe can reach.

## Prepare the account and state

Use a dedicated Stripe account and the private
[S3 state backend](../terraform-state/README.md). State contains the webhook
signing secret. Use a different state key for each live or test account.

Supply the operator key through your secret manager or read it in Bash:

```sh
read -rsp "Stripe operator key: " STRIPE_API_KEY; echo
export STRIPE_API_KEY
terraform -chdir=deploy/stripe init \
  -backend-config="$TF_VAR_terraform_backend_config" \
  -backend-config="key=stripe/production.tfstate"
```

Set the endpoint URL and environment in your private variable file.
`confirm_dedicated_account` must be true before Terraform will register it.

## Review and apply

```sh
terraform -chdir=deploy/stripe fmt -check
terraform -chdir=deploy/stripe validate
terraform -chdir=deploy/stripe plan -out=stripe.tfplan
```

Review the target account, URL, and event list before applying the saved plan:

```sh
terraform -chdir=deploy/stripe apply stripe.tfplan
```

`main.tf` names the supported card, subscription, credit-purchase, refund,
and dispute events. Keep it synchronized with the webhook handler when adding
billing behavior.

## Deliver the signing secret

Store the `webhook_secret` output as `LAZYCLOUD_STRIPE_WEBHOOK_SECRET` in
the deployment's operator secret document. Preserve every other property.
Refresh External Secrets and roll the consuming API pods through
`secretRevisions`; see [credential changes](../CONFIGURATION.md#credential-changes).

The API rejects deliveries without a valid signing secret. Confirm successful
delivery in Stripe and the API logs without printing the secret.

## Publish the billing catalog

From the repository root, with the target deployment configured:

```sh
uv run --group workspace lazycloud-admin billing publish-catalog --confirm-account <account-id>
```

Review the reported account, live/test mode, and missing entries. Add
`--confirm` to publish them. Then preview and publish rates:

```sh
uv run --group workspace lazycloud-admin billing publish-rates
uv run --group workspace lazycloud-admin billing publish-rates --confirm
```

Complete this before opening the installation to users. Kubernetes bootstrap
Jobs run these publications during sync. Catalog publication creates resources
by deterministic names and refuses conflicting existing values.
Terraform does not own catalog products or prices.

## Local development

Use Stripe test credentials and forward events to the local API:

```sh
stripe listen --forward-to localhost:8000/webhooks/stripe
```

Configure the session's signing secret on the local control plane. This flow
needs no Terraform webhook endpoint. Keep the listener running while testing.

The Terraform provider version is pinned in `versions.tf`. Review provider
changes through a pull request before updating that pin.
