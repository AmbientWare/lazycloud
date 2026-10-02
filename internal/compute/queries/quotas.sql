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
