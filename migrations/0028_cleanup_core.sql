-- What starting a release's container takes, as planning admits and creates
-- it: the reservations, the volume mounters, the cards each container holds,
-- the GPU models it accepts, whether placement is preemptible and pinned to a
-- region, zone or machine, and when its newest serve container stopped once
-- starts failed.
create view release_shapes as
select r.id,
       (r.spec -> 'resources' ->> 'cpu_millis')::bigint as cpu_millis,
       ((r.spec -> 'resources' ->> 'memory_mib')::bigint * 1048576)::bigint as memory_bytes,
       release_mounters(r.spec) as mounters,
       release_gpus(r.spec) as gpu_count,
       coalesce(array(select jsonb_array_elements_text(r.spec -> 'resources' -> 'gpu')), '{}')::text[] as gpu_models,
       coalesce((r.spec -> 'placement' ->> 'preemptible')::boolean, true)::bool as preemptible,
       (coalesce(r.spec -> 'placement' ->> 'region', '') <> ''
        or coalesce(r.spec -> 'placement' ->> 'availability_zone', '') <> '')::bool as pinned,
       (coalesce(r.spec -> 'placement' ->> 'machine', '') <> '')::bool as machine,
       r.start_failures,
       case when r.start_failures > 0 then (
           select lc.stopped_at from containers lc
           where lc.release_id = r.id and lc.purpose = 'serve'
           order by lc.id desc limit 1
       ) end::timestamptz as last_stopped_at
from releases r;

-- Whether a preview serves: not stopped, its lease renewed and its deadline
-- not passed.
create function preview_live(p previews) returns boolean
language sql stable
return p.stopped_at is null and p.lease_expires_at > now() and (p.deadline_at is null or p.deadline_at > now());

-- Nothing reads when a schedule or an edge's demand last changed.
alter table schedules drop column created_at, drop column updated_at;
alter table endpoint_loads drop column updated_at;
