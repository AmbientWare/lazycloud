-- Whether a release's serve containers may pack into a fleet GPU host's
-- spare room: a function's that can move.
create function release_packs(spec jsonb) returns boolean
language sql immutable
return spec ->> 'kind' = 'function' and release_moves(spec);

-- Whether a container that reserves container_gpus cards on a host is CPU
-- work packed into the spare room of a fleet GPU host, which GPU work
-- placed there evicts.
create function packed(provider text, host_gpus integer, container_gpus integer) returns boolean
language sql immutable
return provider = 'aws' and host_gpus > 0 and container_gpus = 0;

-- The attempt ended by preemption: its host was lost or reclaimed, or GPU
-- work evicted its container. Billing charges none of its time.
alter table attempts add column preempted boolean not null default false;

create index attempts_preempted on attempts (container_id, started_at) where preempted;

-- When the cooldowns of the pools a requested host's launch walk refused
-- end, which the pools it refuses in later passes share.
alter table hosts add column walk_cooled_until timestamptz;
