# Billing and usage

Packet: billing (wave 3). Migration 0009, no protobuf range. Parity sections:
"Billing and plans" in tasks/parity.md, usage metering, and the plan-limit
seams other packets left (identity, storage, execution, images, domains,
compute).

## Outcome

An account's plan, credit and spending controls work as in the reference:
Free, Team and Business on the reference's current terms, one prepaid balance
funded by trial, subscription and purchased credit, per-second compute priced
from the published rate history, and every limit the rate card names. Usage
comes from container lifetimes, so the dashboard's usage page and the
balance read the same ledger.

## Reference trace (9e259ce75)

Billing in the reference is about 9,400 lines across packages/billing,
packages/shared billing modules, the Stripe provider, the observability usage
pipeline and eleven repositories. Its defects, which this packet removes:

- Usage arrived as 5-second worker windows priced per record under the
  account row lock, from an in-memory cursor: a restart double-charged or lost
  the tail, and every record serialized on the account.
- Every account, Free included, held a $0 Stripe subscription created at
  sign-in, so sign-in depended on Stripe.
- Uncovered usage was metered to Stripe meters through an outbox with twelve
  attempts and reconciliation, beside the local ledger: two authorities for
  one charge.
- Credit purchases and plan changes held the account row lock across Stripe
  calls.
- Credit allocation split each priced segment at every lot boundary and
  wrote an allocation row per slice.

## Design

Owner: `internal/billing`, one package for billing and usage metering.

| Concern | Decision |
| --- | --- |
| Account | The billing account of a workspace is its owner's user. `billing_accounts` is created on first use with the one-time trial lot ($2, 30 days). Free has no Stripe subscription; a Stripe customer exists only once the account saves a card, buys credit or changes plan. |
| Usage facts | A metering pass reads live containers and containers stopped since its last pass. Each container's `usage_cursors.billed_through` advances in batches, writing contiguous `ledger_entries` keyed by `(source, started_at)` that end on UTC quarter-hours, rate changes and the container's end. Billing starts at `ready_at` and ends at `stopped_at`; a lost host's containers end at its `last_seen_at`, and a live container is never billed past its host's last report. |
| Ledger | `ledger_entries` holds the usage fact and its price in one immutable row. A second 1:1 interval table would copy the key and the shape; storage and egress sources add `source_kind` values. |
| Balance | `billing_hours` sums cost per account and UTC hour in the metering transaction. A rollup claims due accounts (`billing_balances` row lock, `FOR UPDATE SKIP LOCKED`), covers uncovered hours oldest first from lots valid then (earliest expiry first; later credit pays earlier debt), and writes the balance and the month's spend. The open interval of each live container is written per pass as `accrued_nanos`, so admission and enforcement see spend up to the last host report. |
| Admission | `Admit` runs inside execution's submit and planning transactions and in image build starts: past due, zero balance and the monthly limit refuse with `payment_required`; concurrency caps new containers per account (CPU and GPU pools), and a cold submit at the cap is refused with `limit_reached`, as in the reference. |
| Enforcement | After each rollup, accounts with live containers and no funds, a reached monthly limit or a past-due subscription have their containers stopped by execution. |
| Stripe | stripe-go, provider only. Every call that creates or changes something is keyed by a row inserted first. No Stripe call runs inside a database transaction. Webhooks are verified, stored and processed by the scheduler, which re-fetches the object. |

### Removed relative to the reference

Stripe meters and the meter outbox, the $0 Free subscription, per-segment
credit allocation rows and wallet offsets, legacy subscription terms (a fresh
schema holds no legacy subscriber), billing reconciliation sweeps that
recomputed meter totals.

## Plan

1. Migration 0009, rate card, pricing and the catalog. *done*
2. Metering, rollup, enforcement; 10k-container measurement.
3. Admission and the seams: identity, execution, images.
4. Stripe: customers, card and portal sessions, credit purchases, automatic
   reload, plan changes, webhooks, subscription credit.
5. API operations and error codes; Python bindings.
6. Unfunded storage retention (needs the storage packet's tables).

## Progress

- [ ] Rate card and catalog
- [ ] Metering and rollup
- [ ] Admission and seams
- [ ] Stripe
- [ ] API
- [ ] Unfunded retention

## Intentional differences

(filled in as they are decided)

## Evidence

(filled in with test names and measurements)

## Gaps

(filled in)
