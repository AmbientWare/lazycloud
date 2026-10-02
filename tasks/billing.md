# Billing and usage

Packet: billing (wave 3). Schema: Billing in `migrations/0001_schema.sql`; no protobuf range. Parity sections:
"Billing and plans" in tasks/parity.md, usage metering, and the plan-limit
seams other packets left.

## Outcome

An account's plan, credit and spending controls work as in the reference:
Free, Team and Business on the reference's current terms, one prepaid balance
funded by trial, subscription and purchased credit, per-second compute priced
from the published rate history, and the limits the rate card names. Usage
comes from container lifetimes, so the usage page and the balance read the
same ledger.

## Reference trace (9e259ce75)

About 9,400 lines across packages/billing, the shared billing modules, the
Stripe provider, the observability usage pipeline and eleven repositories.
Defects this packet removes:

- Usage arrived as 5-second worker windows priced per record under the
  account row lock, from an in-memory cursor: a restart double-charged or lost
  the tail, and every record serialized on the account.
- Every account, Free included, held a $0 Stripe subscription created at
  sign-in, so sign-in depended on Stripe.
- Uncovered usage was metered to Stripe meters through an outbox with
  reconciliation, beside the local ledger: two authorities for one charge.
- Credit purchases and plan changes held the account row lock across Stripe
  calls.
- Credit allocation split each priced segment at every lot boundary and wrote
  an allocation row per slice.
- A scheduled downgrade never checked the account's held limits, though the
  docs say a change cannot go below them.

## Design

Owner: `internal/billing` (billing and usage metering in one package).

| Concern | Decision |
| --- | --- |
| Account | A workspace's billing account is its owner's user. `billing_accounts` is created on first use with the one-time trial lot ($2, 30 days). Free has no Stripe subscription; a Stripe customer exists only once the account saves a card, buys credit or changes plan. |
| Usage facts | Each metering pass (15 s, scheduler, session advisory lock) reads live containers and containers stopped since its last pass minus a 5-minute look-back. A container's `usage_cursors.billed_through` advances in batches of 500, writing contiguous `ledger_entries` keyed by `(source_kind, source_id, started_at)` that end on UTC quarter-hours, published rate changes and the container's end. Billing runs from `ready_at` to `stopped_at`; a host-lost container ends at the host's `last_seen_at`, and a live container is never billed past it. |
| Ledger | `ledger_entries` holds the usage fact and its price in one immutable row; a second 1:1 interval table would only copy the key and shape. |
| Balance | The metering transaction adds each entry's cost to `billing_hours` (account, UTC hour) and marks the balance due. The rollup claims due balances (`billing_balances` row, `FOR UPDATE SKIP LOCKED`, one transaction per account), covers uncovered hours oldest first from lots valid then (earliest expiry first; credit granted later pays earlier debt), and writes balance, month spend and the next recheck (lot expiry or month start). The open interval of live containers is written each pass as `accrued_nanos`. The balance row lock is the per-account lock: it also orders the rollup against metering marking the account due again, which an advisory lock would not. |
| Admission | `billing.Admit` runs in execution's submit and planning transactions and in image build starts: past due, no credit and the monthly limit refuse with `payment_required` (402); planning is capped at the account's concurrency (account advisory lock, so planning and builds count each other); a cold submit at the cap is refused with `limit_reached` (409), as in the reference. |
| Enforcement | After each rollup, accounts with live containers and no funds, a reached limit or a past-due subscription have their function containers stopped by `Execution.StopUnfunded` (drain; running attempts lost and retried). |
| Stripe | stripe-go v87, provider only. Every create/change call is keyed by a row committed first (account, credit purchase, plan change); no Stripe call runs in a database transaction. Webhooks are verified, stored in `stripe_events` and processed by the scheduler, which fetches the object again. `payment_method.detached` keeps the customer from previous attributes. |
| Retention | An account at zero credit that stores volumes, disks or artifacts gets an `unfunded_periods` row and the warning email in one transaction; credit ends it and withdraws an unsent email; after 30 days storage deletes the workspaces' data (`Storage.DeleteUnfunded`). |
| Rates | The reviewed rate history ships as data with the code (`ratecard.go`), each publication with its effective time; no admin tooling publishes rates. |

## Scope decisions

- No admin tooling beyond bootstrap subcommands. Complimentary accounts are
  set through the administrator-only API the dashboard's Admin → Users page
  uses. `server admin set-complimentary -email` exists because
  `deploy/local/run.sh` waives the local development account, which cannot
  pay.

## Progress

- [x] Rate card, pricing and the `/v1/pricing` catalog
- [x] Metering, ledger, rollup, enforcement; 10k-container measurement
- [x] Admission and seams: identity (workspaces, members), execution (submit,
  planning, builds), images (retried builds), storage (artifact retention,
  disk allowance)
- [x] Stripe: customers, card setup and portal sessions, Checkout credit
  purchases, automatic reload, plan changes (proration, scheduled downgrade,
  cancel scheduled change), subscription credit, past due, webhooks
