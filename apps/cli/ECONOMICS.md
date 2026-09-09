# Realized economics

Run against the installation's database after the period closes. The command
reads a consistent database snapshot and makes no provider calls or writes.

```bash
uv run lazycloud-admin economics report \
  --from 2026-08-01T00:00:00Z --to 2026-09-01T00:00:00Z \
  --statement /private/august-financials.json \
  --operations /private/august-fleet.json
```

Omit either file to see which evidence is missing. Exit `2` means incomplete
financial evidence, `1` means contribution or operating loss, and `0` means
break-even or profit. Missing fleet observations appear separately and do not
turn a reconciled financial result into an estimate.

The report follows existing billing attribution. It includes each whole ledger
segment whose start is in `[from, to)`, including its credit allocations, and
checks settlements against all local-credit segments of their parent records.
Payment delivery windows keep charges on either side of credit cutover separate.
Crossing segment counts and gross amounts describe boundary exposure; they are
not extra revenue.
Expense statements must reconcile to this attribution before being supplied.

Financial input has `scope: "installation"`, timezone-qualified `started_at` and
`ended_at`, `attribution_basis: "segment_started_at"`, and an `entries` array.
Each entry has `component`, integer `amount_nanos` in USD nanodollars, and
`reference` naming the source statement line or reconciled schedule. Its `basis`
is `"actual_statement"`. For example, one fee line has this shape:

```json
{"component":"payment_processing_fees","amount_nanos":3250000000,"reference":"2026-08/payment-fees/line-1","basis":"actual_statement"}
```

Provide exactly one consolidated total per component. A missing total stays
unknown; an explicit zero requires evidence. Duplicate components or source lines
are rejected. Reconcile provider invoices and their credits first, then assign
each expense once. Never include a supplier invoice total beside its component
costs. Required components are:

- `gross_usage_control`: usage before local credits and waivers, excluding tax.
- `recognized_subscription_revenue`: earned subscription revenue for the period.
- `revenue_refunds_and_writeoffs`: revenue reductions excluding local credits and waivers.
- `supplier_compute_including_disk_and_ipv4`: platform workload compute and attached resources.
- `object_storage_capacity`: platform object storage capacity charges.
- `object_storage_operations`: object request and retrieval charges.
- `network_including_nat_and_cross_zone`: transfer, NAT and cross-zone charges.
- `payment_processing_fees`: actual processing charges, including top-ups.
- `fixed_infrastructure`: control-plane and other hosting costs excluded above.
- `prepaid_cash_received`: cash received for purchased credits during the period.
- `prepaid_cash_refunded`: purchased-credit cash returned during the period.
- `purchased_credit_opening_net_balance`: signed purchased-credit balance at period start.
- `purchased_credit_closing_net_balance`: signed purchased-credit balance at period end.

Cash and credit entries use their actual booking dates within the same period.
Only the two net balances may be negative. A positive balance is unspent purchased
credit owed to customers. A negative balance is refunded spent credit owed back
by customers. These totals exclude trial and subscription balances.

Opening net balance plus receipts minus refunds minus booked redemptions plus
`purchased_credit_offsets_booked_nanos` must equal closing net balance. Offsets
record other credit paying purchased-credit refund debt. Transfers between two
purchased lots cancel in that total. Promotional offsets also reduce recognized
revenue when booked; they do not create a second usage allocation.

Purchased credit redemption remains usage revenue. A top-up records cash and
purchased credit, without recognizing revenue. Trial and subscription allocations
reduce usage revenue. Historical records without local settlement remain a
reconciliation gap. Retain payment delivery records as financial evidence; their
absence cannot establish successful delivery.

The optional fleet file uses the same scope and exact period, with a `reference`.
`capacity`, `reserved_capacity` and `stranded_capacity` each contain observed
`cpu_core_seconds`, `memory_gib_seconds` and `gpu_card_seconds`. The other fields
are `provisioned_node_seconds`, `idle_node_seconds`, `interruption_count` and
`interruption_recovery_seconds`. Omitted observations stay unknown. Reservation
ratios use these observed intervals. Ledger quantities appear separately as
`billed_reservations`; they cannot establish physical occupancy across a period
boundary. Recovery time already overlaps purchased node time and adds no expense.

Current fleet snapshots and catalog prices remain estimates in the fleet cost
report. They are never accepted as realized supplier invoices here. Keep statement
files outside the repository.

`storage_access` reports deduplicated S3 requests and actual response bytes from
access logs. These logs can arrive late, contain duplicates, or omit requests.
The region field is evidence, not a paid-transfer classification. These requests
and bytes remain unbilled. Missing byte counts and requests without a workspace
stay visible. A workspace does not establish the historical payer. Supplier
invoices still supply the financial expense totals.
