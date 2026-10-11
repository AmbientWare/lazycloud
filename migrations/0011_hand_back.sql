-- Since when an on-demand host hands Spot-tolerant work back to Spot: it
-- lends that work no room, and its containers that can move are replaced
-- on Spot. The planner sets it and clears it.
alter table hosts add column hands_back_at timestamptz;

-- The container a replacement takes over from when its host hands work
-- back. The replaced container drains once its replacement is ready and
-- bills only until then.
alter table containers add column replaces uuid references containers (id);

create index containers_replaces on containers (replaces) where replaces is not null;

-- Whether a release's containers can move to a Spot host without losing
-- what they hold: a function's or an HTTP workload's that tolerates Spot,
-- is not pinned to a zone and has no disk. Pods, sandboxes and the rest
-- stay where they run.
create function release_moves(spec jsonb) returns boolean
language sql immutable
return spec ->> 'kind' in ('function', 'endpoint', 'asgi')
    and coalesce((spec -> 'placement' ->> 'preemptible')::boolean, true)
    and coalesce(spec -> 'placement' ->> 'availability_zone', '') = ''
    and jsonb_array_length(coalesce(spec -> 'disks', '[]'::jsonb)) = 0;
