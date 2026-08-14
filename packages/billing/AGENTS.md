# Billing Package

Who pays for a workspace, what standing they are in, the decisions that read
them, and the delivery of what they owe.

The payment provider protocol lives in `shared.payments`; the metering this is
billed from lives in `observability`; the rates, the priced ledger and the rows
waiting to be sent live in `database`. This package owns the account, the
admission decision, and the sweep.

- One billing account per user, never per workspace. Someone running dev, staging
  and prod holds three workspaces and one payment relationship, and resolution
  goes `workspace -> owner -> account` for the same reason a connected cloud
  account does.
- Every account that signs in gets its row, its provider customer and its
  subscription at sign-up, written before the session is minted, and a sign-in
  whose provisioning cannot be finished fails rather than proceeding. An account
  that exists but cannot be billed is the silent failure that shape removes:
  nobody discovers it until there is money to collect, and by then the work is
  already run.
- Registration is serialized on the account's own row, which is inserted before
  it is locked. A lock taken on a row that does not exist holds nothing, and a
  first registration is exactly the case with no row — so two simultaneous first
  sign-ins would both register a customer and the unique constraint would refuse
  one of them, turning somebody away for double-clicking. The lock is held across
  the provider call deliberately, because waiting is what the second caller does
  instead of registering a second customer; the read that answers every later
  sign-in takes no lock and calls nothing. The provider is asked under a key
  naming the account as well, which is a different failure: a registration whose
  answer was lost is retried, and without the key that retry is a second customer
  holding half the same person's invoices.
- Every account holds a subscription, and the free plan is a $0 one. Provisioning
  is a customer, a subscription carrying the plan's price and the three metered
  prices, the allowance period the subscription's own cycle defines, and the
  grant that funds it — one shape rather than two, so there is one billing flow,
  a plan change is a price swapped on the subscription that already exists, and
  free usage is metered into the provider like any other. The metered prices on
  the free subscription are load-bearing: without them, usage past what the plan
  includes reaches no invoice at all.
- A plan change never creates a subscription. The licensed item's price is
  swapped in place, so the subscription id, the billing anniversary and the three
  metered items survive and the usage already recorded this cycle is billed where
  it belongs.
- A plan change is recorded before it is attempted and settled after. The
  provider raises and collects the proration inside the call that swaps the
  price, and it cannot join a transaction here, so everything between that
  charge and the account row naming the new plan is a window where a customer
  has paid for terms this platform is not giving them — judged by admission on
  the allowance they left, and offered the button again. The intent row is what
  makes that window recoverable: it is committed first, the provider is called,
  and the outcome is written against the claim the intent was taken under.
- Asking the provider settles it, and the answer is definitive rather than
  likely. The swap is sent refusing to complete without payment, so the
  subscription carries the new plan's price only if the money was taken: "the
  item is on Team" and "the proration was collected" are one fact, readable in
  one request, with no invoice search and no timing window. That is what lets a
  sweep decide an intent nobody recorded an outcome for — the provider holds the
  plan, so the cycle and the row are given the terms that were paid for; it does
  not, so nothing happened and nothing is written.
- The plan is not the whole of the answer, because a subscription that has ended
  keeps the items it ended holding. A change is written back only onto the
  subscription it was made on and only while the provider still calls that
  subscription live; anything else is kept as evidence and reported, never
  written. The row and the plan together are what admission reads, so a settler
  that took the plan alone would put a live-looking subscription back on an
  account whose subscription is gone, buy the allowance that plan includes
  against a cycle nothing will invoice, and admit every container that follows —
  which is the state ending a subscription clears the row to prevent, recreated
  by the sweep that exists to protect the money.
- A refusal is not an answer. `error_if_incomplete` can fail after the invoice
  was raised, and a read taken behind a call that timed out cannot tell a
  provider that never moved from one still moving — so the request settles the
  change only where the provider already holds the plan, and paces everything
  else for the sweep. Closing the intent on that early read is the one outcome
  nothing recovers from: a swap that lands afterwards is a charge with no record
  that anybody meant it. A customer whose card was refused waits a schedule for
  their button rather than forever, which is the price of not guessing.
- At most one plan change is open per account, enforced by a partial unique
  index rather than by a lock. No transaction spans the provider call, so
  nothing serializes two simultaneous subscribes on the account row, and two of
  those are two prorations charged for one upgrade. The second caller is refused
  with a conflict, which is a button that says so rather than a bill that does
  not.
- A plan change corrects and reconciliation only reports, and the difference is
  the intent. The plan-change sweep is finishing a transaction this platform
  started and wrote down, so it knows what was meant and may complete it.
  Reconciliation observes objects nothing here recorded an intent about: a
  difference there may be a delivery that never arrived or a change somebody
  made in the provider's own dashboard, and making the two agree would be a
  money write on a guess. It writes to no billing table on any branch, and says
  what disagrees instead — plan, standing, cycle, and the last closed invoice's
  metered totals against the ledger less whatever the outbox has not delivered.
  One durable event per account per divergence, re-emitted only when what it
  disagrees about changes, because an hourly pass that reported every account
  every time would bury the report it exists to make.
- The invoice it compares is the newest finalized one covering a period there is
  usage in. A plan change is prorated onto an invoice raised there and then,
  which carries no metered line and covers no span; taken as the newest closed
  period it would compare an empty window against an empty invoice, agree, and
  leave the account with no usage reconciliation from its upgrade onwards — the
  event whose lost delivery this pass exists to catch.
