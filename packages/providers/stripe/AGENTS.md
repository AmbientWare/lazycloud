# Stripe provider

Stripe owns payments, subscriptions, invoices, catalog publication and signed
webhooks. Billing owns local credits and allocations. This adapter translates
provider-neutral contracts and never reprices usage or decides admission.

- Catalog products, versioned price lookup keys and meter event names have
  repository-owned identities. Customer, payment, subscription and schedule IDs
  come from Stripe. Do not add catalog-ID environment variables.
- Publish the catalog through its command. Terraform cannot set product IDs;
  `deploy/stripe` manages the webhook endpoint and its returned secret.
- Create missing products and versioned prices. Refuse mismatched amounts rather
  than transferring lookup keys or retiring a price held by existing accounts.
- The rate card owns customer prices. Stripe metered prices convert nanodollars
  into currency and never repeat compute, storage or egress rates. Flat monthly
  subscription prices are the amounts catalog publication sends to Stripe.
- Gross ledger charges remain immutable. The meter outbox retains historical
  exports. New usage and wallet debt stay local and must not create a second
  charge through Stripe.
- Convert nanodollars to whole cents at Stripe boundaries that require cents.
  Refuse fractional-cent plan or grant amounts rather than rounding them.
- Each plan has its own product so invoices name it correctly. New subscriptions
  carry only the licensed plan item; metered usage is charged to local credit.
  Callers name plans and immutable terms, never catalog object IDs.

- Invoice reads verify customer identity, status and complete pagination.
  `invoices_for` accepts a bounded count or `None` for complete history.
- Paid invoice lines establish included-credit rights through their subscription,
  covered interval and verified immutable plan price. A product, metadata label,
  grant category or saved card is not payment evidence.
- Purchased credit requires the matching captured payment. Read refunds and
  disputes from that charge, verify identities and currency, and exhaust every
  page. Billing records the resulting immutable adjustments.

- Immediate upgrades replace the licensed item with paid proration. Cheaper terms
  use an owned subscription schedule and take effect at renewal. Preserve other
  subscription items. Selecting held terms releases the schedule.
- Schedule creation from a subscription cannot attach ownership metadata
  atomically. Recover partial creation by replaying the durable intent's exact
  idempotency key within 20 hours and checking the attached ID. After metadata
  exists, verify it. Never adopt an unrelated or unverified schedule.
- Customer registration uses the account ID as its idempotency key. Provider
  keys protect lost responses only while Stripe retains them; durable account
  locks protect concurrent registration.
- Subscription creation first lists the customer's live subscriptions. An
  account-only creation key could return a canceled subscription during Stripe's
  retention window and prevent resubscription.

- Read subscription periods from their items and refresh provider state when
  processing deliveries. A delayed webhook must not move a period backwards.

- Preserve Stripe's status and event vocabularies at the adapter boundary. The
  domain owns which values trigger workflows.
- Stripe deduplicates meter identifiers for 24 hours and refuses timestamps more
  than 35 days old. The billing outbox must stay within those retry bounds.
- Invalid meter payloads and aged-out timestamps are permanent failures checked
  before sending. Other provider refusals, including rate limits and credentials,
  are retryable. An unpublished meter must not abandon an entire backlog.
- Hosted pages own card entry and invoice viewing. Card details never reach this
  process; return provider identifiers and hosted URLs only.
