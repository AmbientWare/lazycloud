# Billing package

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
  first registration is exactly the case with no row, so two simultaneous first
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
  grant that funds it. One shape rather than two, so there is one billing flow,
  a plan change is a price swapped on the subscription that already exists, and
  free usage is metered into the provider like any other. The free subscription
  needs its metered prices: without them, usage past what the plan includes
  reaches no invoice at all.
- A plan change never creates a subscription, and moving down never ends one.
  The licensed item's price is swapped in place in both directions, so the
  subscription id, the billing anniversary and the three metered items survive
  and the usage already recorded this cycle is billed where it belongs. Ending
  the subscription instead would take the metered prices with it, so containers
  still running would meter onto meters no subscription item references and reach
  no invoice at all. The account row cleared for an ended subscription then
  refuses every new container, so a paying customer who pressed cancel would be
  locked out within seconds and put back on a fresh subscription by the next
  billing route they touched. "Cancel" is the free plan's price, and it is the
  same request as any other change.
- Which direction a change goes in decides what happens to the part of the cycle
  already invoiced, and the caller states it rather than the adapter guessing.
  Dearer takes the difference at once, which is what makes the provider holding
  the plan proof that the money was collected. Cheaper takes nothing and returns
  nothing: the month was invoiced when the cycle opened, and refunding part of it
  would hand back money for compute the account was free to spend and mostly has.
  So a move down is cancel-at-period-end in economic effect with no scheduling
  machinery. The price swaps now, the next invoice is the smaller one.
- An allowance is never reduced inside the period it was stamped on, whichever
  caller writes that period. What a customer was given when the cycle opened is
  what they spent against while it ran, and re-terming it downwards mid-cycle
  would leave the smaller figure standing in front of usage that was included
  when it happened. The provider applies credit at finalization, so the invoice
  would ask for the difference and bill somebody for compute their plan had
  already covered. The cycle keeps its terms and its spend, nothing is expired,
  nothing is bought, and the smaller plan applies from the next cycle. How much
  may run at once is not stamped on the period and is read live, so that does
  drop at once; the asymmetry is stated to the customer rather than smoothed
  over. It is also what makes the grant idempotency key unreachable for a grant
  that was expired: expiry happens only on a re-term, and a re-term now implies a
  different amount or a different expiry.
- The swap is this platform's own because the provider's hosted portal cannot do
  it. Their portal refuses to *update* a subscription that carries multiple
  products or usage-based prices, and this one carries both by design; the plan
  price beside the three metered prices is what makes free usage meter like any
  other. A customer sent to the portal to change plan would find only the option
  to cancel, and cancelling takes the metered prices with it and leaves their
  usage reaching no invoice at all.

  So this is not a hosted flow reimplemented for preference. It is the case the
  provider hands back, and the reason to keep it is not visible from the code:
  read on its own, `BillingPlanChangeService` and its intent table look exactly
  like something a portal redirect would replace. The portal stays what it
  already is here, replacing a card and reading past invoices, and a plan changed
  in the provider's own dashboard still arrives as a delivery and is settled the
  same way.
- A plan change is recorded before it is attempted and settled after. The
  provider raises and collects the proration inside the call that swaps the
  price, and it cannot join a transaction here, so everything between that
  charge and the account row naming the new plan is a window where a customer
  has paid for terms this platform is not giving them. They are judged by
  admission on the allowance they left, and offered the button again. The intent
  row is what makes that window recoverable: it is committed first, the provider
  is called, and the outcome is written against the claim the intent was taken
  under.
- Asking the provider settles it, and the answer is definitive rather than
  likely. The swap is sent refusing to complete without payment, so where it
  charges a difference the subscription carries the new plan's price only if the
  money was taken: "the item is on Team" and "the proration was collected" are
  one fact, readable in one request, with no invoice search and no timing window.
  Downwards there is nothing to collect and so nothing that could have failed to,
  and the plan being there is the whole of what happened. Either way the sweep
  can decide an intent nobody recorded an outcome for. The provider holds the
  plan, so the cycle and the row are given those terms; or it does not, so
  nothing happened and nothing is written.
