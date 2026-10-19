-- How the fleet served one mixedload run, read from the stack's database.
--
--     psql "$DATABASE_URL" -v app=mixedload_10191530 -f acceptance/mixedload/report.sql
--
-- The run's window is from its first container's creation to its last
-- container's stop. Fleet rows count every cloud host in that window, so run
-- it on a stack nothing else loads.

\set ON_ERROR_STOP on

\echo '== starts by function: wait to placement and to ready, seconds'
with run as (
    select c.*, wl.name as function
    from containers c
    join workloads wl on wl.id = c.workload_id
    join apps a on a.id = wl.app_id
    where a.name = :'app'
), w as (
    select min(created_at) as since, coalesce(max(stopped_at), now()) as until from run
)
select function, count(*) as tasks,
       percentile_cont(0.5) within group (order by extract(epoch from assigned_at - created_at))::numeric(8, 1) as placed_p50,
       percentile_cont(0.95) within group (order by extract(epoch from assigned_at - created_at))::numeric(8, 1) as placed_p95,
       percentile_cont(0.5) within group (order by extract(epoch from ready_at - created_at))::numeric(8, 1) as ready_p50,
       percentile_cont(0.95) within group (order by extract(epoch from ready_at - created_at))::numeric(8, 1) as ready_p95,
       count(*) filter (where ready_at is null) as never_ready
from run
group by function
order by function;

\echo '== containers that did not finish cleanly'
with run as (
    select c.*, wl.name as function
    from containers c
    join workloads wl on wl.id = c.workload_id
    join apps a on a.id = wl.app_id
    where a.name = :'app'
), w as (
    select min(created_at) as since, coalesce(max(stopped_at), now()) as until from run
)
select function, stop_reason, count(*) from run
where stop_reason is not null and stop_reason not in ('stopped', 'exited')
group by 1, 2 order by 1, 2;

\echo '== hosts the fleet requested in the window'
with run as (
    select c.*, wl.name as function
    from containers c
    join workloads wl on wl.id = c.workload_id
    join apps a on a.id = wl.app_id
    where a.name = :'app'
), w as (
    select min(created_at) as since, coalesce(max(stopped_at), now()) as until from run
)
select h.market, h.gpu_count > 0 as gpu, count(*) as requested, count(h.launched_at) as launched,
       count(*) filter (where h.failure = 'no_capacity') as no_capacity,
       count(*) filter (where h.phase = 'failed' and h.failure is distinct from 'no_capacity') as failed,
       round(avg(h.launch_pools), 2) as pools_tried,
       percentile_cont(0.5) within group (order by extract(epoch from h.launched_at - h.created_at))::numeric(8, 1) as launch_p50_s,
       count(*) filter (where h.reserve_mode is not null) as reserves
from hosts h, w
where h.kind in ('platform', 'connection') and h.created_at between w.since and w.until
group by 1, 2 order by 1, 2;

\echo '== reserve wakes in the window: count and resume to ready, seconds'
with run as (
    select c.*, wl.name as function
    from containers c
    join workloads wl on wl.id = c.workload_id
    join apps a on a.id = wl.app_id
    where a.name = :'app'
), w as (
    select min(created_at) as since, coalesce(max(stopped_at), now()) as until from run
), wakes as (
    select c.host_id, c.at,
           (select min(r.at) from host_phase_changes r where r.host_id = c.host_id and r.at > c.at and r.phase = 'ready') as ready_at
    from host_phase_changes c, w
    where c.phase = 'resuming' and c.at between w.since and w.until
)
select count(*) as wakes, count(ready_at) as woke,
       percentile_cont(0.5) within group (order by extract(epoch from ready_at - at))::numeric(8, 1) as ready_p50,
       percentile_cont(0.95) within group (order by extract(epoch from ready_at - at))::numeric(8, 1) as ready_p95
from wakes;

\echo '== Spot interruptions in the window'
with run as (
    select c.*, wl.name as function
    from containers c
    join workloads wl on wl.id = c.workload_id
    join apps a on a.id = wl.app_id
    where a.name = :'app'
), w as (
    select min(created_at) as since, coalesce(max(stopped_at), now()) as until from run
)
select count(*) as interrupted from hosts h, w
where h.interruption_at between w.since and w.until;

\echo '== fleet cost over the window, dollars: each host''s hourly price while its instance ran'
with run as (
    select c.*, wl.name as function
    from containers c
    join workloads wl on wl.id = c.workload_id
    join apps a on a.id = wl.app_id
    where a.name = :'app'
), w as (
    select min(created_at) as since, coalesce(max(stopped_at), now()) as until from run
), changes as (
    select h.hourly_micros, h.margin_waived, c.phase, c.at,
           lead(c.at, 1, now()) over (partition by c.host_id order by c.at) as until
    from hosts h join host_phase_changes c on c.host_id = h.id
    where h.kind in ('platform', 'connection') and h.hourly_micros is not null
)
select round(sum(hourly_micros * extract(epoch from least(c.until, w.until) - greatest(c.at, w.since)) / 3600) / 1e6, 2) as cost,
       round(sum(hourly_micros * extract(epoch from least(c.until, w.until) - greatest(c.at, w.since)) / 3600)
             filter (where margin_waived) / 1e6, 2) as at_a_loss
from changes c, w
where c.phase in ('provisioning', 'booting', 'joining', 'ready', 'draining', 'preparing', 'stopping', 'resuming')
  and c.until > w.since and c.at < w.until;
