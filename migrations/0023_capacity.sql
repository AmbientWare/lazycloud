-- A launch EC2 refused in every pool for capacity fails as no_capacity.
alter table hosts drop constraint hosts_failure_check;
alter table hosts add constraint hosts_failure_check check (failure in (
    'agent_download_failed', 'runtime_install_failed', 'network_join_failed', 'provider_identity_failed',
    'agent_enrollment_failed', 'worker_image_pull_failed', 'worker_start_failed',
    'worker_readiness_failed', 'bootstrap_timed_out', 'host_preflight_failed', 'service_lost',
    'machine_record_deleted', 'provider_stopped', 'provider_terminated', 'no_capacity', 'unknown'
));

-- Each phase and capacity state a host enters, with when: wakes, time
-- asleep, time to ready and what a host cost are read from it. Written by
-- the triggers below, in the transaction that changes the host, at the
-- time of each change so changes within one transaction keep their order;
-- purged by age.
create table host_phase_changes (
    host_id uuid not null references hosts (id) on delete cascade,
    at timestamptz not null,
    phase text not null,
    capacity_state text not null,
    primary key (host_id, at, phase, capacity_state)
);
create index host_phase_changes_at on host_phase_changes (at);

create function record_host_phase() returns trigger
language plpgsql as $$
begin
    insert into host_phase_changes (host_id, at, phase, capacity_state)
    values (new.id, clock_timestamp(), new.phase, new.capacity_state)
    on conflict do nothing;
    return null;
end $$;

create trigger hosts_phase_created after insert on hosts
    for each row execute function record_host_phase();
create trigger hosts_phase_updated after update of phase, capacity_state on hosts
    for each row when (old.phase is distinct from new.phase or old.capacity_state is distinct from new.capacity_state)
    execute function record_host_phase();

-- When each quota was last read, or a read of it failed, which paces the
-- next read.
alter table fleet_quotas add column checked_at timestamptz;
