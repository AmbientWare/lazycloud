-- Attempts the task lost to preemption. Each retried one adds an attempt to
-- max_attempts, so preemptions never spend the user's retries.
alter table tasks add column preemptions integer not null default 0 check (preemptions >= 0);