- [x] Dashboard API and error codes; Python bindings regenerated
- [x] Unfunded retention with its email
- [x] Admin API: account list (search, role, status) and complimentary
- [x] Storage metering (volumes, artifacts per app, disks held and
  attached) and egress (closed quarter-hour totals), on the usage page
- [x] Containers record `gpu_count`, `gpu_type`, `rate_class` and
  `billing_owner`; planning fills count and rate class from the release spec
- [x] PR #424 review fixes, each with a regression test
- [x] stripe-mock in `compose.test.yaml` and CI; Stripe tests fail rather
  than skip without it
- [x] Compute seams. Assignment records the host's GPU model and whose
  machine it is (`platform` → `platform_fleet`, `connection` →
  `connected_cloud`, `machine` → `self_hosted`). Connecting AWS needs a
  plan that includes it. Connections count in `/v1/billing` usage and in
  plan-change fit. Evidence: compute
  `TestAssignmentRecordsWhoseMachineAndWhichGPUBillingPrices` and
  `TestConnectingAWSNeedsThePlanThatIncludesIt`.
- [x] Real Stripe test-mode flows (`TestStripeTestMode`). Server and
  scheduler read `LAZYCLOUD_STRIPE_API_KEY`, the reference's deployed secret
  name.

## API (for the web packet)

`GET /v1/pricing` (public), `GET /v1/billing`, `PUT /v1/billing/preferences`,
`POST /v1/billing/automatic-reload/resume`, `PUT /v1/billing/plan`,
`POST /v1/billing/payment-method-sessions`, `POST /v1/billing/portal-sessions`,
`POST /v1/billing/credit-purchases`, `GET /v1/billing/credit-purchases/{purchase}`,
`GET /v1/billing/costs`, `GET /v1/billing/cost-series`, and for administrators
`GET /v1/billing/accounts`, `PUT /v1/billing/accounts/{user}/complimentary`.
Stripe posts to `/webhooks/stripe`. `GET /v1/billing` folds the reference's
summary, credits, usage-budget, preferences and automatic-reload reads into
one response.

## Intentional differences

- Uncovered usage is never invoiced through Stripe meters; it stays a negative
  balance that the next credit covers first. One authority for each charge.
- Free accounts hold no Stripe subscription or customer until they need one.
- The trial starts when the account is first used for billing (first
  dashboard billing read or first work), not at sign-in.
- Compute bills from readiness: image pulls and handler import before
  `ready` are free. A lost host's containers stop billing at its last report.
- A cheaper plan change is checked against held limits too (the reference
  checked only upgrades).
- When a subscription ends the account returns to Free in good standing.
- Unfunded enforcement drains function containers; image builds finish.
- Wire shapes: GPU entitlements list models instead of `"all"`, unlimited
  limits are absent fields, placement multipliers are numbers, absent ids are
  absent rather than empty strings, pages use `next_cursor`.
- Only the current plan terms exist (`free-v2`, `team-v3`, `business-v2`); a
  fresh schema has no subscriber on earlier terms.
- The pricing catalog is `/v1/pricing` like every API path; the
  reference's `/api/v1` prefix is gone. docs/platform/plans.mdx links it and
  says compute bills from readiness.

## Evidence

Tests (real PostgreSQL; Stripe at the boundary through stripe-mock):

- Metering: `TestMeteringBillsReadyToStoppedOnTheGrid` (grid, idempotent
  repeat after a lost cursor), `TestMeteringStopsAtTheHostsLastReport`,
  `TestMeteringSplitsAtPublishedRateChanges`,
  `TestMeteringFindsContainersThatLivedBetweenPasses`,
  `TestMeteringReadsLiveAndRecentContainersOnly` (no history scans).
- Rollup: `TestTrialCoversUsageAndNewCreditPaysDebtFirst`,
  `TestExpiredCreditCoversOnlyUsageBeforeItsExpiry`,
  `TestSubscriptionCreditIsSpentBeforePurchasedCredit`,
  `TestComplimentaryUsageIsPricedAndWaived`,
  `TestReversalPastSpentCreditIsPaidFromOtherCredit`.
- Admission: `TestAdmitRefusesWorkTheAccountCannotPayFor`,
  `TestAdmitCapsContainersAtTheAccountsConcurrency`,
  `TestConcurrentStartsCountEachOther`,
  `TestWorkspaceAndMemberLimitsFollowThePlan`,
  `TestCapabilitiesFollowThePlan`, `TestRetentionFollowsThePlan`,
  `TestPlanChangesCannotGoBelowHeldLimits`; execution
  `TestPlanningStartsWhatTheAccountMayRun`,
  `TestSubmitNeedsCreditAndRoomForColdWork`,
  `TestUnfundedAccountsStopTheirContainers`; storage
  `TestDisksAreHeldToThePlansAllowance`,
  `TestUnfundedWorkspaceDataIsDeletedExceptWhatIsInUse`.
- Payments: `TestStripeAcceptsEveryRequest` (every call validated by
  stripe-mock), `TestReturnAddressesStayOnThePlatform`,
  `TestWebhooksAreVerifiedAndStoredOnce`,
  `TestPaymentOutcomesFundOnceAndTakeBackReversals`,
  `TestSubscriptionsSetTheAccountsPlan`,
  `TestIncludedCreditForNewUpgradedAndRenewedPeriods`,
  `TestUnfundedAccountsKeepTheirDataThirtyDaysAndAreWarned`.
