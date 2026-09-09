# Billing package

Billing owns accounts, admission, credit funding, plan changes and payment
recovery. `shared.payments` owns provider contracts, `observability` owns usage
metering, and `database` owns rates, ledger entries and durable payment records.

- Resolve the payer through `workspace -> owner -> account`. One user has one
  billing account across all owned workspaces. Provision the account, provider
  customer and subscription before completing sign-in.
- Insert the account before taking its registration lock. Hold that lock across
  provider registration and use an account-scoped provider idempotency key. This
  prevents duplicate customers from concurrent sign-ins and lost responses.
- Each account has one signed credit balance. Trial, subscription and purchased
  credit fund every metered resource. Purchased credit never expires. Other
  grants retain their effective and expiry dates. Allocate expiring credit first.
- Serialize grants, usage settlement and payment adjustments under the account
  lock. Price usage into immutable ledger entries and deduct each charge once.
  Preserve integer totals across allocation boundaries.
- Uncovered usage makes the balance negative. Later live credit pays debt first.
  A delayed grant that has expired may cover unpaid usage inside its original
  eligibility window, but cannot cover later usage or refunded-purchase debt.
  Deduct recorded usage even when no subscription period is active or funded.
- Local wallet usage never creates a provider meter event. Preserve historical
  exports and never charge their usage to the wallet again. Invoice reconciliation
  excludes wallet allocations, outstanding wallet debt, pending settlement, waivers and
  undelivered legacy exports. Never invoice a wallet shortfall a second time.
- Issue paid subscription credit only from a confirmed paid invoice and matching
  plan line. A saved card or active subscription is not payment evidence. Receipts,
  retries and renewals must not issue the same credit twice.
- Initial provisioning grants one account-scoped trial, valid for 30 days.
  Migration, extra workspaces, renewals and resubscription do not grant another.
  Free cycles have no recurring credit. Card changes never change the balance.
- Purchased-credit refunds are immutable adjustments. Preserve charged usage and
  allow the refund to create debt. Later funds pay that debt before new work.
- An account's complimentary flag waives usage without consuming credit or
  creating debt. Preserve the waiver in each settlement, or in its legacy outbox
  row, after the flag changes. Complimentary accounts use Team entitlements;
  platform authorization roles do not determine billing status.

- Subscription terms are immutable versions. Read existing subscriptions and paid
  invoice lines against their verified version. Unknown prices leave terms
  unavailable. Delayed funding retains the paid amount and covered interval.
- Paid upgrades take effect after collecting the prorated difference. Prorate
  included credit over the same interval, net of the prior plan's credit. Preserve
  previous grants and exports. Renewals grant their version's full included credit.
- Schedule cheaper terms at renewal and retain current entitlements until then.
  Selecting the held version cancels a scheduled change, including when that
  version is no longer sold. Preserve subscription identity, anniversary and
  metered items.
- The application owns plan changes and their credit funding. Use the hosted
  provider portal for payment methods and invoice history.
- Commit a plan-change intent before calling the provider. A partial unique
  index permits at most one open intent per account. Settle against its claim
  and the same live subscription. Never restore an ended subscription from an
  item or plan left in the provider response.
- A timed-out or refused plan-change call can still have collected payment.
  Reconcile observed success and retry unresolved outcomes through the intent.
  A read taken before an in-flight change finishes does not prove failure.
- Provisioning records every account field from the provider's response. An ended
  subscription clears both plan and subscription identity. Missing billing rows
  or subscriptions refuse billed work and are provisioned by billing entrypoints.

- Recovery may advance recorded plan-change intents and
  retry funding supported by payment evidence. Report unrelated standing or plan
  differences.
- Reconcile the newest finalized invoice covering a period with usage. An
  immediate proration invoice must not replace that period comparison.
- Backfill allowance totals from the immutable ledger when a delayed renewal
  opens the next period, including usage recorded before that period row existed.

- Admission reads local rows. Require a live subscription, acceptable payment
  standing, positive credit and remaining monthly usage budget. A saved card does
  not bypass these checks. No credit reservations or worker funding permits.
- The usage monitor stops compute through the normal stop path when credit or
  budget runs out. Interval and shutdown overage remains charged to the balance.
- Gate new CLI and dashboard transfers at existing authenticated endpoints.
  Already-started transfers may finish. Do not add a metered proxy or byte holds.
  Direct S3 downloads remain unbilled; supplier transfer costs belong in expenses.
- Retain platform-managed data for 30 days after credit runs out, without storage
  charges for that interval. Notify the owner. A positive balance closes retention.
  Recheck credit under the account lock before claiming deletion and preserve BYO
  infrastructure.
- Enforce concurrency across the payer's workspaces with a distinct capacity
  error. Count CPU containers separately from GPU cards. Resource limits bound
  exposure between usage records; they are not financial reservations.
- Resolve GPU wildcards to the plan's offered models before scheduling. Validate
  explicit models without substituting preferences. Gate other plan-limited
  resources through the same entitlement owner.
- Allow the first workspace before billing provisioning. Adopting an existing
  workspace is not creation and must not lock its owner out.

- The legacy outbox sends frozen rows written in the pricing transaction; it does
  not scan usage or recompute prices. Claim rows with fencing, settle each against
  its own claim, and pace failures so one account cannot block the queue.
- Keep retries inside the provider's deduplication window. The adapter owns
  provider limits and permanent-refusal classification. Resolve credentials before
  claiming work so missing credentials do not consume attempts.
- Keep sent, waived and abandoned rows as evidence. Abandonment emits one durable
  event naming the charge. Retryable failures stay on the row and in sweep counts.
  Query outstanding abandoned counts separately from the most recent drain.