- The plan is not the whole of the answer, because a subscription that has ended
  keeps the items it ended holding. A change is written back only onto the
  subscription it was made on and only while the provider still calls that
  subscription live; anything else is kept as evidence and reported, never
  written. The row and the plan together are what admission reads, so a settler
  that took the plan alone would put a live-looking subscription back on an
  account whose subscription is gone, buy the allowance that plan includes
  against a cycle nothing will invoice, and admit every container that follows.
  That is the state ending a subscription clears the row to prevent, recreated
  by the sweep that exists to protect the money.
- A refusal is not an answer. `error_if_incomplete` can fail after the invoice
  was raised, and a read taken behind a call that timed out cannot tell a
  provider that never moved from one still moving. So the request settles the
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
  what disagrees instead: plan, standing, cycle, and the last closed invoice's
  metered totals against the ledger less whatever the outbox has not delivered.
  One durable event per account per divergence, re-emitted only when what it
  disagrees about changes, because an hourly pass that reported every account
  every time would bury the report it exists to make.
- The invoice it compares is the newest finalized one covering a period there is
  usage in. A plan change is prorated onto an invoice raised there and then,
  which carries no metered line and covers no span; taken as the newest closed
  period it would compare an empty window against an empty invoice, agree, and
  leave the account with no usage reconciliation from its upgrade onwards, and
  an upgrade is exactly the event whose lost delivery this pass exists to catch.
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
  arriving as a delivery behave as a plan change. The two are indistinguishable
  from the call site and obvious from the row.
- When a bought allowance becomes spendable is decided the same way, from the
  cycle that came before this one, which the same write reports, because these
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
  subscription. The two say one thing together, what this account is on, and a
  row that kept its plan would show the customer terms nothing holds them to,
  hide the way back onto a subscription, and satisfy admission while its usage
  reached no invoice.
- Absence of a row, and a row naming no plan, both mean an account on no
  subscription: one that has never reached a route that provisions billing, such
  as the offline bootstrap administrator or one an administrator created through
  the users route, or one whose subscription the provider says has ended. Such an
  account is shown no plan and no allowance rather than terms nothing will hold
  it to, is refused new work, and is provisioned by the first billing route it
  reaches.
- Admission asks whether what this runs will reach an invoice somebody is paying.
  For an account somebody can bill, that is the whole question and the amount is
  no part of it: overage is billed by the provider and chased through their card,
  only the provider knows whether it was collected, and spending past what a plan
  includes is something to invoice rather than something to refuse on. Two things
  answer no. The provider has reported a payment did not go through; or the
  account holds no subscription for its usage to land on, which covers an account
  never provisioned, one whose provisioning stopped part-way, and one whose
  subscription has ended. All of those would otherwise meter usage into a ledger
  and a meter that reach no invoice, which is unbounded compute nobody is charged
  for. Provisioning at sign-in is what makes that state unreachable; the refusal
  is what makes it a fact rather than an expectation.
- An administrator can waive an account's bill, and the waiver is a column on
  the account rather than a plan. A plan is something the provider prices and the
  rate card publishes; the rate card refuses a plan id it has no price for, and
  the account row's check constraint refuses a plan value it does not know. A
  waived account is held to the Team plan's terms as if a card were on file, so
  the concurrency ceiling still bounds what the platform is exposed to, and its
  subscription is left untouched so withdrawing the waiver puts it back on that
  subscription with nothing to provision. The waiver is never read off the
  platform role. Who pays is a billing fact and who administers is an
  authorization fact, and a demotion must not switch somebody's bill on. Usage
  is still priced into the ledger so the account can see it, and the meter
  event that would carry it to the provider is written as `waived` rather than
  left out. Every
  priced record then owes exactly one outbox row whatever the account's standing
  was, and the row is what tells reconciliation afterwards why that window
  reached no invoice, since the waiver itself may be gone by then. Waived rows
  are subtracted before the invoice comparison like abandoned ones and are no
  divergence, and like abandoned ones they are never pruned.
- An account with no card on file is the case that reasoning does not cover, and
  it is the one place an amount decides. There is no card to chase and no invoice
  that will ever be paid, so what such an account spends past its allowance is not
  billed late. It is lost, against hardware already paid for. So a cardless
  account is refused once its allowance is gone, and only a cardless account is:
  attaching a card moves it onto the plan's terms and out of this check for good.
  What it may spend before that is deliberately small, because every figure in it
  is money the platform has decided to give away to find out whether it can bill
  anybody.
- Every billed thing asks that question, not only a container, and the method is
  named for the question rather than for what is asking. A volume asks it before
  it exists; a third billable resource asks the same one and adds no method. Only
  a container carries a count and a shape, so only a container has a second.
