# One resource: which events the payment provider sends, and where. The list
# follows internal/billing/webhooks.go processEvent.

locals {
  # A card saved, which makes it the customer's default (either of the last
  # two can arrive first), and a card removed or changed, which refreshes the
  # one on file.
  card_events = [
    "customer.updated",
    "payment_method.attached",
    "payment_method.detached",
    "setup_intent.succeeded",
  ]

  # Where a subscribed account stands. None is believed: each
  # is a cue to read the subscription back, which is the one object that answers
  # paid-up, which cycle, and still there or gone. `invoice.payment_failed` is
  # read as standing and never as a cue to retry a card — retrying is Stripe's,
  # and an opinion here about when to try again is the collection engine this
  # platform deleted.
  subscription_events = [
    "customer.subscription.created",
    "customer.subscription.deleted",
    "customer.subscription.updated",
    "invoice.paid",
    "invoice.payment_failed",
    "invoice.payment_succeeded",
  ]

  credit_purchase_events = [
    "checkout.session.completed",
    "checkout.session.expired",
    "payment_intent.succeeded",
    "payment_intent.payment_failed",
    "payment_intent.canceled",
    "payment_intent.requires_action",
    "charge.refunded",
    "charge.dispute.created",
    "charge.dispute.updated",
    "charge.dispute.closed",
    "charge.dispute.funds_reinstated",
    "charge.dispute.funds_withdrawn",
  ]
}

resource "stripe_webhook_endpoint" "billing" {
  url            = var.webhook_url
  enabled_events = concat(local.card_events, local.subscription_events, local.credit_purchase_events)
  description    = "LazyCloud ${var.environment} billing deliveries"

  lifecycle {
    precondition {
      condition     = var.confirm_dedicated_account
      error_message = "Refusing to register an endpoint until confirm_dedicated_account is true."
    }
  }
}
