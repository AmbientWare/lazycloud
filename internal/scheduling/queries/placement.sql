-- name: TryPlacementLock :one
-- Serializes placers for the rest of the transaction, so the capacity
-- snapshot stays exact until the assignments commit.
select pg_try_advisory_xact_lock(hashtextextended('placement', 0))::bool;

-- name: PendingContainers :many
-- Pending containers that fit the largest free host in each resource, in
-- per-workspace round robin: every workspace's oldest request, then every
-- workspace's second, and so on. Reads the containers_pending partial index.
select p.id, p.workspace_id, p.release_id, p.cpu_millis, p.memory_bytes
from (
    select c.id, c.workspace_id, c.release_id, c.cpu_millis, c.memory_bytes, c.created_at,
           row_number() over (partition by c.workspace_id order by c.created_at, c.id) as turn
    from containers c
    where c.state = 'pending'
      and c.cpu_millis <= @max_free_cpu_millis
      and c.memory_bytes <= @max_free_memory_bytes
) p
order by p.turn, p.created_at, p.id
limit @batch_size;

-- name: AssignContainers :many
-- The state check loses to a planner that stopped the container meanwhile.
-- Hosts are locked FOR SHARE and must still be online, so an assignment
-- either commits before host loss lists the host's containers or skips a
-- host that host loss marked lost meanwhile.
with online as (
    select h.id from hosts h
    where h.id = any(@host_ids::uuid[]) and h.state = 'online'
    for share
)
update containers c
set state = 'starting', host_id = a.host_id, assigned_at = now()
from (select unnest(@ids::uuid[]) as id, unnest(@host_ids::uuid[]) as host_id) a
join online on online.id = a.host_id
where c.id = a.id and c.state = 'pending'
returning c.id, c.host_id, c.release_id;
