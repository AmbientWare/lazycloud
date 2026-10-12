-- When GPU work placed on a fleet GPU host evicted the container, CPU work
-- packed into the host's spare room. Its room went to the GPU work then:
-- an idle container claims nothing more and drains once its replacement
-- is ready; a busy one stopped, its attempts preempted, and bills nothing
-- from the first of them.
alter table containers add column evicted_at timestamptz;

-- Whether a live container of a release of spec leaves its host: the host
-- hands work back and the release can move, which on a Spot GPU host
-- moves only CPU work; GPU work evicted it; or a replacement of it is
-- ready.
create or replace function container_leaves(container uuid, host uuid, spec jsonb) returns boolean
language sql stable
return release_moves(spec) and exists (
        select 1 from hosts h
        where h.id = host and h.hands_back_at is not null and (h.market = 'on_demand' or coalesce(release_gpus(spec), 0) = 0)
    )
    or exists (select 1 from containers e where e.id = container and e.evicted_at is not null)
    or exists (select 1 from containers r where r.replaces = container and r.ready_at is not null);
