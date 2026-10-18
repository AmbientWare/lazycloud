alter table cloud_connections
    drop column warm_cpu,
    drop column warm_gpu;

-- Each account's EC2 vCPU quotas: the platform's under 'platform', a
-- connected account's under its connection id.
alter table fleet_quotas add column owner text not null default 'platform';
alter table fleet_quotas drop constraint fleet_quotas_pkey;
alter table fleet_quotas add primary key (owner, region, quota_class, market);
alter table fleet_quotas alter column owner drop default;
