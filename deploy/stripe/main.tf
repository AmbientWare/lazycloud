# One resource, deliberately.
#
# The provider offers products, prices, coupons, tax rates, billing meters and a
# whole v2 pricing-plan family. None of them belong here:
# `packages/providers/stripe/AGENTS.md` states that this platform computes what is
# owed and hands over finished amounts, and that an adapter holding a price would
# become a second answer to a question the platform already answers. A catalog
# declared here would be exactly that, one layer further out.
#
# What the payment provider genuinely owns is the inbound direction: which events
# it sends, and where. That is this file.

locals {
  # Exactly what `BillingWebhookService` acts on, and nothing else. Anything
  # matching `INVOICE_EVENT_PREFIX` is read as "this invoice changed, go look",
  # and the two card events are how a saved card becomes the customer's default.
  # An event not in this list is delivered, claimed, and discarded — so a longer
  # list is not harmless, it is work the endpoint does for no reason.
  invoice_events = [
    "invoice.finalized",
    "invoice.paid",
    "invoice.payment_failed",
    "invoice.payment_succeeded",
    "invoice.voided",
  ]

  card_events = [
    "payment_method.attached",
    "setup_intent.succeeded",
  ]
}

resource "stripe_webhook_endpoint" "billing" {
  url            = var.webhook_url
  enabled_events = concat(local.invoice_events, local.card_events)
  description    = "LazyCloud ${var.environment} billing outcomes"

  lifecycle {
    precondition {
      condition     = var.confirm_dedicated_account
      error_message = "Refusing to register an endpoint until confirm_dedicated_account is true."
    }
  }
}
