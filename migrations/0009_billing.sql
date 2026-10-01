-- Billing: accounts and plans, credit, usage metering, the ledger, balances
-- and Stripe operations. A workspace bills its owner's account.

-- An account is created on first use with its trial credit. Free accounts
-- have no Stripe subscription, and an account has no Stripe customer until
-- it saves a card, buys credit or changes plan. A subscription's state is a
-- copy Stripe deliveries refresh.
create table billing_accounts (
    user_id uuid primary key references users (id) on delete cascade,
    terms_version text not null default 'free-v2' check (terms_version in ('free-v2', 'team-v3', 'business-v2')),
    status text not null default 'active' check (status in ('active', 'past_due')),
    stripe_customer_id text unique,
    stripe_subscription_id text unique,
    period_started_at timestamptz,
    period_ended_at timestamptz,
    scheduled_terms_version text check (scheduled_terms_version in ('free-v2', 'team-v3', 'business-v2')),
    scheduled_change_at timestamptz,
    payment_method_attached_at timestamptz,
    complimentary_since timestamptz,
    -- Spending controls. A null limit is no limit.
    monthly_usage_limit_nanos bigint check (monthly_usage_limit_nanos >= 0),
    reload_enabled boolean not null default false,
    reload_threshold_cents integer not null default 1000 check (reload_threshold_cents between 0 and 100000),
    reload_amount_cents integer not null default 2000 check (reload_amount_cents between 500 and 100000),
    -- An automatic payment that was declined or needs authentication pauses
    -- reload until the account resumes it.
    reload_paused_purchase_id uuid,
    reload_pause_reason text check (reload_pause_reason in ('declined', 'action_required')),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    check ((scheduled_terms_version is null) = (scheduled_change_at is null)),
    check ((reload_paused_purchase_id is null) = (reload_pause_reason is null))
);

create index billing_accounts_reload on billing_accounts (user_id) where reload_enabled and reload_paused_purchase_id is null;

-- Credit an account holds: the one-time trial, subscription credit for one
-- billing period and purchased credit, which never expires. source names
-- what granted it, so a grant is written once. spent_nanos is what the
-- rollup has covered with it; reversed_nanos is refunded or disputed credit.
create table credit_lots (
    id uuid primary key default uuidv7(),
    user_id uuid not null references billing_accounts (user_id) on delete cascade,
    kind text not null check (kind in ('trial', 'subscription', 'purchased')),
    source text not null,
    amount_nanos bigint not null check (amount_nanos > 0),
    reversed_nanos bigint not null default 0 check (reversed_nanos between 0 and amount_nanos),
    spent_nanos bigint not null default 0,
    effective_at timestamptz not null,
    expires_at timestamptz,
    created_at timestamptz not null default now(),
    unique (user_id, source),
    check (kind <> 'purchased' or expires_at is null),
    check (expires_at is null or expires_at > effective_at)
);

create index credit_lots_unspent on credit_lots (user_id, expires_at) where spent_nanos <> amount_nanos - reversed_nanos;

-- One metered interval of one container: the usage fact and its price in
-- one immutable row. A container's entries are contiguous from its ready_at
-- and keyed by their start; they end on UTC quarter-hours, published rate
-- changes and the container's end. Workspace, app and workload ids carry no
-- foreign keys so the charge outlives what incurred it.
create table ledger_entries (
    id bigint generated always as identity primary key,
    source_kind text not null check (source_kind in ('container')),
    source_id uuid not null,
    started_at timestamptz not null,
    ended_at timestamptz not null,
    user_id uuid not null references billing_accounts (user_id) on delete cascade,
    workspace_id uuid not null,
    app_id uuid,
    workload_id uuid,
    category text check (category in ('image-build')),
    billing_owner text not null check (billing_owner in ('platform_fleet', 'connected_cloud', 'self_hosted')),
    rate_class text not null check (rate_class in ('auto', 'pinned', 'non_preemptible', 'pinned_non_preemptible')),
    gpu_type text,
    gpu_count integer not null check (gpu_count >= 0),
    cpu_millis bigint not null check (cpu_millis >= 0),
    memory_bytes bigint not null check (memory_bytes >= 0),
    pricing_version text not null,
    container_nanos bigint not null check (container_nanos >= 0),
    cpu_nanos bigint not null check (cpu_nanos >= 0),
    memory_nanos bigint not null check (memory_nanos >= 0),
    gpu_nanos bigint not null check (gpu_nanos >= 0),
    cost_nanos bigint generated always as (container_nanos + cpu_nanos + memory_nanos + gpu_nanos) stored,
    unique (source_kind, source_id, started_at),
    check (ended_at > started_at)
);

create index ledger_entries_payer on ledger_entries (user_id, started_at);

-- How far each container is metered. complete marks a stopped container
-- whose last entry is written; such rows are removed once the metering
-- look-back passes them.
create table usage_cursors (
    container_id uuid primary key,
    billed_through timestamptz not null,
    complete boolean not null default false,
    updated_at timestamptz not null default now()
);