- The container method answers with the GPU models to schedule rather than only
  with yes. A request for `any` card is a request the plan narrows, and narrowing
  it here is the only place that can happen before the scheduler acts on it.
  Passed through, the first offer taken would be whatever the fleet had spare,
  which on a plan that sells three models is usually one of the other five. A
  request naming models comes back unchanged once every one of them is a model
  the plan offers, because substituting a stated preference would run something
  other than what was asked for. A model the plan does not offer is a payment
  refusal naming the model and the plan that does offer it, since that is a
  customer who has to decide something rather than one who has to wait.
- A volume is admitted on creation alone, and the remainder is deliberate rather
  than overlooked. Storage is the one billed dimension that keeps accruing with
  nothing running, so the sweep that stops containers can do nothing about it,
  which is why the refusal has to happen before the volume exists. But resolving
  a volume that already exists is never refused: that is how a container mounts
  one and how its owner reads their own files back, and locking an account out of
  its data to collect a few cents is a data-loss incident wearing a billing
  control's clothes. Nothing bounds how large an existing volume grows either.
  Uploads are not admitted and there is no size quota, so the gate shrinks the
  window rather than closing it; closing it needs a quota or an admission on the
  write path, and neither exists. Cloning a stub also creates volumes without
  asking, which is tolerable only because a volume is priced on byte-seconds and
  an empty one is free.
- Concurrency is refused separately and with a different error, because an
  account at its limit owes nothing and paying would not help it. It is a bound
  on how much one account can have running before anything notices, which matters
  because usage reaches the ledger on an interval and spend is therefore always
  seen slightly late; the limit is what keeps the size of that blind spot
  proportional. Counted across every workspace the account owns, since the limit
  is a term of a plan and making another workspace is self-serve. Deliberately
  approximate under concurrent starts. Closing that gap would put a per-account
  exclusive lock in the path of every autoscaler ramp to protect a guardrail
  whose overshoot self-corrects and is invoiced like anything else.
- Two concurrency pools, not one count with a GPU share inside it. A container
  counts against the CPU pool, or, when it asks for cards, against the GPU pool
  by the number of cards it holds, never both. One count would mean GPU work
  crowding out the web apps the same plan promises, and it would mean a container
  asking for eight cards costing an account the same as one asking for none.
  Cards rather than containers because cards are what is scarce and what the
  hourly rate is charged per.
- A workspace is admitted against the plan too, and the first one is always
  allowed with no account required. Sign-in provisions the default workspace
  before billing exists, so a check that read terms there would refuse a customer
  the workspace their sign-in was creating, and the sign-in meant to produce both
  would produce neither. Adoption of a workspace that already carries the name is
  not a creation and is not gated. Refusing it would lock an account out of
  workspaces it already holds.
- All of it is read from local rows rather than the provider, because these
  questions are asked on every container start and a balance call there is a
  start that fails whenever the provider is slow. Whether a card exists is
  therefore a column, refreshed when one is attached and re-asked of the provider
  at each cycle boundary, which is also what makes a card *removed* take effect
  at the end of the period rather than under the work already running.
- The allowance counter loses nothing to the renewal seam. A cost priced between
  a cycle ending at the provider and the delivery that opens the next one here has
  no period to be counted against at that moment, and is read back from the ledger
  when that period opens rather than dropped. The ledger row was written in the
  same transaction, so the figure the customer is shown and the usage they are
  invoiced for stay the same number.
- The outbox is claimed, never derived. The sweep sends rows the pricer wrote in
  the transaction that priced the usage; it never scans usage, never groups, and
  never computes a cost. A sweep that recomputed its work set every run would put
  a permanently failing account at the front of every batch and hold everything
  behind it, the defect this shape exists to remove.
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
  the provider will always reject because it is malformed or timestamped outside
  the period they still accept usage for, is abandoned with a durable
  `billing.meter_event.abandoned` error event naming the identifier, workspace,
  dimension and value, and the row is kept as the evidence of money that never
  left. Nothing here restates a provider's limits as a number of its own to keep
  in step.
- The abandoned backlog is a counted figure and not only an event. A row is
  abandoned once, is never pruned, and its delta is gone after the tick that
  produced it, so the standing count and what those rows metered are asked of
  the outbox, a number an operator watches, which only falls when somebody has
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
