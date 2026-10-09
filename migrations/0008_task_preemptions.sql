-- Attempts the task lost to preemption. Each adds an attempt to
-- max_attempts, so user retries never pay for one; execution fails the task
-- once it reaches its preemption bound.
alter table tasks add column preemptions integer not null default 0 check (preemptions >= 0);