create index usage_cursors_complete on usage_cursors (updated_at) where complete;

-- The metering pass's look-back over stopped containers starts here.
create table metering_state (
    singleton boolean primary key default true check (singleton),
    stopped_since timestamptz not null
);

-- Metering finds containers that stopped since its last pass.
create index containers_stopped_metering on containers (stopped_at) where ready_at is not null;

-- An account's cost per UTC hour, added to as entries are written, and how
-- much of it credit covered. uncovered = cost - credited - waived is debt.
create table billing_hours (
    user_id uuid not null references billing_accounts (user_id) on delete cascade,
    hour timestamptz not null,
    cost_nanos bigint not null default 0,
    credited_nanos bigint not null default 0,
    subscription_nanos bigint not null default 0,
    waived_nanos bigint not null default 0,
    primary key (user_id, hour),
    check (credited_nanos + waived_nanos <= cost_nanos and subscription_nanos <= credited_nanos)
);

create index billing_hours_uncovered on billing_hours (user_id, hour) where credited_nanos + waived_nanos < cost_nanos;

-- An account's balance as of its last rollup. due asks for a rollup;
-- recheck_at is the next instant the balance changes without new usage: a
-- lot with credit left expiring, or the next month. accrued_nanos is the
-- cost of the open intervals of the account's live containers, written by
-- each metering pass with their count. Admission and enforcement read
-- balance_nanos - accrued_nanos.
create table billing_balances (
    user_id uuid primary key references billing_accounts (user_id) on delete cascade,
    balance_nanos bigint not null default 0,
    accrued_nanos bigint not null default 0,
    live_containers integer not null default 0,
    month_started_at timestamptz not null,
    month_spent_nanos bigint not null default 0,
    due boolean not null default true,
    recheck_at timestamptz not null,
    rolled_at timestamptz
);

create index billing_balances_due on billing_balances (user_id) where due;
create index billing_balances_recheck on billing_balances (recheck_at);
create index billing_balances_live on billing_balances (user_id) where live_containers > 0;

-- A plan change, inserted before Stripe is called so its id keys the call.
-- One per account is open at a time.
create table plan_changes (
    id uuid primary key default uuidv7(),
    user_id uuid not null references billing_accounts (user_id) on delete cascade,
    from_terms text not null,
    to_terms text not null,
    state text not null default 'open' check (state in ('open', 'applied', 'failed')),
    attempts integer not null default 0,
    next_attempt_at timestamptz not null default now(),
    lease_until timestamptz,
    error text not null default '',
    created_at timestamptz not null default now(),
    finished_at timestamptz
);

create unique index plan_changes_open on plan_changes (user_id) where state = 'open';
create index plan_changes_due on plan_changes (next_attempt_at) where state = 'open';

-- A credit purchase through Checkout (manual) or an off-session charge of
-- the saved card (automatic), inserted before Stripe is called.
create table credit_purchases (
    id uuid primary key default uuidv7(),
    user_id uuid not null references billing_accounts (user_id) on delete cascade,
    kind text not null check (kind in ('manual', 'automatic')),
    request_key uuid,
    amount_nanos bigint not null check (amount_nanos > 0),
    status text not null default 'pending'
        check (status in ('pending', 'action_required', 'succeeded', 'declined', 'cancelled')),
    success_url text not null default '',
    cancel_url text not null default '',
    checkout_session_id text unique,
    checkout_url text,
    checkout_expires_at timestamptz,
    payment_intent_id text unique,
    lot_id uuid references credit_lots (id) on delete set null,
    reversed_nanos bigint not null default 0 check (reversed_nanos >= 0),
    attempts integer not null default 0,
    next_attempt_at timestamptz not null default now(),
    last_error text not null default '',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    funded_at timestamptz,
    check ((kind = 'manual') = (request_key is not null))
);

create unique index credit_purchases_request on credit_purchases (user_id, request_key) where request_key is not null;
-- One automatic payment at a time.
create unique index credit_purchases_automatic_open on credit_purchases (user_id)
    where kind = 'automatic' and status = 'pending';
create index credit_purchases_due on credit_purchases (next_attempt_at) where status = 'pending';
create index credit_purchases_user on credit_purchases (user_id, created_at);

-- Verified Stripe deliveries. The scheduler processes each by fetching the
-- object it names again; processed rows are removed after a retention
-- period.
create table stripe_events (
    id text primary key,
    type text not null,
    object_id text not null,
    -- The customer the event names, kept because a detached payment
    -- method no longer names it when fetched again.
    customer_id text,
    received_at timestamptz not null default now(),
    attempts integer not null default 0,
    next_attempt_at timestamptz not null default now(),
    lease_until timestamptz,
    processed_at timestamptz,
    last_error text not null default ''
);

create index stripe_events_due on stripe_events (next_attempt_at) where processed_at is null;
create index stripe_events_processed on stripe_events (processed_at) where processed_at is not null;
