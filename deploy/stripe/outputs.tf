output "webhook_endpoint_id" {
  description = "Identifier of the endpoint, for locating it in the Stripe dashboard."
  value       = stripe_webhook_endpoint.billing.id
}

output "webhook_secret" {
  description = "Set as LAZYCLOUD_STRIPE_WEBHOOK_SECRET on the control plane and the scheduler."
  value       = stripe_webhook_endpoint.billing.secret
  sensitive   = true
}

output "enabled_events" {
  description = "Events this endpoint is subscribed to, for comparison against what the handler reads."
  value       = stripe_webhook_endpoint.billing.enabled_events
}
