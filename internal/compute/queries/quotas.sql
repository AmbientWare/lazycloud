-- name: UpsertQuotas :exec
-- One account's EC2 vCPU quotas in one region, in one statement. The
-- arrays have one element per quota.
insert into fleet_quotas (owner, region, quota_class, market, vcpus, observed_at, checked_at)
select @owner::text, @region::text, unnest(@classes::text[]), unnest(@markets::text[]), unnest(@vcpus::integer[]), now(), now()
on conflict (owner, region, quota_class, market) do update
set vcpus = excluded.vcpus, observed_at = excluded.observed_at, checked_at = excluded.checked_at;

-- name: CheckQuotas :exec
-- A read of one account's quotas in one region that failed: the region
-- waits the recheck before the next, and its quotas stay as last read.
insert into fleet_quotas (owner, region, quota_class, market, checked_at)
select @owner::text, @region::text, unnest(@classes::text[]), unnest(@markets::text[]), now()
on conflict (owner, region, quota_class, market) do update set checked_at = excluded.checked_at;

-- name: RefuseQuota :exec
-- EC2 refused one account a launch or start for quota: the class has no
-- room in the region until the cooldown ends, whatever the quota read said.
insert into fleet_quotas (owner, region, quota_class, market, refused_until)
values (@owner, @region, @quota_class, @market, now() + make_interval(secs => @seconds::float8))
on conflict (owner, region, quota_class, market) do update set refused_until = excluded.refused_until;

-- name: Quotas :many
select owner, region, quota_class, market, vcpus, observed_at, checked_at, coalesce(refused_until > now(), false)::bool as refused
from fleet_quotas
order by owner, region, quota_class, market;
