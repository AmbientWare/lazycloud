# One resource: which events the payment provider sends, and where.

locals {
  # The two events that say a card was saved, which is how it becomes the
  # customer's default. Both, because either can arrive first: the hosted page
  # attaches the method and then succeeds the intent, and a card replaced through
  # the portal produces only the attach.
  card_events = [
    "payment_method.attached",
    "setup_intent.succeeded",
  ]

  # The four that say where a subscribed account stands. None is believed: each
  # is a cue to read the subscription back, which is the one object that answers
  # paid-up, which cycle, and still there or gone. `invoice.payment_failed` is
  # read as standing and never as a cue to retry a card — retrying is Stripe's,
  # and an opinion here about when to try again is the collection engine this
  # platform deleted.
  subscription_events = [
    "customer.subscription.deleted",
    "customer.subscription.updated",
    "invoice.paid",
    "invoice.payment_failed",
  ]
}

resource "stripe_webhook_endpoint" "billing" {
  url = var.webhook_url
  # Exactly what `BillingWebhookService` acts on, and nothing else. An event not
  # in this list is delivered and discarded — so a longer list is not harmless,
  # it is work the endpoint does for no reason, and it tells the next operator
  # that deliveries this platform ignores are acted on.
  enabled_events = concat(local.card_events, local.subscription_events)
  description    = "LazyCloud ${var.environment} billing deliveries"

  lifecycle {
    precondition {
      condition     = var.confirm_dedicated_account
      error_message = "Refusing to register an endpoint until confirm_dedicated_account is true."
    }
  }
}
