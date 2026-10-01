# Stripe webhook

This root registers the deployment's `POST /webhooks/stripe` endpoint and
the events billing reads. `main.tf` follows `processEvent` in
internal/billing/webhooks.go; change both together. State holds the
webhook signing secret; use one state key per Stripe account.

```sh
read -rsp "Stripe operator key: " STRIPE_API_KEY; echo
export STRIPE_API_KEY
terraform -chdir=deploy/terraform/stripe init \
  -backend-config="$TF_VAR_terraform_backend_config" \
  -backend-config="key=<the account's state key>"
terraform -chdir=deploy/terraform/stripe plan -out=stripe.tfplan
```

`confirm_dedicated_account` must be true before Terraform registers an
endpoint. Changing the event list updates the endpoint in place and keeps its
signing secret. A new endpoint returns a new `webhook_secret`: store it as
`LAZYCLOUD_STRIPE_WEBHOOK_SECRET` in the operator document, preserving the
other properties, and restart the server pods once External Secrets has
refreshed the Secret.

Rates ship with the code (internal/billing/ratecard.go). The server creates
each plan's product and monthly price on first use, found again by lookup
key `lazycloud-<terms version>`, so there is no catalog to publish.

Locally, `stripe listen --forward-to 127.0.0.1:8080/webhooks/stripe` needs
no endpoint here; give the server the session's signing secret.
