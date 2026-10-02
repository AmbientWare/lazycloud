-- name: UpsertQuotas :exec
-- One region's EC2 vCPU quotas, in one statement. The arrays have one
-- element per quota.
insert into fleet_quotas (region, quota_class, market, vcpus, observed_at)
select @region::text, unnest(@classes::text[]), unnest(@markets::text[]), unnest(@vcpus::integer[]), now()
on conflict (region, quota_class, market) do update
set vcpus = excluded.vcpus, observed_at = excluded.observed_at;

-- name: RefuseQuota :exec
-- EC2 refused a launch or start for quota: the class has no room in the
-- region until the cooldown ends, whatever the quota read said.
insert into fleet_quotas (region, quota_class, market, refused_until)
values (@region, @quota_class, @market, now() + make_interval(secs => @seconds::float8))
on conflict (region, quota_class, market) do update set refused_until = excluded.refused_until;

-- name: Quotas :many
select region, quota_class, market, vcpus, observed_at, coalesce(refused_until > now(), false)::bool as refused
from fleet_quotas
order by region, quota_class, market;

-- name: QuotaUsage :many
-- Platform instances that hold vCPU quota: every launched or launching
-- host that is not stopped, by region, type and market.
select region, instance_type, market::text, count(*)::int as hosts
from hosts
where provider = 'aws' and kind = 'platform' and market is not null
  and phase not in ('deleted', 'failed', 'stopped')
group by region, instance_type, market
order by region, instance_type, market;
