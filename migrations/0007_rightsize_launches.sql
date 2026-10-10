-- The costlier idle host a rightsize bought this one to replace. Its launch
-- does not move to another pool when EC2 refuses its own, since the next
-- pool may cost no less than the host it replaces; the planner chooses
-- again. Once it serves it holds the replaced host's warm slots.
alter table hosts add column replaces uuid references hosts (id);

-- When EC2 last refused a launch to replace this host; rightsize leaves it
-- for the cost horizon rather than try each refused pool in turn.
alter table hosts add column rightsize_refused_at timestamptz;

-- What a requested host must hold in whichever pool its launch lands: the
-- work and warm slots the planner bought it for, or its reserve room. A
-- launch moved to another pool keeps it and takes that pool's capacity.
alter table hosts add column holds_cpu_millis bigint, add column holds_memory_bytes bigint;

-- A refusal cools its own zone, or on a quota its class; nothing counts
-- refusals across a region.
alter table capacity_cooldowns drop column refused_at;
