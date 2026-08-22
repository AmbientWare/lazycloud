# Stripe provider

The payment relationship behind one billing account: the customer, the catalog
they are sold from, the subscription and allowance they hold, the usage reported
against them, and the signature on what Stripe sends back.

- Stripe owns the money side of the relationship: the subscription a customer is
  on, the allowance it comes with, the invoice, and the retries when a card is
  refused. Collection machinery written here would be a second implementation of
  a system that already exists, and the three audits that found starvation,
  unpaced retries and dropped months were all reading ours rather than theirs.
- What this platform owns is metering, the rate card that turns it into money,
  the ledger that attributes a cost to an app, and admission. Metering is what
  ran, on whose hardware, and how many core-seconds, gibibyte-seconds and
  card-seconds it held or burnt. One meter per billed dimension, however many
  components the ledger breaks a dimension into: a meter is an invoice line, and
  adding one is a live account change. None of those is anything Stripe can
  observe: their meters are aggregates per customer and cannot say which app
  spent the money, and a balance call in the admission path would be a network
  round trip on every container start.
- Every object is addressed by a name this repository chose: a product id, a
  price lookup key, a meter event name. Nothing stores an identifier Stripe
  generated, so there is no lookup table to keep in step with the account and no
  way for one process to be pointed at a different price than another. The
  constants are constants for that reason. An environment variable is how the
  publisher, the drainer and the reconciler come to disagree.
- The catalog is published by a command rather than declared in Terraform. The
  Terraform provider marks a product's id computed and cannot set one, which
  would leave the account holding Stripe-assigned identifiers this repository
  would then have to store, precisely the lookup table the naming above exists
  to avoid. `deploy/stripe` keeps only the webhook endpoint, the one object whose
  creation returns a secret that has to survive somewhere.
- Publishing reads before it writes, creates only what is missing, and never
  deletes or edits. A price's amount cannot be changed at Stripe, so an existing
  object that disagrees with this repository is refused by name and figure rather
  than worked around; the fix is a new price and a moved lookup key, which is a
  decision with customers on the other side of it.
- A meter event carries the ledger segment's `cost_nanos` verbatim, and the
  metered prices are one nanodollar per unit. Nothing is rederived on the way
  out, so an invoice and the ledger behind it compare as exact integers and a
  difference is a fact rather than a rounding argument. That comparison is the
  only guard that what was sent is what was billed, and it changes nothing at
  Stripe.
- Invoices are listed, never stored. `invoices_for` reads one page of a
  customer's recent bills so the comparison above can be made without an invoice
  identifier arriving from somewhere: an invoice is Stripe's record, and keeping
  a copy of their list here would be a second ledger to hold in step with theirs.
  It is bounded by a window and a count because the reconciler reads it per
  account, and the reconciler takes one invoice from it, the newest whose period
  has closed and covers a span, since a period still being assembled has a total
  that has not stopped moving, and one raised for a proration covers an instant
  that no usage falls inside. An invoice Stripe listed without a status is
  skipped: nothing here can classify one, and their vocabulary is theirs to
  extend.
- Money crosses this boundary in nanodollars, because that is the only unit the
  rest of the platform counts in. Usage crosses as nanodollars through the
  metered prices; the two flat figures Stripe insists on in cents, a plan price
  and an allowance grant, are converted here and a figure that is not a whole
  number of cents is refused rather than rounded. Rounding would publish a price
  nobody chose, and it would do it to the one number a customer checks against
  their statement.
- The catalog holds a plan line per plan an account can be on, each with its own
  product and price, beside the three usage lines. A product per plan rather than
  one priced twice, because the product name is what the invoice line is called.
- No caller names a catalog object. `create_subscription` takes a customer and a
  plan and puts them on this package's own published prices for it, so a
  subscription cannot be assembled from the wrong lines by a caller holding a
  stale list, and no provider-neutral package has to speak the word "lookup key"
  to sell a plan. Every subscription carries all three metered prices whatever
  plan it is on, including the plan priced at zero; without them there is
  nowhere for that account's overage to land.
- A plan change is `set_subscription_plan`, which swaps the price on the existing
  licensed item by item id and prorates it onto an invoice raised immediately. It
  is never a second subscription: that would carry the same metered prices, and a
  customer's usage counted onto two invoices is a charge nobody can explain. It
  is idempotent by reading first, so a subscription already on the plan comes
  back unchanged, because the caller is a transaction that can die between
  changing the plan and recording it.