- Usage the outbox gave up on is subtracted like a backlog and reported unlike
  one. Both are money that did not reach the invoice, so both come off the
  ledger before the arithmetic, or every account holding either reads as a
  disagreement about a figure that is not in dispute. But a backlog is delivered
  eventually and this is a charge nobody will make until somebody makes it, so
  it is a divergence of its own rather than a silence: the ledger-against-invoice
  comparison is the only place it is attributable to the account that lost it.
- What happens to the grant an account holds is decided from what writing the
  period did to it, never from which caller is asking. Re-terming the cycle in
  progress is a plan change, and its outgoing grant is expired before the
  replacement is bought so one grant covers the cycle; opening a cycle is a
  renewal, and its outgoing grant is left alone because that grant is what funds
  the invoice finalizing at that moment. Reading it off the period is what makes
  an upgrade landing in the renewal seam behave as a renewal, and a plan change
  arriving as a delivery behave as a plan change — the two are indistinguishable
  from the call site and obvious from the row.
- When a bought allowance becomes spendable is decided the same way, from the
  cycle that came before this one — which the same write reports, because these
  rows are the only record of it. The account row holds the newest grant and
  forgets the one before, and the outgoing grant a plan change expires says
  nothing about what came earlier. An account's first cycle follows nothing, and
  its allowance is spendable at once: held back for a predecessor that does not
  exist, the plan somebody has just paid for includes nothing until three days
  after they bought it.
- Provisioning writes the plan the provider answered with rather than the plan it
  asked for, and every column of the account row is stated on every write. A
  parameter that can be left out is one that keeps a subscription which ended or
  a grant which was expired, and both of those read as live to everything
  downstream.
- A subscription the provider says has ended clears the plan as well as the
  subscription. The two say one thing together — what this account is on — and a
  row that kept its plan would show the customer terms nothing holds them to,
  hide the way back onto a subscription, and satisfy admission while its usage
  reached no invoice.
- Absence of a row, and a row naming no plan, both mean an account on no
  subscription — one that has never reached a billing surface, such as the
  offline bootstrap administrator or one an administrator created through the
  users route, or one whose subscription the provider says has ended. Such an
  account is shown no plan and no allowance rather than terms nothing will hold
  it to, is refused new work, and is provisioned by the first billing surface it
  reaches.
- Admission asks one question: will what this runs reach an invoice somebody is
  paying? Never how much it costs. Every account's overage is billed by the
  provider and chased through their card, and only the provider knows whether it
  was collected — so spending past what a plan includes is something to invoice,
  not something to refuse on, and there is no second shape for the accounts that
  have not paid yet. Two things answer no: the provider has reported a payment
  did not go through, or the account holds no subscription for its usage to land
  on. The second covers an account never provisioned, one whose provisioning
  stopped part-way, and one whose subscription has ended — all of which would
  otherwise meter usage into a ledger and a meter that reach no invoice, which is
  unbounded compute nobody is charged for. Provisioning at sign-in is what makes
  that state unreachable; the refusal is what makes it a fact rather than an
  expectation. Read from the local row rather than the provider, because the
  question is asked on every container start and a balance call there is a start
  that fails whenever the provider is slow.
- The allowance counter loses nothing to the renewal seam. A cost priced between
  a cycle ending at the provider and the delivery that opens the next one here has
  no period to be counted against at that moment, and is read back from the ledger
  when that period opens rather than dropped — the ledger row was written in the
  same transaction, so the figure the customer is shown and the usage they are
  invoiced for stay the same number.
- The outbox is claimed, never derived. The sweep sends rows the pricer wrote in
  the transaction that priced the usage; it never scans usage, never groups, and
  never computes a cost. A sweep that recomputed its work set every run would put
  a permanently failing account at the front of every batch and hold everything
  behind it — the defect this shape exists to remove.
- Every row carries its own outcome, settled against the claim it was taken
  under. A refusal is paced into the future so the next claim passes over it, a
  row another drainer has reclaimed settles nothing, and neither says anything
  about the rows beside it.
- Delivery is at-least-once, and that is the guarantee rather than a compromise:
  the provider deduplicates on the row's `identifier`, so a send whose
  acknowledgement is lost costs a repeat the provider discards, where an
  exactly-once protocol would cost usage nobody is charged for. That protection
  lasts only as long as the provider's deduplication window, which is why the
  retry schedule here is bounded to about an hour rather than to a day: a retry
  that outlived the window would turn a lost acknowledgement into a second
  charge. How long the window is belongs to the provider adapter, not here.
- Terminal is stated, never inferred. An exhausted attempt count, or a payload
  the provider will always reject — malformed, or timestamped outside the period
  they still accept usage for — is abandoned with a durable
  `billing.meter_event.abandoned` error event naming the identifier, workspace,
  dimension and value, and the row is kept as the evidence of money that never
  left. Nothing here restates a provider's limits as a number of its own to keep
  in step.
- The abandoned backlog is a counted figure and not only an event. A row is
  abandoned once, is never pruned, and its delta is gone after the tick that
  produced it, so the standing count and what those rows metered are asked of
  the outbox — a number an operator watches, which only falls when somebody has
  answered for the money and removed the evidence. Asked rather than returned by
  the drain, because the provider is what makes a drain fail and a figure
  carried out of one would read zero for the length of the outage that grows it.
  It is what the usage was priced at rather than what it would have been billed:
  an account spends its allowance before it is charged for anything, so part of
  any total there would have reached an invoice at zero.
- A retryable failure is not an event. It is written to the row it belongs to and
  counted into the sweep's result, so a provider outage leaves a queryable
  backlog and a visible failure count instead of one durable event per row per
  tick. The credential is resolved before anything is claimed, because a claim
  spends an attempt and no number of retries fixes a key this process cannot
  read.
