-- name: InsertMetricSamples :execrows
-- One statement stores a batch from any number of hosts. A sample counts
-- only when its container is assigned to the host that sent it.
insert into container_metric_samples (
    container_id, sampled_at, interval_ms, cpu_usage_usec, memory_rss_bytes, memory_swap_bytes,
    network_rx_bytes, network_tx_bytes, disk_read_bytes, disk_write_bytes,
    gpu_utilization_pct, gpu_memory_used_bytes, gpu_memory_total_bytes, gpu_type)
select s.container_id, s.sampled_at, s.interval_ms, s.cpu_usage_usec, s.memory_rss_bytes, s.memory_swap_bytes,
       s.network_rx_bytes, s.network_tx_bytes, s.disk_read_bytes, s.disk_write_bytes,
       nullif(s.gpu_utilization_pct, 'NaN'::real), nullif(s.gpu_memory_used_bytes, -1),
       nullif(s.gpu_memory_total_bytes, -1), nullif(s.gpu_type, '')
from (
    select unnest(@container_ids::uuid[]) as container_id, unnest(@host_ids::uuid[]) as host_id,
           unnest(@sampled_at::timestamptz[]) as sampled_at, unnest(@interval_ms::int[]) as interval_ms,
           unnest(@cpu_usage_usec::bigint[]) as cpu_usage_usec, unnest(@memory_rss_bytes::bigint[]) as memory_rss_bytes,
           unnest(@memory_swap_bytes::bigint[]) as memory_swap_bytes, unnest(@network_rx_bytes::bigint[]) as network_rx_bytes,
           unnest(@network_tx_bytes::bigint[]) as network_tx_bytes, unnest(@disk_read_bytes::bigint[]) as disk_read_bytes,
           unnest(@disk_write_bytes::bigint[]) as disk_write_bytes, unnest(@gpu_utilization_pct::real[]) as gpu_utilization_pct,
           unnest(@gpu_memory_used_bytes::bigint[]) as gpu_memory_used_bytes,
           unnest(@gpu_memory_total_bytes::bigint[]) as gpu_memory_total_bytes, unnest(@gpu_type::text[]) as gpu_type
) s
join containers c on c.id = s.container_id and c.host_id = s.host_id
on conflict do nothing;

-- name: LockRollup :one
-- The row lock serializes rollups across scheduler replicas, so each minute
-- folds once. The window ends two minutes back, past any ingest transaction
-- still open, and spans at most ten minutes, so a pass after downtime stays
-- bounded and later passes catch up.
select rolled_through::timestamptz as from_ts,
       least(rolled_through + '10 minutes'::interval,
             date_trunc('minute', now() - '2 minutes'::interval))::timestamptz as to_ts
from container_metric_rollup
for update;

-- name: FoldMinutes :execrows
insert into container_metric_minutes (
    container_id, minute, samples, interval_ms, cpu_usage_usec, memory_rss_bytes, memory_swap_bytes,
    network_rx_bytes, network_tx_bytes, disk_read_bytes, disk_write_bytes,
    gpu_utilization_pct, gpu_memory_used_bytes, gpu_memory_total_bytes, gpu_type)
select container_id, date_trunc('minute', sampled_at), count(*), sum(interval_ms), sum(cpu_usage_usec),
       max(memory_rss_bytes), max(memory_swap_bytes), sum(network_rx_bytes), sum(network_tx_bytes),
       sum(disk_read_bytes), sum(disk_write_bytes), avg(gpu_utilization_pct)::real,
       max(gpu_memory_used_bytes), max(gpu_memory_total_bytes), max(gpu_type)
from container_metric_samples
where sampled_at >= @from_ts and sampled_at < @to_ts
group by 1, 2
on conflict (container_id, minute) do nothing;

-- name: AdvanceRollup :exec
update container_metric_rollup set rolled_through = @to_ts where rolled_through < @to_ts;

-- name: DeleteSamplesBefore :execrows
-- A bounded batch of the oldest expired samples.
delete from container_metric_samples
where ctid = any(array(
    select old.ctid from container_metric_samples old where old.sampled_at < @before order by old.sampled_at limit @max_rows));

-- name: DeleteMinutesBefore :execrows
delete from container_metric_minutes
where ctid = any(array(
    select old.ctid from container_metric_minutes old where old.minute < @before order by old.minute limit @max_rows));

-- name: ContainerMetricsScope :one
select c.cpu_millis, c.memory_bytes, c.created_at, c.stopped_at, now()::timestamptz as observed_at,
       (select rolled_through from container_metric_rollup)::timestamptz as rolled_through
from containers c
where c.id = @id and c.workspace_id = @workspace_id and c.release_id is not null;

-- name: SamplePoints :many
select date_bin(@step::interval, sampled_at, to_timestamp(0))::timestamptz as bucket,
       sum(interval_ms)::bigint as interval_ms, sum(cpu_usage_usec)::bigint as cpu_usage_usec,
       max(memory_rss_bytes)::bigint as memory_rss_bytes, max(memory_swap_bytes)::bigint as memory_swap_bytes,
       sum(network_rx_bytes)::bigint as network_rx_bytes, sum(network_tx_bytes)::bigint as network_tx_bytes,
       sum(disk_read_bytes)::bigint as disk_read_bytes, sum(disk_write_bytes)::bigint as disk_write_bytes,
       count(gpu_utilization_pct)::int as gpu_samples, coalesce(avg(gpu_utilization_pct), 0)::float8 as gpu_utilization_pct,
       coalesce(max(gpu_memory_used_bytes), 0)::bigint as gpu_memory_used_bytes,
       coalesce(max(gpu_memory_total_bytes), 0)::bigint as gpu_memory_total_bytes, coalesce(max(gpu_type), '')::text as gpu_type
from container_metric_samples
where container_id = @container_id and sampled_at >= @start_at and sampled_at < @end_at
group by 1
order by 1;

-- name: MinutePoints :many
select date_bin(@step::interval, minute, to_timestamp(0))::timestamptz as bucket,
       sum(interval_ms)::bigint as interval_ms, sum(cpu_usage_usec)::bigint as cpu_usage_usec,
       max(memory_rss_bytes)::bigint as memory_rss_bytes, max(memory_swap_bytes)::bigint as memory_swap_bytes,
       sum(network_rx_bytes)::bigint as network_rx_bytes, sum(network_tx_bytes)::bigint as network_tx_bytes,
       sum(disk_read_bytes)::bigint as disk_read_bytes, sum(disk_write_bytes)::bigint as disk_write_bytes,
       count(gpu_utilization_pct)::int as gpu_samples, coalesce(avg(gpu_utilization_pct), 0)::float8 as gpu_utilization_pct,
       coalesce(max(gpu_memory_used_bytes), 0)::bigint as gpu_memory_used_bytes,
       coalesce(max(gpu_memory_total_bytes), 0)::bigint as gpu_memory_total_bytes, coalesce(max(gpu_type), '')::text as gpu_type
from container_metric_minutes
where container_id = @container_id and minute >= @start_at and minute < @end_at
group by 1
order by 1;