- Registering a customer and granting an allowance carry an idempotency key. The
  customer's is the account id; a grant's is the account with the amount and
  expiry, because a plan change buys a different grant inside the same cycle and
  must not be mistaken for the retry of the one it replaces. The keys cover
  crash-before-commit only, for as long as Stripe remembers them; the durable
  protection is the account row and the lock held over it.
- `create_subscription` carries no key and is idempotent by listing the
  customer's live subscriptions first, the way a plan change reads the
  subscription first. A key derived from the account alone would answer a whole
  day of requests with the subscription that account used to hold, so a customer
  whose subscription was cancelled could not be given a new one, and there is no
  value here that changes exactly when a second subscription is wanted, which is
  the only kind a key may be derived from. Reading also outlives the day Stripe
  remembers a key, which the crash it protects against does not.
- A credit grant expires `CREDIT_GRANT_SETTLEMENT_GRACE` after the cycle it
  funds. Credit is applied when an invoice is finalized rather than when it is
  raised, so a grant has to outlive its own period to reach its invoice.
- When a grant becomes spendable is a fact about the cycle *before* it and never
  about the cycle it funds. One bought for a cycle that follows another is held
  back until that cycle has had the same grace to settle, or an overrun there is
  paid out of this cycle's allowance. Overrun is the design rather than an edge
  case, so that is the ordinary path. One that follows nothing says nothing about
  its start, and Stripe stamps it on the clock the customer's own subscription
  runs on. The caller names the cycle before, because holding an account's first
  allowance back is a customer charged for the three days before they could spend
  what they were told they had, and a plan change bought minutes after sign-up is
  exactly that account.
- No timestamp this host computed is ever sent as a start. Stripe refuses any
  `effective_at` at or before their own now, measured rather than read, so two
  seconds back is refused as flatly as an hour. A deferred start is therefore a
  value that cannot be sent late, and a clamp to this host's clock would put a
  test-clock customer's allowance a month into their own future. The host clock
  decides only whether the cycle before can still be settling, which is a
  question about the past.
- Stripe will not expire a grant that has not become effective yet, and will not
  void one that is already over. So expiring is tried first and voiding is what
  answers the refusal: a grant that has funded an invoice is ended and keeps what
  it paid for, and one that was never spendable is invalidated, which is the only
  way to stop it. Ending an allowance is never allowed to fail. The caller has
  already collected the customer's money for the plan whose allowance it is
  replacing, so a refusal here is a charge with no plan recorded against it.
- Stripe deduplicates a meter event on its `identifier` for 24 hours and refuses
  a timestamp older than 35 days. The first is what makes at-least-once delivery
  safe rather than a compromise, and it is the ceiling every retry schedule that
  sends meter events must fit under. The second is why a queued event that has
  aged out is terminal: no number of attempts will make it acceptable.
- A subscription's billing period lives on its items, not on the subscription.
  Items created together share one cycle, so the span across them is that cycle.
  It is read back from Stripe rather than taken from the delivery that named it,
  because deliveries are retried for days and one describing a period that has
  since rolled would move an allowance backwards.
- The provider's own words for subscription status and event type are carried
  through rather than mapped to an enum here. Stripe owns those vocabularies and
  adds to them, and a value this package had not been taught would turn a
  delivery into a rejection. The domain owns the set it acts on.
- A rate limit and a rejected credential are retryable despite being 4xx. Neither
  is the caller's to fix, and treating either as terminal leaves a customer
  unable to save a card for a reason they cannot act on and abandons a meter
  event a re-issued key would have carried.
- Which meter-event refusals are permanent is decided here, because the limits
  behind them are Stripe's. Exactly two are: a payload this package can see is
  unacceptable, and a timestamp aged out of the backfill window. Both are checked
  before the request, so neither depends on reading a message Stripe writes.
  Everything else they decline is the account rather than the event, since an
  unpublished meter answers the same way a malformed one does, and reporting it
  as unavailability is what stops the first drain against an unpublished account
  from abandoning every row in every batch with no way back.
- Card details never reach this process. Both pages are hosted by Stripe and what
  comes back is an identifier, which is what keeps this repository outside the
  scope of cardholder data rules. Anything that would return a card number, a
  last four, or a brand belongs on their page instead.
