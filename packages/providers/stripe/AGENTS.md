# Stripe

- Translate payments/subscriptions/webhooks; billing owns credits and admission.
  New wallet usage/debt must never create a second provider charge.
- Catalog identities are code-owned. Publish versioned prices through the catalog
  command; refuse mismatches and preserve prices held by existing subscriptions.
- The rate card owns rates. Convert to cents only at required boundaries and
  refuse fractional cents. Keep raw card data on hosted provider pages.
- Verify invoice/payment customer, currency, status, subscription, interval and
  immutable terms; exhaust pagination for complete evidence. Cards/metadata do
  not prove payment. Refunds/disputes follow the captured charge.
- Upgrades collect proration; cheaper terms use an owned schedule at renewal.
  Preserve other items and verify schedule ownership. Recover partial schedule
  creation with its exact durable intent key within the 20-hour recovery window.
- Registration uses account idempotency plus durable locking. Before creating a
  subscription, inspect live subscriptions; do not reuse a canceled response.
- Refresh state for webhooks; delayed delivery cannot move periods backward.
  Preserve provider status vocabulary for domain decisions.
- Legacy meter retries respect 24-hour deduplication and 35-day timestamp limits.
  Invalid/aged payloads are permanent; configuration and provider refusals remain
  retryable so an unpublished meter cannot abandon a backlog.
