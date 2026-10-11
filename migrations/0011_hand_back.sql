-- Since when an on-demand host hands Spot-tolerant work back to Spot: it
-- lends that work no room, and its containers that can run elsewhere are
-- replaced there. The planner sets it while Spot can host the work and
-- nothing else keeps the host up, and clears it once Spot cannot, or the
-- host leaves serving.
alter table hosts add column hands_back_at timestamptz;

-- The container a replacement takes over from when its host hands work
-- back. The replaced container drains once its replacement is ready and
-- bills only until then.
alter table containers add column replaces uuid references containers (id);

create index containers_replaces on containers (replaces) where replaces is not null;

-- Whether a release's containers can be replaced on another host without
-- losing what they hold: a function's or an HTTP workload's, without a
-- disk. Pods, sandboxes and workloads with a disk stay where they run.
create function release_moves(spec jsonb) returns boolean
language sql immutable
return spec ->> 'kind' in ('function', 'endpoint', 'asgi') and jsonb_array_length(coalesce(spec -> 'disks', '[]'::jsonb)) = 0;

-- Whether a live container leaves its host: a replacement of it is ready,
-- or it could run on Spot, its release's containers move and its host
-- hands Spot-tolerant work back.
create function container_leaves(container uuid, on_host uuid, class text, spec jsonb) returns boolean
language sql stable
return exists (select 1 from containers r where r.replaces = container and r.ready_at is not null)
    or class in ('auto', 'pinned') and release_moves(spec)
       and exists (select 1 from hosts h where h.id = on_host and h.hands_back_at is not null);
