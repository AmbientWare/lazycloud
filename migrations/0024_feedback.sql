-- What a person tells the platform's administrators from the dashboard: a
-- feature request, a bug or a support question. A deleted row is hidden from
-- administrators but still counts against its author's rate limit until
-- identity housekeeping removes it a day later.
create table feedback (
    id uuid primary key default uuidv7(),
    user_id uuid not null references users (id) on delete cascade,
    kind text not null check (kind in ('feature', 'bug', 'support')),
    message text not null check (char_length(message) between 1 and 2000),
    created_at timestamptz not null default now(),
    deleted_at timestamptz
);
-- Counts an account's recent feedback against its rate limit.
create index feedback_user_created on feedback (user_id, created_at);
create index feedback_deleted on feedback (deleted_at) where deleted_at is not null;
