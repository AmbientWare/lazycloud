# The endpoint Stripe delivers billing events to, and the events
# internal/billing/webhooks.go processEvent reads; change both together.
# Its signing secret goes straight into the platform secret document.
resource "stripe_webhook_endpoint" "billing" {
  url         = "https://${var.domain}/webhooks/stripe"
  description = "LazyCloud ${var.deployment} billing"
  enabled_events = [
    "checkout.session.completed", "checkout.session.expired",
    "setup_intent.succeeded", "payment_method.attached", "payment_method.detached", "customer.updated",
    "payment_intent.succeeded", "payment_intent.payment_failed", "payment_intent.canceled", "payment_intent.requires_action",
    "charge.refunded",
    "charge.dispute.created", "charge.dispute.updated", "charge.dispute.closed",
    "charge.dispute.funds_reinstated", "charge.dispute.funds_withdrawn",
    "customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted",
    "invoice.paid", "invoice.payment_failed", "invoice.payment_succeeded",
  ]
}
