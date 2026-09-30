# Billing

- Own accounts, admission, credits, plan changes and recovery. Resolve the payer
  through workspace → owner → account; metering belongs to observability.
- Insert accounts before locking registration; use account-scoped provider
  idempotency. Complete provisioning before sign-in succeeds.
- Serialize funding, settlement and adjustments under the account lock. Keep
  ledger charges immutable, allocate expiring credit first and charge usage once.
- Preserve grant eligibility windows and integer totals. Purchased credit never
  expires; later live funds pay debt first. Refunds are new adjustments and may
  create debt. Delayed expired grants cannot pay later usage or refund debt.
- Grant one account-scoped trial. Fund subscriptions only from verified paid
  invoice lines; cards, renewals and extra workspaces never create another trial.
- Complimentary status waives usage without debt and retains settlement evidence;
  it grants Business entitlements independently of platform authorization roles.
- Keep subscription terms versioned. Paid upgrades collect prorated differences;
  cheaper terms wait for renewal. Preserve subscription identity, prior grants
  and unrelated items. Unknown prices do not establish entitlements.
- Commit plan-change intents before provider calls; allow one open intent per
  account. Fence settlement to that intent and live subscription. Reconcile
  uncertain outcomes instead of treating a timeout or stale read as failure.
- Missing/ended subscriptions refuse billed work. Admission reads local standing,
  credit and budget; warm admission uses one unlocked committed snapshot. New
  container reservations lock the account through capacity validation and write.
- Enforce account-wide concurrency separately from financial limits. Use the
  normal stop path on exhaustion and bill shutdown overage. No money reservations,
  execution permits, transfer byte holds or duplicate provider usage charges.
- Managed storage gets 30 days of uncharged retention after exhaustion. Notify,
  preserve BYO data and recheck credit under the account lock before deletion.
- Resolve GPU choices through entitlements. Allow initial workspace provisioning;
  adopting an existing workspace is not creation.
- Legacy exports remain frozen. Fence outbox claims, stay within provider retry
  windows and resolve credentials before claiming. Retain sent/waived/abandoned
  evidence and durable errors; one failing account must not block the queue.
