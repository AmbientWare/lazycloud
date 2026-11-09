-- Where platform administrators hear of new accounts and of the platform
-- fleet's spend, and the spend that alerts them. One row, written when an
-- administrator first sets it.
create table admin_alerts (
    singleton boolean primary key default true check (singleton),
    email text not null default '',
    -- The fleet's cost per hour at this moment, and over the last 24 hours,
    -- that alerts; null alerts on neither.
    hourly_limit_micros bigint check (hourly_limit_micros > 0),
    daily_limit_micros bigint check (daily_limit_micros > 0),
    -- Set when a limit's alert is sent and cleared once spend falls back
    -- under it, so each crossing sends one email.
    hourly_over boolean not null default false,
    daily_over boolean not null default false,
    updated_at timestamptz not null default now()
);
