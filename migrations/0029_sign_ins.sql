-- One row per sign-in. A session's row goes when it ends, so the count of
-- people signing in each day reads these.
create table sign_ins (
    id uuid primary key default uuidv7(),
    user_id uuid not null references users (id) on delete cascade,
    at timestamptz not null default now()
);
create index sign_ins_at on sign_ins (at);