- Storage, egress and shapes: `TestStorageIsMeteredAndShownOnTheUsagePage`,
  `TestEgressIsPricedOncePerClosedQuarter`,
  `TestHeldDisksPayForTheirSizeAndRetainedDataIsFree`,
  `TestContainersPriceTheirGPUPlacementAndMachine`,
  `TestAdmitCountsGPUsInTheirOwnPool`; execution
  `TestPlanningRecordsWhatBillingPrices`.
- Review fixes: `TestReloadDecidesFromASettledBalanceOnce`,
  `TestAutomaticPaymentIsNotRetriedOnceReloadIsOff`,
  `TestAPaidReloadWhoseResponseWasLostIsFoundByItsWebhook`,
  `TestMeteringMarksDueBehindAConcurrentRollup`,
  `TestRetentionKeepsDataOfAnAccountFundedOnTheLastDay`,
  `TestScheduledDowngradeHoldsAdditionsToTheCheaperPlan`,
  `TestConcurrentWorkspaceCreationsCountEachOther`,
  `TestAContainerReadLiveAndStoppedIsWrittenOnce`,
  `TestThePortalOffersOnlyInvoicesAndCards`; execution
  `TestStopUnfundedStopsEveryRunningContainerAndSkipsDraining`.
- API: `TestBillingOperationsActForTheCallersOwnAccount`,
  `TestOnlyAdministratorsSeeAndWaiveAccounts`,
  `TestStripeDeliveriesNeedTheEndpointSecret`.

Stripe test mode (`TestStripeTestMode`, 2026-10-01, about 21 s, API version
2026-09-30.endive). It runs when `LAZYCLOUD_TEST_STRIPE_API_KEY` holds a
test-mode key and refuses any other key. Deliveries are the events Stripe
recorded, signed and passed through `ReceiveWebhook` and `ProcessEvents`.
It covers:

- the customer, created on the first card setup page and reused;
- a test card attached, its `payment_method.attached` delivery making it
  the default;
- a credit Checkout, expired, its delivery cancelling the purchase;
- automatic reload charging the card off-session once and funding $20, its
  `payment_intent.succeeded` delivery and a second pass changing nothing;
- subscribing to Team, with included credit granted once (the
  `invoice.paid` delivery adds none);
- upgrading to Business with prorated credit;
- scheduling Team, a `customer.subscription.updated` delivery keeping the
  schedule, then cancelling it;
- scheduling Free, then cancelling that;
- the portal opening with invoices and cards only.

The customer is deleted afterwards. The plan prices and the portal
configuration stay in the test account because the platform reuses them.

The first run found that the current API version refuses
`payment_method_types` on Checkout. The Checkout sessions and the
off-session payment now use `allowed_payment_method_types`.

Measurements (`BenchmarkMetering10kContainers`, 1,000 accounts × 10 running
containers, local PostgreSQL 18, i9-12900K):

| Pass | Time |
| --- | --- |
| First pass: 10,000 cursors and about 30,000 entries written | 1.65 s |
| Steady pass between grid boundaries (accrual only) | 0.24 s |
| Rollup of 1,000 due accounts | 0.80 s (0.8 ms per account) |

At a 15-second cadence a 10k-container fleet costs one 0.24 s read pass, a
write pass each quarter-hour and about 40,000 ledger rows per hour. Submit
adds one live-container count and one account read to its transaction.

## Measured use

Each closed metering period of a container bills CPU and memory at the
greater of its reservation and what observability's metrics measured in
it: CPU core-seconds from the usage counters, memory byte-seconds from the
resident set, read from the samples while they are kept (an hour) and from
the folded minutes after, where a minute counts at its peak. The ledger row
records the shape it billed. Container time and GPUs bill as reserved.

A period closes only once observability's rollup watermark passes its end,
because ingest refuses samples older than the watermark; until then it is
accrued at its reservation, including a stopped container's last period,
which the stopped look-back keeps reading until the watermark passes it.
`TestMeteringBillsTheGreaterOfReservationAndMeasuredUse`,
`TestMeteringWaitsForMeasuredUseBeforeClosingAPeriod`.

Grouping costs by task splits each container ledger entry among the
attempts that ran in it by the time they overlapped it, at read time;
idle container time stays on the workload's row and concurrent attempts
share by overlap. `TestCostsByTaskSplitContainerTimeAmongItsRuns`.

## Gaps

- The cost of storage scans at scale is not measured; only the container
  pass is.
- Stripe test mode does not cover these:
  - completing a hosted Checkout page, which the API cannot do;
  - renewal invoices, which need a test clock;
  - declines and authentication-required cards.

  CI runs stripe-mock only. Its newest spec (v0.205.0) predates Checkout's
  `allowed_payment_method_types`, so the two Checkout calls are proven only
  by `TestStripeTestMode`. That test needs a test-mode key, which CI does
  not hold.
- Enforcement and retention are not exercised on the local stack end to end.
