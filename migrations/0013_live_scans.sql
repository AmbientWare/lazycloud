-- Host commands resend cancels of attempts that ended without their slot
-- knowing. Reading only those attempts keeps each host report independent of
-- how many attempts a live container finished recently.
create index attempts_ended_unseen on attempts (container_id, finished_at)
    where state in ('cancelled', 'timed_out', 'lost');
