-- name: TryPlacementLock :one
-- Serializes placers for the rest of the transaction, so the capacity
-- snapshot stays exact until the assignments commit.
select pg_try_advisory_xact_lock(hashtextextended('placement', 0))::bool;

-- name: PendingContainers :many
-- Pending containers that fit the largest free host in each resource and
-- whose target has a host, in per-workspace round robin: every workspace's
-- oldest request, then every workspace's second, and so on. A container's
-- target is its release's machine pin, else its workspace's connection,
-- else the platform; a mirror build always runs on the platform. Reads the
-- containers_pending partial index. Each container of the batch reads
-- its workspace bucket's region and the measured bytes of the platform
-- volumes its release mounts, which its start reads from there.
select q.id, q.workspace_id, q.release_id, q.cpu_millis, q.memory_bytes, q.connection_id,
       q.machine, q.region, q.zone, q.preemptible, q.gpus, q.gpu_count, q.disks, q.disk_host,
       sb.region as read_region, coalesce(vb.bytes, 0)::bigint as read_bytes
from (
    select p.id, p.workspace_id, p.release_id, p.cpu_millis, p.memory_bytes, p.connection_id,
           p.machine, p.region, p.zone, p.preemptible, p.gpus, p.gpu_count, p.disks, p.disk_host,
           p.volumes, p.turn, p.created_at
    from (
        select c.id, c.workspace_id, c.release_id, c.cpu_millis, c.memory_bytes, c.created_at,
               cw.connection_id,
               jsonb_array_length(coalesce(r.spec -> 'disks', '[]'::jsonb))::int as disks,
               -- The host that last held one of the container's disks, while its
               -- frame cache likely holds the disk: the disk is still saving
               -- there or was released within disk_warm_seconds.
               (select hc.host_id
                from disks d
                join containers hc on hc.id = d.holder_container_id
                where d.workspace_id = c.workspace_id and d.state = 'active'
                  and d.name in (select jsonb_array_elements(r.spec -> 'disks') ->> 'name')
                  and (d.released_at is null or d.released_at > now() - make_interval(secs => @disk_warm_seconds::float8))
                order by d.released_at desc nulls first
                limit 1) as disk_host,
               coalesce(r.spec -> 'placement' ->> 'machine', '')::text as machine,
               coalesce(r.spec -> 'placement' ->> 'region', '')::text as region,
               coalesce(r.spec -> 'placement' ->> 'availability_zone', '')::text as zone,
               coalesce((r.spec -> 'placement' ->> 'preemptible')::boolean, true)::bool as preemptible,
               array(select jsonb_array_elements_text(coalesce(r.spec -> 'resources' -> 'gpu', build_gpus(c.image_build_id), '[]'::jsonb)))::text[] as gpus,
               coalesce((r.spec -> 'resources' ->> 'gpu_count')::int, 0)::int as gpu_count,
               r.spec -> 'volumes' as volumes,
               row_number() over (partition by c.workspace_id order by c.created_at, c.id) as turn
        from containers c
        left join releases r on r.id = c.release_id
        left join image_builds b on b.id = c.image_build_id
        left join workspaces cw on cw.id = c.workspace_id and b.mirror is not true
        where c.state = 'pending'
          and c.cpu_millis <= @max_free_cpu_millis
          and c.memory_bytes <= @max_free_memory_bytes
    ) p
    where (case
               when p.machine <> '' then 'machine:' || p.workspace_id::text || ':' || p.machine
               when p.connection_id is not null then 'connection:' || p.connection_id::text
               else 'platform'
           end) = any(@targets::text[])
    order by p.turn, p.created_at, p.id
    limit @batch_size
) q
cross join lateral (select volume_read_bytes(q.workspace_id, q.volumes) as bytes) vb
left join workspace_buckets sb on sb.workspace_id = q.workspace_id and vb.bytes is not null
order by q.turn, q.created_at, q.id;

-- name: AssignContainers :many
-- The state check loses to a planner that stopped the container meanwhile.
-- Hosts are locked FOR SHARE and must still take work, so an assignment
-- either commits before host loss, a drain or a removal touches the host's
-- containers, or skips a host that one of them changed meanwhile. A host
-- whose agent must move to the target release takes none. The host
-- decides what billing prices: its GPU model and whose machine it is.
with online as (
    select h.id, h.kind, h.gpu_type from hosts h
    where h.id = any(@host_ids::uuid[]) and h.state = 'online' and h.phase = 'ready'
      and h.capacity_state = 'available'
      and not exists (select 1 from agent_updates u where u.host_id = h.id)
    for share of h
)
update containers c
set state = 'starting', host_id = a.host_id, assigned_at = now(), capacity_wait = null,
    gpu_type = case when c.gpu_count > 0 then online.gpu_type else '' end,
    billing_owner = case online.kind
        when 'connection' then 'connected_cloud'
        when 'machine' then 'self_hosted'
        else 'platform_fleet'
    end
from (select unnest(@ids::uuid[]) as id, unnest(@host_ids::uuid[]) as host_id) a
join online on online.id = a.host_id
where c.id = a.id and c.state = 'pending'
returning c.id, c.host_id, c.release_id, c.created_at, c.traceparent;
